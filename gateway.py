from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
import httpx
import os
import asyncio
import json
import hashlib
from typing import Optional
from redis import asyncio as aioredis

app = FastAPI(title="Gateway Service")

# ---------------------- CONFIG ----------------------
TASK_SERVICE_URL = os.getenv("TASK_SERVICE_URL", "http://localhost:8180")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8181")
TOWN_SERVICE_URL = os.getenv("TOWN_SERVICE_URL", "http://localhost:4001")
CHARACTER_SERVICE_URL = os.getenv("CHARACTER_SERVICE_URL", "http://localhost:4002")

# Timeout in seconds for backend requests
BACKEND_TIMEOUT = 5

# Maximum number of concurrent requests
MAX_CONCURRENT_TASKS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Cache config
CACHE_URL = os.getenv("CACHE_URL", "redis://localhost:6379")
CACHE_DEFAULT_TTL = int(os.getenv("CACHE_DEFAULT_TTL", "15"))
redis_client: Optional[aioredis.Redis] = None


# ---------------------- LIFECYCLE ----------------------
@app.on_event("startup")
async def startup():
    global redis_client
    try:
        redis_client = aioredis.from_url(CACHE_URL, decode_responses=False)
        await redis_client.ping()
    except Exception:
        redis_client = None

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()


# ---------------------- CACHE HELPERS ----------------------
def _cache_key(method: str, full_url: str, headers_raw: list[tuple[bytes, bytes]]) -> str:
    vary = {}
    for h in (b"authorization", b"x-user-id", b"accept-language"):
        for k, v in headers_raw:
            if k.lower() == h:
                vary[h.decode()] = v.decode(errors="ignore")
    material = json.dumps({"m": method, "u": full_url, "h": vary}, sort_keys=True).encode()
    return "gw:" + hashlib.sha256(material).hexdigest()

async def cache_get(key: str) -> Optional[Response]:
    if not redis_client:
        return None
    blob = await redis_client.get(key)
    if not blob:
        return None
    cached = json.loads(blob)
    return Response(
        content=bytes.fromhex(cached["body_hex"]),
        status_code=cached["status"],
        headers=cached["headers"],
        media_type=cached.get("media_type"),
    )

async def cache_set(key: str, resp: Response, ttl: int):
    if not redis_client or resp.status_code >= 400:
        return
    headers = {k: v for k, v in resp.headers.items()
               if k.lower() not in {"connection","keep-alive","transfer-encoding","proxy-authenticate","proxy-authorization","te","trailers","upgrade"}}
    payload = {
        "status": resp.status_code,
        "headers": headers,
        "media_type": resp.media_type,
        "body_hex": (resp.body or b"").hex(),
    }
    await redis_client.setex(key, ttl, json.dumps(payload))

def should_bypass_cache(request: Request) -> bool:
    if "cache-control" in request.headers and "no-cache" in request.headers.get("cache-control", ""):
        return True
    if request.query_params.get("cache") in ("skip", "1", "true"):
        return True
    return False


# ---------------------- PROXY FUNCTION (unchanged) ----------------------
async def proxy_request(service_url: str, request: Request) -> Response:
    async with semaphore:
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


# ---------------------- CACHE WRAPPER ----------------------
async def cached_proxy(service_url: str, request: Request, ttl: int = CACHE_DEFAULT_TTL) -> Response:
    full_url = service_url
    if request.url.query:
        full_url += f"?{request.url.query}"

    if request.method.upper() != "GET" or should_bypass_cache(request) or ttl <= 0:
        resp = await proxy_request(service_url, request)
        resp.headers["X-Cache"] = "BYPASS"
        return resp

    key = _cache_key(request.method.upper(), full_url, request.headers.raw)

    hit = await cache_get(key)
    if hit:
        hit.headers["X-Cache"] = "HIT"
        return hit

    resp = await proxy_request(service_url, request)
    await cache_set(key, resp, ttl)
    resp.headers["X-Cache"] = "MISS"
    return resp


# ---------------------- TASK SERVICE ----------------------
@app.post("/api/tasks/assign")
async def task_assign(request: Request):
    service_url = f"{TASK_SERVICE_URL}/api/tasks/assign"
    return await proxy_request(service_url, request)

@app.get("/api/tasks/view/{character_id}")
async def task_view(character_id: int, request: Request):
    service_url = f"{TASK_SERVICE_URL}/api/tasks/view/{character_id}"
    return await cached_proxy(service_url, request, ttl=15)

@app.post("/api/tasks/complete/{task_id}/{character_id}")
async def task_complete(task_id: int, character_id: int, request: Request):
    service_url = f"{TASK_SERVICE_URL}/api/tasks/complete/{task_id}/{character_id}"
    return await proxy_request(service_url, request)


# ---------------------- VOTING SERVICE ----------------------
@app.get("/api/voting/results/{lobby_id}")
async def voting_results(lobby_id: int, request: Request):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/results/{lobby_id}"
    return await cached_proxy(service_url, request, ttl=15)

@app.post("/api/voting/cast")
async def voting_cast(request: Request):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/cast"
    return await proxy_request(service_url, request)


# ---------------------- TOWN SERVICE ----------------------
@app.get("/api/town")
async def town_list(request: Request):
    """Example: /api/town?lobbyId=2001"""
    service_url = f"{TOWN_SERVICE_URL}/api/town"
    return await cached_proxy(service_url, request, ttl=15)

@app.get("/api/town/lobbies/{lobby_id}/locations/{location_id}/occupants")
async def town_occupants(lobby_id: int, location_id: int, request: Request):
    service_url = f"{TOWN_SERVICE_URL}/api/town/lobbies/{lobby_id}/locations/{location_id}/occupants"
    return await cached_proxy(service_url, request, ttl=10)

@app.post("/api/town/move")
async def town_move(request: Request):
    service_url = f"{TOWN_SERVICE_URL}/api/town/move"
    return await proxy_request(service_url, request)


# ---------------------- CHARACTER SERVICE ----------------------
@app.get("/api/characters")
async def get_all_characters(request: Request):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters"
    return await cached_proxy(service_url, request, ttl=20)

@app.get("/api/characters/user/{user_id}")
async def get_character_by_user(user_id: int, request: Request):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}"
    return await cached_proxy(service_url, request, ttl=15)

@app.patch("/api/characters/user/{user_id}")
async def update_character(user_id: int, request: Request):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}"
    return await proxy_request(service_url, request)
