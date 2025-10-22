import asyncio
import aiohttp
import os
import socket
import signal

SERVICE_DISCOVERY_URL = os.getenv("SERVICE_DISCOVERY_URL", "http://service_discovery:8500")
SERVICE_NAME = "gateway"
SERVICE_PORT = int(os.getenv("PORT", 8000))
SERVICE_ID = f"{SERVICE_NAME}-{socket.gethostname()}"

heartbeat_task = None


async def register_service():
    """Register this service with the Service Discovery system."""
    registration_data = {
        "service_name": SERVICE_NAME,
        "service_id": SERVICE_ID,
        "host": socket.gethostname(),
        "port": SERVICE_PORT,
        "health_check_url": "/health",
        "metadata": {"version": "1.0.0", "environment": os.getenv("ENV", "development")}
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{SERVICE_DISCOVERY_URL}/register", json=registration_data) as resp:
                if resp.status == 200:
                    print(f"[GATEWAY] Registered with Service Discovery as {SERVICE_ID}")
                    return True
                else:
                    print(f"[GATEWAY] Failed to register (status {resp.status})")
        except Exception as e:
            print(f"[GATEWAY] Registration error: {e}")
    return False


async def send_heartbeat():
    """Send a heartbeat periodically."""
    global heartbeat_task
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.post(f"{SERVICE_DISCOVERY_URL}/heartbeat", json={"service_id": SERVICE_ID}) as resp:
                    if resp.status == 200:
                        print("[GATEWAY] Heartbeat sent successfully")
                    else:
                        print(f"[GATEWAY] Heartbeat failed (status {resp.status})")
            except Exception as e:
                print(f"[GATEWAY] Heartbeat error: {e}")
            await asyncio.sleep(30)  # every 30 seconds


async def start_heartbeat():
    """Start the heartbeat background task."""
    global heartbeat_task
    if heartbeat_task is None or heartbeat_task.done():
        heartbeat_task = asyncio.create_task(send_heartbeat())
        print("[GATEWAY] Heartbeat started")


async def deregister_service():
    """Remove this service from Service Discovery."""
    global heartbeat_task
    if heartbeat_task:
        heartbeat_task.cancel()

    async with aiohttp.ClientSession() as session:
        try:
            async with session.delete(f"{SERVICE_DISCOVERY_URL}/deregister/{SERVICE_ID}") as resp:
                if resp.status == 200:
                    print(f"[GATEWAY] Deregistered successfully ({SERVICE_ID})")
                else:
                    print(f"[GATEWAY] Deregistration failed (status {resp.status})")
        except Exception as e:
            print(f"[GATEWAY] Deregistration error: {e}")


def setup_graceful_shutdown(loop):
    """Ensure clean deregistration on shutdown."""
    def shutdown_signal_handler():
        print("[GATEWAY] Shutting down gracefully...")
        loop.create_task(deregister_service())
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_signal_handler)
