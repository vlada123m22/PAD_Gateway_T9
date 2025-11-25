# brokerClient.py
import asyncio
import os
import aio_pika
import json
import uuid
from typing import Optional

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

class BrokerClient:
    def __init__(self):
        self.connection = None
        self.channel = None

    async def connect(self):
        for attempt in range(5):
            try:
                print(f"Trying RabbitMQ connection attempt {attempt+1}/5...")
                self.connection = await aio_pika.connect_robust(RABBITMQ_URL)
                self.channel = await self.connection.channel()
                print("RabbitMQ connected!")
                return
            except Exception as e:
                print("RabbitMQ connection failed:", e)
                await asyncio.sleep(3)

        print("Failed to connect to RabbitMQ after retries. Will retry on publish.")


    async def _on_response(self, message: aio_pika.IncomingMessage):
        async with message.process():
            correlation_id = message.correlation_id
            if correlation_id in self.response_futures:
                future = self.response_futures.pop(correlation_id)
                try:
                    future.set_result(json.loads(message.body.decode()))
                except Exception as e:
                    future.set_exception(e)

    async def publish(self, queue: str, message: dict):
        if not self.channel:
            raise RuntimeError("BrokerClient not connected")
        await self.channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(message).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            ),
            routing_key=queue
        )

    async def publish_and_wait(self, queue: str, message: dict, timeout: int = 5) -> dict:
        if not self.channel:
            raise RuntimeError("BrokerClient not connected")

        correlation_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        self.response_futures[correlation_id] = future

        await self.channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(message).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                reply_to=self.callback_queue.name,
                correlation_id=correlation_id
            ),
            routing_key=queue
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.response_futures.pop(correlation_id, None)
            raise asyncio.TimeoutError("Timeout waiting for broker response")

# Singleton instance
brokerClient = BrokerClient()

# Usage in FastAPI:
# await brokerClient.connect()
# await brokerClient.publish("gateway.user-service.request", {"type": "CREATE_USER", ...})
# response = await brokerClient.publish_and_wait("gateway.user-service.request", {...})
