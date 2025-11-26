import asyncio
import os
import json
import uuid
from typing import Optional, Dict
import httpx

BROKER_URL = os.getenv("BROKER_URL", "http://localhost:8001")
SERVICE_NAME = os.getenv("SERVICE_NAME", "gateway-service")
MAX_RETRIES = 10
RETRY_DELAY = 3  # seconds
POLL_INTERVAL = 1  # seconds


class BrokerClient:
    def __init__(self):
        self.service_name = SERVICE_NAME
        self.broker_url = BROKER_URL
        self.connected = False
        self.queues: Dict[str, str] = {}
        self.polling_tasks: Dict[str, asyncio.Task] = {}
        self.response_futures: Dict[str, asyncio.Future] = {}
        self.client: Optional[httpx.AsyncClient] = None

    async def connect(self, retries: int = MAX_RETRIES, delay: int = RETRY_DELAY):
        """Connect to the custom message broker via HTTP health check"""
        for attempt in range(1, retries + 1):
            try:
                print(f"Attempting broker connection {attempt}/{retries}...")
                
                # Create persistent HTTP client
                if not self.client:
                    self.client = httpx.AsyncClient(timeout=5.0)
                
                response = await self.client.get(f"{self.broker_url}/health")
                response.raise_for_status()
                
                health_data = response.json()
                if health_data.get("status") == "healthy":
                    self.connected = True
                    print(f"[BROKER] Connected to custom message broker")
                    print(f"[BROKER] Load balancing: {health_data.get('load_balancing')}")
                    print(f"[BROKER] Backend: {health_data.get('broker')}")
                    return
                    
            except Exception as e:
                print(f"[BROKER] Connection failed (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    print(f"[BROKER] Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    print(f"[BROKER] Failed to connect after {retries} attempts")
                    # Don't raise - let the app start anyway
                    return

    async def register_queue(self, queue_name: str):
        """Register a queue with the broker"""
        if not self.connected:
            await self.connect()
        
        try:
            full_queue_name = f"{queue_name}.{self.service_name}"
            
            response = await self.client.post(
                f"{self.broker_url}/register",
                json={
                    "service_name": self.service_name,
                    "topics": [queue_name]
                }
            )
            response.raise_for_status()
            
            self.queues[queue_name] = full_queue_name
            print(f"[BROKER] Registered queue: {queue_name} -> {full_queue_name}")
            
        except Exception as e:
            print(f"[BROKER] Failed to register queue {queue_name}: {e}")
            raise

    async def publish(self, queue: str, message: dict):
        """Publish a message to a queue"""
        if not self.connected:
            raise RuntimeError("BrokerClient not connected")
        
        try:
            response = await self.client.post(
                f"{self.broker_url}/publish",
                json={
                    "topic": queue,
                    "message": message
                }
            )
            response.raise_for_status()
            print(f"[BROKER] Published message to {queue}")
            
        except Exception as e:
            print(f"[BROKER] Failed to publish to {queue}: {e}")
            raise

    async def _poll_queue(self, queue_name: str, callback):
        """Internal method to poll a queue for messages"""
        full_queue_name = self.queues[queue_name]
        
        while queue_name in self.polling_tasks:
            try:
                response = await self.client.get(
                    f"{self.broker_url}/consume/{full_queue_name}",
                    params={"max_messages": 1}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    messages = data.get("messages", [])
                    
                    for msg in messages:
                        try:
                            payload = msg.get("payload", {})
                            await callback(payload)
                            print(f"[BROKER] Processed message from {queue_name}")
                        except Exception as e:
                            print(f"[BROKER] Error processing message: {e}")
                            
            except Exception as e:
                if not isinstance(e, asyncio.CancelledError):
                    print(f"[BROKER] Error polling {queue_name}: {e}")
            
            await asyncio.sleep(POLL_INTERVAL)

    async def consume(self, queue_name: str, callback):
        """Start consuming messages from a queue (polling-based)"""
        if not self.connected:
            await self.connect()
        
        if queue_name not in self.queues:
            await self.register_queue(queue_name)
        
        # Start polling task
        task = asyncio.create_task(self._poll_queue(queue_name, callback))
        self.polling_tasks[queue_name] = task
        print(f"[BROKER] Started consuming queue: {queue_name} (polling every {POLL_INTERVAL}s)")

    async def publish_and_wait(
        self, 
        queue: str, 
        message: dict, 
        timeout: int = 5
    ) -> dict:
        """Publish a message and wait for a response (request-response pattern)"""
        if not self.connected:
            raise RuntimeError("BrokerClient not connected")

        correlation_id = str(uuid.uuid4())
        reply_queue = f"reply.{self.service_name}.{correlation_id}"
        
        # Register temporary reply queue
        await self.register_queue(reply_queue)
        
        # Create future for response
        future = asyncio.get_event_loop().create_future()
        self.response_futures[correlation_id] = future

        # Define callback for reply queue
        async def reply_callback(response: dict):
            if response.get("correlationId") == correlation_id:
                if correlation_id in self.response_futures:
                    future = self.response_futures.pop(correlation_id)
                    if not future.done():
                        future.set_result(response)
                # Stop consuming after receiving response
                await self.stop_consuming(reply_queue)

        # Start consuming from reply queue
        await self.consume(reply_queue, reply_callback)

        # Publish message with reply info
        message_with_reply = {
            **message,
            "correlationId": correlation_id,
            "reply_to": reply_queue
        }

        await self.publish(queue, message_with_reply)

        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self.response_futures.pop(correlation_id, None)
            await self.stop_consuming(reply_queue)
            raise asyncio.TimeoutError("Timeout waiting for broker response")

    async def stop_consuming(self, queue_name: str):
        """Stop consuming from a queue"""
        if queue_name in self.polling_tasks:
            task = self.polling_tasks.pop(queue_name)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            print(f"[BROKER] Stopped consuming queue: {queue_name}")

    async def close(self):
        """Close all connections and stop all polling tasks"""
        # Stop all polling tasks
        for queue_name in list(self.polling_tasks.keys()):
            await self.stop_consuming(queue_name)
        
        # Close HTTP client
        if self.client:
            await self.client.aclose()
            self.client = None
        
        self.connected = False
        print("[BROKER] Closed all connections")


# Singleton instance
brokerClient = BrokerClient()