from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx
import os
import asyncio
import jwt
from typing import Optional, Dict
from datetime import datetime
from redis import asyncio as aioredis
import json
import hashlib

app = FastAPI(title="Gateway Service")

# ============================================================
# CONFIG
# ============================================================
TASK_SERVICE_URL = os.getenv("TASK_SERVICE_URL", "http://localhost:8180")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8181")
TOWN_SERVICE_URL = os.getenv("TOWN_SERVICE_URL", "http://townservice:4001")
CHARACTER_SERVICE_URL = os.getenv("CHARACTER_SERVICE_URL", "http://characterservice:4002")

JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

BACKEND_TIMEOUT = 5
MAX_CONCURRENT_TASKS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
security = HTTPBearer()

# ============================================================
# REDIS CACHE CONFIG
# ============================================================
CACHE_URL = os.getenv("CACHE_URL", "redis://localhost:6379")
CACHE_DEFAULT_TTL = int(os.getenv("CACHE_DEFAULT_TTL", "15"))
redis_client: Optional[aioredis.Redis] = None


@app.on_event("startup")
async def startup():
    global redis_client
    try:
        redis_client = aioredis.from_url(CACHE_URL, decode_responses=False)
        await redis_client.ping()
        print("✅ Redis connected")
    except Exception as e:
        redis_client = None
        print("⚠️ Redis unavailable:", e)


@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()


# ============================================================
# AUTHENTICATION & ROLE MANAGEMENT
# ============================================================
class AuthUser:
    def __init__(self, user_id: str, username: str, roles: list[str],
                 character_id: Optional[str] = None, lobby_id: Optional[str] = None):
        self.user_id = user_id
        self.username = username
        self.roles = roles
        self.character_id = character_id
        self.lobby_id = lobby_id


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> AuthUser:
    token = credentials.credentials
    cache_key = f"auth:{token}"

    # Try cached auth
    if redis_client:
        cached = await redis_client.get(cache_key)
        if cached:
            print(f"[AUTH CACHE] HIT {cache_key}")
            return AuthUser(**json.loads(cached))
        else:
            print(f"[AUTH CACHE] MISS {cache_key}")

    # Decode JWT
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        exp = payload.get("exp")
        if exp and datetime.utcnow().timestamp() > exp:
            raise HTTPException(status_code=401, detail="Token expired")

        user_id = payload.get("user_id")
        username = payload.get("username")
        roles = payload.get("roles", [])
        character_id = payload.get("character_id")
        lobby_id = payload.get("lobby_id")

        if not user_id or not username:
            raise HTTPException(status_code=401, detail="Invalid token payload")

        user_data = {
            "user_id": user_id,
            "username": username,
            "roles": roles,
            "character_id": character_id,
            "lobby_id": lobby_id,
        }
        user = AuthUser(**user_data)

        if redis_client:
            await redis_client.setex(cache_key, 3600, json.dumps(user_data))
            print(f"[AUTH CACHE] Stored {cache_key}")

        return user

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authorization failed: {e}")


def require_roles(*required_roles: str):
    async def role_checker(user: AuthUser = Depends(verify_token)) -> AuthUser:
        if not any(role in user.roles for role in required_roles):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required roles: {', '.join(required_roles)}"
            )
        return user
    return role_checker


# ============================================================
# CACHE HELPERS
# ============================================================
def _cache_key(method: str, full_url: str, headers_raw: list[tuple[bytes, bytes]]) -> str:
    vary = {}
    for h in (b"x-user-id", b"x-user-roles", b"x-character-id"):
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
               if k.lower() not in {"connection", "keep-alive", "transfer-encoding", "te", "trailers"}}
    payload = {
        "status": resp.status_code,
        "headers": headers,
        "media_type": resp.media_type,
        "body_hex": (resp.body or b"").hex(),
    }
    await redis_client.setex(key, ttl, json.dumps(payload))


def should_bypass_cache(request: Request) -> bool:
    cache_control = (request.headers.get("cache-control") or "").lower()
    cache_param = request.query_params.get("cache", "").lower()
    if "no-cache" in cache_control or "no-store" in cache_control:
        return True
    if cache_param in ("skip", "1", "true"):
        return True
    return False


# ============================================================
# PROXY & CACHE WRAPPERS
# ============================================================
async def proxy_request(service_url: str, request: Request,
                        user: Optional[AuthUser] = None,
                        additional_headers: Optional[Dict[str, str]] = None) -> Response:
    async with semaphore:
        try:
            async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
                headers = {
                    k.decode(): v.decode()
                    for k, v in request.headers.raw
                    if k.decode().lower() not in ["host", "authorization"]
                }
                if user:
                    headers["X-User-ID"] = str(user.user_id)
                    headers["X-Username"] = user.username
                    headers["X-User-Roles"] = ",".join(user.roles)
                    if user.character_id:
                        headers["X-Character-ID"] = str(user.character_id)
                if additional_headers:
                    headers.update(additional_headers)

                backend_response = await client.request(
                    method=request.method,
                    url=service_url,
                    headers=headers,
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


async def cached_proxy(service_url: str, request: Request,
                       user: Optional[AuthUser] = None, ttl: int = CACHE_DEFAULT_TTL) -> Response:
    full_url = service_url
    if request.url.query:
        full_url += f"?{request.url.query}"

    if request.method.upper() != "GET" or should_bypass_cache(request) or ttl <= 0:
        resp = await proxy_request(service_url, request, user)
        resp.headers["X-Cache"] = "BYPASS"
        return resp

    key = _cache_key(request.method.upper(), full_url, request.headers.raw)
    hit = await cache_get(key)
    if hit:
        hit.headers["X-Cache"] = "HIT"
        return hit

    resp = await proxy_request(service_url, request, user)
    await cache_set(key, resp, ttl)
    resp.headers["X-Cache"] = "MISS"
    return resp


# ============================================================
# HEALTH CHECK
# ============================================================
@app.get("/health")
async def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ============================================================
# TASK SERVICE
# ============================================================
@app.post("/api/tasks/assign")
async def task_assign(request: Request, user: AuthUser = Depends(verify_token)):
    return await proxy_request(f"{TASK_SERVICE_URL}/api/tasks/assign", request, user)


@app.get("/api/tasks/view/{character_id}")
async def task_view(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    if user.character_id != str(character_id) and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Cannot view another character's tasks")
    return await cached_proxy(f"{TASK_SERVICE_URL}/api/tasks/view/{character_id}", request, user, ttl=15)


@app.post("/api/tasks/complete/{task_id}/{character_id}")
async def task_complete(task_id: int, character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    if user.character_id != str(character_id) and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Cannot complete tasks for another character")
    return await proxy_request(f"{TASK_SERVICE_URL}/api/tasks/complete/{task_id}/{character_id}", request, user)


# ============================================================
# VOTING SERVICE
# ============================================================
@app.get("/api/voting/results/{lobby_id}")
async def voting_results(lobby_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{VOTING_SERVICE_URL}/api/voting/results/{lobby_id}", request, user, ttl=15)


@app.post("/api/voting/cast")
async def voting_cast(request: Request, user: AuthUser = Depends(verify_token)):
    return await proxy_request(f"{VOTING_SERVICE_URL}/api/voting/cast", request, user)


# ============================================================
# TOWN SERVICE
# ============================================================
@app.get("/api/town")
async def town_list(request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{TOWN_SERVICE_URL}/api/town", request, user, ttl=15)


@app.get("/api/town/lobbies/{lobby_id}/locations/{location_id}/occupants")
async def town_occupants(lobby_id: int, location_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{TOWN_SERVICE_URL}/api/town/lobbies/{lobby_id}/locations/{location_id}/occupants", request, user, ttl=10)


@app.post("/api/town/move")
async def town_move(request: Request, user: AuthUser = Depends(verify_token)):
    return await proxy_request(f"{TOWN_SERVICE_URL}/api/town/move", request, user)


@app.get("/api/town/movements")
async def town_movements(request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{TOWN_SERVICE_URL}/api/town/movements", request, user, ttl=10)


@app.get("/api/town/phase/{lobby_id}")
async def get_town_phase(lobby_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{TOWN_SERVICE_URL}/api/town/phase/{lobby_id}", request, user, ttl=5)


@app.post("/api/town/phase/{lobby_id}/toggle")
async def toggle_town_phase(lobby_id: int, request: Request, user: AuthUser = Depends(require_roles("admin"))):
    return await proxy_request(f"{TOWN_SERVICE_URL}/api/town/phase/{lobby_id}/toggle", request, user)


# ============================================================
# CHARACTER SERVICE
# ============================================================
@app.get("/api/characters")
async def get_all_characters(request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{CHARACTER_SERVICE_URL}/api/characters", request, user, ttl=20)


@app.get("/api/characters/user/{user_id}")
async def get_character_by_user(user_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}", request, user, ttl=15)


@app.patch("/api/characters/user/{user_id}")
async def update_character(user_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    return await proxy_request(f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}", request, user)


@app.get("/api/characters/user/{user_id}/balance")
async def get_balance(user_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}/balance", request, user, ttl=15)


@app.post("/api/characters/user/{user_id}/add-gold")
async def add_gold(user_id: int, request: Request, user: AuthUser = Depends(require_roles("admin"))):
    return await proxy_request(f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}/add-gold", request, user)


@app.get("/api/characters/{character_id}")
async def get_character_by_id(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{CHARACTER_SERVICE_URL}/api/characters/{character_id}", request, user, ttl=15)
