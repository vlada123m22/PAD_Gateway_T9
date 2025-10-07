from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
import httpx
import os
import asyncio

app = FastAPI(title="Gateway Service")

# ---------------------- CONFIG ----------------------
TASK_SERVICE_URL = os.getenv("TASK_SERVICE_URL", "http://localhost:8180")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8181")

# Timeout in seconds for backend requests
BACKEND_TIMEOUT = 5

# Maximum number of concurrent requests
MAX_CONCURRENT_TASKS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# ---------------------- PROXY FUNCTION ----------------------

async def proxy_request(service_url: str, request: Request) -> Response:
    """
    Generic proxy that forwards the request to a backend service.
    Implements timeout and concurrency limit.
    """
    async with semaphore:  # Limit concurrency
        try:
            async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
                backend_response = await client.request(
                    method=request.method,
                    url=service_url,
                    headers={k.decode(): v.decode() for k, v in request.headers.raw if k.decode().lower() != "host"},
                    params=request.query_params,
                    content=await request.body(),
                )
                return Response(
                    content=backend_response.content,
                    status_code=backend_response.status_code,
                    headers=dict(backend_response.headers),
                    media_type=backend_response.headers.get("content-type"),
                )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Bad Gateway: {e}")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Gateway Timeout")

# ---------------------- TASK SERVICE ----------------------

@app.post("/api/tasks/assign")
async def task_assign(request: Request):
    service_url = f"{TASK_SERVICE_URL}/api/tasks/assign"
    return await proxy_request(service_url, request)

@app.get("/api/tasks/view/{character_id}")
async def task_view(character_id: int, request: Request):
    service_url = f"{TASK_SERVICE_URL}/api/tasks/view/{character_id}"
    return await proxy_request(service_url, request)

@app.post("/api/tasks/complete/{task_id}/{character_id}")
async def task_complete(task_id: int, character_id: int, request: Request):
    service_url = f"{TASK_SERVICE_URL}/api/tasks/complete/{task_id}/{character_id}"
    return await proxy_request(service_url, request)

# ---------------------- VOTING SERVICE ----------------------

@app.get("/api/voting/results/{lobby_id}")
async def voting_results(lobby_id: int, request: Request):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/results/{lobby_id}"
    return await proxy_request(service_url, request)

@app.post("/api/voting/cast")
async def voting_cast(request: Request):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/cast"
    return await proxy_request(service_url, request)
