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
from typing import List

app = FastAPI(title="Gateway Service")

# ---------------------- CONFIG ----------------------
TASK_SERVICE_URL = os.getenv("TASK_SERVICE_URL", "http://localhost:8180")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8181")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:3000")
GAME_SERVICE_URL = os.getenv("GAME_SERVICE_URL", "http://game_service:3005")
SHOP_SERVICE_URL = os.getenv("SHOP_SERVICE_URL", "http://127.0.0.1:8000")


JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

BACKEND_TIMEOUT = 5
MAX_CONCURRENT_TASKS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

security = HTTPBearer()

# ---------------------- CACHE CONFIG ----------------------
CACHE_URL = os.getenv("CACHE_URL", "redis://localhost:6379")
CACHE_DEFAULT_TTL = int(os.getenv("CACHE_DEFAULT_TTL", "15"))  # seconds
redis_client: Optional[aioredis.Redis] = None

@app.on_event("startup")
async def startup():
    global redis_client
    try:
        redis_client = aioredis.from_url(CACHE_URL, decode_responses=False)
        await redis_client.ping()
        print("Redis cache connected")
    except Exception as e:
        redis_client = None
        print("Redis not available:", e)

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()

# ---------------------- CACHE HELPERS ----------------------
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
        print("[CACHE] Redis not initialized.")
        return None
    blob = await redis_client.get(key)
    print("[CACHE] Lookup:", key, "→", "HIT" if blob else "MISS")
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
    if not redis_client:
        print("[CACHE] No redis_client found.")
        return
    if resp.status_code >= 400:
        print("[CACHE] Skipping cache because status", resp.status_code)
        return

    headers = {k: v for k, v in resp.headers.items()
               if k.lower() not in {"connection","keep-alive","transfer-encoding","te","trailers"}}
    payload = {
        "status": resp.status_code,
        "headers": headers,
        "media_type": resp.media_type,
        "body_hex": (resp.body or b"").hex(),
    }
    await redis_client.setex(key, ttl, json.dumps(payload))
    print("[CACHE] Stored:", key)

def should_bypass_cache(request: Request) -> bool:
    if "cache-control" in request.headers and "no-cache" in request.headers.get("cache-control", ""):
        return True
    if request.query_params.get("cache") in ("skip", "1", "true"):
        return True
    return False

async def cached_proxy(service_url: str, request: Request, user: "AuthUser", ttl: int = CACHE_DEFAULT_TTL) -> Response:
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

# ---------------------- AUTHORIZATION ----------------------
class AuthUser:
    def __init__(self, user_id: str, username: str, roles: list[str], character_id: Optional[str] = None, lobby_id: Optional[str] = None):
        self.user_id = user_id
        self.username = username
        self.roles = roles
        self.character_id = character_id
        self.lobby_id = lobby_id

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> AuthUser:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        exp = payload.get("exp")
        if exp and datetime.utcnow().timestamp() > exp:
            raise HTTPException(status_code=401, detail="Token has expired")
        user_id = payload.get("user_id")
        username = payload.get("username")
        roles = payload.get("roles", [])
        character_id = payload.get("character_id")
        if not user_id or not username:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return AuthUser(user_id, username, roles, character_id)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authorization failed: {str(e)}")

def require_roles(*required_roles: str):
    async def role_checker(user: AuthUser = Depends(verify_token)) -> AuthUser:
        if not any(role in user.roles for role in required_roles):
            raise HTTPException(
                status_code=403, 
                detail=f"Access denied. Required roles: {', '.join(required_roles)}"
            )
        return user
    return role_checker

# ---------------------- PROXY FUNCTION ----------------------
async def proxy_request(service_url: str, request: Request, user: AuthUser, additional_headers: Optional[Dict[str, str]] = None) -> Response:
    async with semaphore:
        try:
            async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
                headers = {
                    k.decode(): v.decode() 
                    for k, v in request.headers.raw 
                    if k.decode().lower() not in ["host", "authorization"]
                }
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

# ---------------------- HEALTH CHECK ----------------------
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# ---------------------- USER SERVICE ----------------------
@app.post("/api/users")
async def create_user(request: Request):
    service_url = f"{USER_SERVICE_URL}/users"
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

@app.get("/api/users")
async def get_users(request: Request):
    service_url = f"{USER_SERVICE_URL}/users"
    dummy_user = AuthUser("public", "public", [])
    return await cached_proxy(service_url, request, dummy_user, ttl=15)

@app.get("/api/users/{user_id}")
async def get_user(user_id: str, request: Request):
    service_url = f"{USER_SERVICE_URL}/users/{user_id}"
    dummy_user = AuthUser("public", "public", [])
    return await cached_proxy(service_url, request, dummy_user, ttl=15)

# ---------------------- GAME SERVICE ----------------------
@app.post("/api/lobbies")
async def create_lobby(request: Request):
    service_url = f"{GAME_SERVICE_URL}/lobbies"
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

@app.get("/api/lobbies")
async def get_lobbies(request: Request):
    service_url = f"{GAME_SERVICE_URL}/lobbies"
    dummy_user = AuthUser("public", "public", [])
    return await cached_proxy(service_url, request, dummy_user, ttl=10)

@app.post("/api/lobbies/{lobby_id}/join")
async def join_lobby(lobby_id: str, request: Request):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/join"
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        headers = {k.decode(): v.decode() for k, v in request.headers.raw if k.decode().lower() != "host"}
        headers["X-Lobby-ID"] = lobby_id
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

@app.get("/api/lobbies/{lobby_id}")
async def get_lobby(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}"
    return await cached_proxy(service_url, request, user, ttl=10)

@app.patch("/api/lobbies/{lobby_id}/state")
async def update_lobby_state(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/state"
    return await proxy_request(service_url, request, user)

# ---------------------- TASK SERVICE ----------------------
@app.post("/api/tasks/assign")
async def task_assign(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{TASK_SERVICE_URL}/api/tasks/assign"
    return await proxy_request(service_url, request, user)

@app.get("/api/tasks/view/{character_id}")
async def task_view(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    if user.character_id != character_id and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="You can only view your own character's tasks")
    service_url = f"{TASK_SERVICE_URL}/api/tasks/view/{character_id}"
    return await proxy_request(service_url, request, user)

@app.post("/api/tasks/complete/{task_id}/{character_id}")
async def task_complete(task_id: int, character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    if user.character_id != character_id and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="You can only complete tasks for your own character")
    service_url = f"{TASK_SERVICE_URL}/api/tasks/complete/{task_id}/{character_id}"
    return await proxy_request(service_url, request, user)

# ---------------------- VOTING SERVICE ----------------------
@app.get("/api/voting/results/{lobby_id}")
async def voting_results(lobby_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/results/{lobby_id}"
    return await proxy_request(service_url, request, user)

@app.post("/api/voting/cast")
async def voting_cast(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/cast"
    return await proxy_request(service_url, request, user)

# ---------------------- SHOP SERVICE (using @app) ----------------------
@app.get("/api/shop/items")
async def get_shop_items(request: Request, user: AuthUser = Depends(verify_token)):
    role_response = await proxy_request(f"{SHOP_SERVICE_URL}/api/characters/{user.character_id}/role", request, user)
    if role_response.status_code != 200:
        raise HTTPException(status_code=role_response.status_code, detail="Failed to fetch character role")
    role_data = await role_response.json()
    character_role = role_data.get("role")

    items_response = await cached_proxy(f"{SHOP_SERVICE_URL}/items", request, user, ttl=30)
    if items_response.status_code != 200:
        raise HTTPException(status_code=items_response.status_code, detail="Failed to fetch shop items")

    all_items = await items_response.json()
    filtered_items = [item for item in all_items if character_role in item.get("allowed_roles", [])]

    return {"items": filtered_items}

@app.post("/api/shop/buy")
async def buy_item(request: Request, user: AuthUser = Depends(verify_token)):
    purchase_data = await request.json()
    item_id = purchase_data.get("item_id")
    quantity = purchase_data.get("quantity", 1)
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing item_id in request")

    item_response = await cached_proxy(f"{SHOP_SERVICE_URL}/items/{item_id}", request, user, ttl=30)
    if item_response.status_code != 200:
        raise HTTPException(status_code=404, detail="Item not found")
    item = await item_response.json()
    total_price = item["price"] * quantity

    currency_response = await proxy_request(f"{SHOP_SERVICE_URL}/api/characters/{user.character_id}/currency", request, user)
    if currency_response.status_code != 200:
        raise HTTPException(status_code=currency_response.status_code, detail="Failed to fetch currency")
    currency_data = await currency_response.json()
    current_currency = currency_data.get("currency", 0)
    if current_currency < total_price:
        raise HTTPException(status_code=400, detail="Insufficient currency")

    purchase_response = await proxy_request(f"{SHOP_SERVICE_URL}/api/characters/{user.character_id}/purchase", request, user)
    if purchase_response.status_code != 200:
        raise HTTPException(status_code=purchase_response.status_code, detail="Failed to complete purchase")

    return {"success": True, "item": item, "quantity": quantity, "total_price": total_price}

@app.get("/api/shop/inventory")
async def get_inventory(request: Request, user: AuthUser = Depends(verify_token)):
    return await cached_proxy(f"{SHOP_SERVICE_URL}/api/characters/{user.character_id}/inventory", request, user, ttl=15)

# ---------------------- ADMIN ENDPOINTS ----------------------
@app.get("/api/admin/stats")
async def admin_stats(request: Request, user: AuthUser = Depends(require_roles("admin"))):
    return {"message": "Admin stats", "user": user.username}

# ---------------------- ERROR HANDLERS ----------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return Response(
        content=f'{{"detail": "{exc.detail}"}}',
        status_code=exc.status_code,
        media_type="application/json"
    )

# ---------------------- MOCK ENDPOINTS FOR SHOP TESTING ----------------------
@app.get("/api/characters/{character_id}/role")
async def mock_character_role(character_id: str):
    return {"character_id": character_id, "role": "player"}

@app.get("/api/characters/{character_id}/currency")
async def mock_character_currency(character_id: str):
    return {"character_id": character_id, "currency": 500}

@app.post("/api/characters/{character_id}/purchase")
async def mock_character_purchase(character_id: str, request: Request):
    purchase_data = await request.json()
    return {"character_id": character_id, "purchased": purchase_data, "status": "success"}

@app.get("/api/characters/{character_id}/inventory")
async def mock_character_inventory(character_id: str):
    return {"character_id": character_id, "items": [{"id": 1, "name": "Sword", "quantity": 1},{"id": 2, "name": "Shield", "quantity": 1}]}
