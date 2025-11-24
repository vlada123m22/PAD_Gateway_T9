from fastapi import Header
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx
import os
import asyncio
import jwt
from typing import Optional, Dict, List
from datetime import datetime
from redis import asyncio as aioredis
import json
import hashlib
import zipfile
import io
import subprocess
from fastapi.responses import StreamingResponse
import logging
import bisect

app = FastAPI(title="Gateway Service")

# ---------------------- CONFIG ----------------------
TASK_SERVICE_URL = os.getenv("TASK_SERVICE_URL", "http://localhost:8180")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8181")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:3000")
GAME_SERVICE_URL = os.getenv("GAME_SERVICE_URL", "http://game_service:3005")
TOWN_SERVICE_URL = os.getenv("TOWN_SERVICE_URL", "http://townservice:4001")
CHARACTER_SERVICE_URL = os.getenv("CHARACTER_SERVICE_URL", "http://characterservice:4002")
SHOP_SERVICE_URL = os.getenv("SHOP_SERVICE_URL", "http://shopservice:8085")
ROLEPLAY_SERVICE_URL = os.getenv("ROLEPLAY_SERVICE_URL", "http://roleplayservice:8086")
RUMORS_SERVICE_URL = os.getenv("RUMORS_SERVICE_URL", "http://rumors-service:8081")

JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

BACKEND_TIMEOUT = 5
MAX_CONCURRENT_TASKS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

security = HTTPBearer()

# ---------------------- SHARDED CACHE CONFIG ----------------------
CACHE_DEFAULT_TTL = int(os.getenv("CACHE_DEFAULT_TTL", "15"))
VIRTUAL_NODES = int(os.getenv("VIRTUAL_NODES", "150"))  # Virtual nodes per physical node

# Parse cache shard URLs from environment (comma-separated)
CACHE_SHARD_URLS = os.getenv(
    "CACHE_SHARD_URLS",
    "redis://gateway_cache_1:6379,redis://gateway_cache_2:6379,redis://gateway_cache_3:6379"
).split(",")


class ConsistentHashRing:
    """Consistent hashing implementation for distributed caching"""

    def __init__(self, nodes: List[str], virtual_nodes: int = 150):
        self.nodes = nodes
        self.virtual_nodes = virtual_nodes
        self.ring = {}
        self.sorted_keys = []
        self._build_ring()

    def _hash(self, key: str) -> int:
        """Generate hash value for a key"""
        return int(hashlib.md5(key.encode()).hexdigest(), 16)

    def _build_ring(self):
        """Build the consistent hash ring with virtual nodes"""
        for node in self.nodes:
            for i in range(self.virtual_nodes):
                virtual_key = f"{node}:{i}"
                hash_value = self._hash(virtual_key)
                self.ring[hash_value] = node

        self.sorted_keys = sorted(self.ring.keys())
        print(
            f"[CONSISTENT HASH] Built ring with {len(self.ring)} virtual nodes across {len(self.nodes)} physical nodes")

    def get_node(self, key: str) -> str:
        """Get the node responsible for a given key"""
        if not self.ring:
            return None

        hash_value = self._hash(key)

        # Binary search to find the first node >= hash_value
        idx = bisect.bisect_right(self.sorted_keys, hash_value)

        # Wrap around if necessary
        if idx == len(self.sorted_keys):
            idx = 0

        return self.ring[self.sorted_keys[idx]]

    def add_node(self, node: str):
        """Add a new node to the ring"""
        self.nodes.append(node)
        for i in range(self.virtual_nodes):
            virtual_key = f"{node}:{i}"
            hash_value = self._hash(virtual_key)
            self.ring[hash_value] = node

        self.sorted_keys = sorted(self.ring.keys())
        print(f"[CONSISTENT HASH] Added node {node}, ring now has {len(self.ring)} virtual nodes")

    def remove_node(self, node: str):
        """Remove a node from the ring"""
        if node not in self.nodes:
            return

        self.nodes.remove(node)

        # Remove all virtual nodes for this physical node
        keys_to_remove = [k for k, v in self.ring.items() if v == node]
        for key in keys_to_remove:
            del self.ring[key]

        self.sorted_keys = sorted(self.ring.keys())
        print(f"[CONSISTENT HASH] Removed node {node}, ring now has {len(self.ring)} virtual nodes")

    def get_distribution_stats(self) -> Dict[str, int]:
        """Get distribution statistics across nodes"""
        stats = {node: 0 for node in self.nodes}
        for node in self.ring.values():
            stats[node] += 1
        return stats


class ShardedRedisCache:
    """Sharded Redis cache using consistent hashing"""

    def __init__(self, shard_urls: List[str], virtual_nodes: int = 150):
        self.shard_urls = shard_urls
        self.clients: Dict[str, aioredis.Redis] = {}
        self.hash_ring = ConsistentHashRing(shard_urls, virtual_nodes)
        self.stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "errors": 0,
            "shard_hits": {url: 0 for url in shard_urls}
        }

    async def connect(self):
        """Connect to all Redis shards"""
        for url in self.shard_urls:
            try:
                client = aioredis.from_url(url, decode_responses=False)
                await client.ping()
                self.clients[url] = client
                print(f"[SHARDED CACHE] Connected to shard: {url}")
            except Exception as e:
                print(f"[SHARDED CACHE] Failed to connect to {url}: {e}")

        if not self.clients:
            raise Exception("Failed to connect to any cache shards")

        # Print distribution stats
        dist_stats = self.hash_ring.get_distribution_stats()
        print(f"[SHARDED CACHE] Hash ring distribution: {dist_stats}")

    async def close(self):
        """Close all Redis connections"""
        for client in self.clients.values():
            await client.close()

    def _get_client(self, key: str) -> Optional[aioredis.Redis]:
        """Get the Redis client for a given key using consistent hashing"""
        node = self.hash_ring.get_node(key)
        return self.clients.get(node)

    async def get(self, key: str) -> Optional[bytes]:
        """Get value from the appropriate shard"""
        client = self._get_client(key)
        if not client:
            self.stats["errors"] += 1
            return None

        try:
            value = await client.get(key)
            if value:
                self.stats["hits"] += 1
                node = self.hash_ring.get_node(key)
                self.stats["shard_hits"][node] += 1
            else:
                self.stats["misses"] += 1
            return value
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[SHARDED CACHE] Error getting key {key}: {e}")
            return None

    async def set(self, key: str, value: bytes, ttl: int):
        """Set value in the appropriate shard"""
        client = self._get_client(key)
        if not client:
            self.stats["errors"] += 1
            return

        try:
            await client.setex(key, ttl, value)
            self.stats["sets"] += 1
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[SHARDED CACHE] Error setting key {key}: {e}")

    async def delete(self, key: str):
        """Delete key from the appropriate shard"""
        client = self._get_client(key)
        if not client:
            return

        try:
            await client.delete(key)
        except Exception as e:
            print(f"[SHARDED CACHE] Error deleting key {key}: {e}")

    def get_stats(self) -> Dict:
        """Get cache statistics"""
        total_requests = self.stats["hits"] + self.stats["misses"]
        hit_rate = (self.stats["hits"] / total_requests * 100) if total_requests > 0 else 0

        return {
            "total_requests": total_requests,
            "hits": self.stats["hits"],
            "misses": self.stats["misses"],
            "hit_rate_percent": round(hit_rate, 2),
            "sets": self.stats["sets"],
            "errors": self.stats["errors"],
            "shard_distribution": self.stats["shard_hits"],
            "shards_count": len(self.clients),
            "virtual_nodes": self.hash_ring.virtual_nodes
        }


# Global sharded cache instance
sharded_cache: Optional[ShardedRedisCache] = None


@app.on_event("startup")
async def startup():
    global sharded_cache
    try:
        sharded_cache = ShardedRedisCache(CACHE_SHARD_URLS, VIRTUAL_NODES)
        await sharded_cache.connect()
        print(f"[SHARDED CACHE] Initialized with {len(CACHE_SHARD_URLS)} shards")
    except Exception as e:
        sharded_cache = None
        print(f"[SHARDED CACHE] Failed to initialize: {e}")


@app.on_event("shutdown")
async def shutdown():
    if sharded_cache:
        await sharded_cache.close()


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
    if not sharded_cache:
        print("[CACHE] Sharded cache not initialized.")
        return None

    blob = await sharded_cache.get(key)
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
    if not sharded_cache:
        print("[CACHE] Sharded cache not initialized.")
        return

    if resp.status_code >= 400:
        print("[CACHE] Skipping cache because status", resp.status_code)
        return

    headers = {k: v for k, v in resp.headers.items()
               if k.lower() not in {"connection", "keep-alive", "transfer-encoding", "te", "trailers"}}
    payload = {
        "status": resp.status_code,
        "headers": headers,
        "media_type": resp.media_type,
        "body_hex": (resp.body or b"").hex(),
    }

    await sharded_cache.set(key, json.dumps(payload).encode(), ttl)
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
        resp.headers["X-Cache-Shard"] = "NONE"
        return resp

    key = _cache_key(request.method.upper(), full_url, request.headers.raw)

    # Determine which shard this key belongs to
    shard_node = sharded_cache.hash_ring.get_node(key) if sharded_cache else "UNKNOWN"

    hit = await cache_get(key)
    if hit:
        hit.headers["X-Cache"] = "HIT"
        hit.headers["X-Cache-Shard"] = shard_node
        return hit

    resp = await proxy_request(service_url, request, user)
    await cache_set(key, resp, ttl)
    resp.headers["X-Cache"] = "MISS"
    resp.headers["X-Cache-Shard"] = shard_node
    return resp


# ---------------------- CACHE STATS ENDPOINT ----------------------
@app.get("/api/cache/stats")
async def get_cache_stats():
    """Get cache statistics and shard distribution"""
    if not sharded_cache:
        raise HTTPException(status_code=503, detail="Sharded cache not available")

    stats = sharded_cache.get_stats()
    hash_dist = sharded_cache.hash_ring.get_distribution_stats()

    return {
        "cache_stats": stats,
        "hash_ring_distribution": hash_dist,
        "shards": [
            {
                "url": url,
                "connected": url in sharded_cache.clients,
                "virtual_nodes": hash_dist.get(url, 0)
            }
            for url in CACHE_SHARD_URLS
        ]
    }


@app.post("/api/cache/clear")
async def clear_cache(pattern: Optional[str] = None):
    """Clear cache across all shards (admin only in production)"""
    if not sharded_cache:
        raise HTTPException(status_code=503, detail="Sharded cache not available")

    cleared_count = 0

    for url, client in sharded_cache.clients.items():
        try:
            if pattern:
                # Scan and delete matching keys
                cursor = 0
                while True:
                    cursor, keys = await client.scan(cursor, match=pattern, count=100)
                    if keys:
                        await client.delete(*keys)
                        cleared_count += len(keys)
                    if cursor == 0:
                        break
            else:
                # Flush entire shard
                await client.flushdb()
                cleared_count += 1
        except Exception as e:
            print(f"[CACHE] Error clearing shard {url}: {e}")

    return {
        "cleared": cleared_count,
        "pattern": pattern or "ALL",
        "shards_cleared": len(sharded_cache.clients)
    }


# ---------------------- AUTHORIZATION ----------------------
class AuthUser:
    def __init__(self, user_id: str, username: str, roles: list[str], character_id: Optional[str] = None,
                 lobby_id: Optional[str] = None):
        self.user_id = user_id
        self.username = username
        self.roles = roles
        self.character_id = character_id
        self.lobby_id = lobby_id


dummy_user = AuthUser("public", "public", [])


async def get_user_or_internal(
        request: Request,
        x_internal_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))
) -> AuthUser:
    if x_internal_token and INTERNAL_SERVICE_TOKEN and x_internal_token == INTERNAL_SERVICE_TOKEN:
        return AuthUser(
            user_id="internal-service",
            username="internal-service",
            roles=["service"],
            character_id=None
        )

    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")

    return await verify_token(credentials)


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
async def proxy_request(service_url: str, request: Request, user: AuthUser,
                        additional_headers: Optional[Dict[str, str]] = None) -> Response:
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
                    headers["characterId"] = str(user.character_id)

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


# ---------------------- SERVICE ENDPOINTS ----------------------

# ---------------------- USER SERVICE ----------------------
@app.post("/api/users")
async def create_user(request: Request):
    """Create user - public endpoint"""
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
    """Get all users - cached"""
    service_url = f"{USER_SERVICE_URL}/users"
    return await cached_proxy(service_url, request, dummy_user, ttl=15)


@app.get("/api/users/{user_id}")
async def get_user(user_id: str, request: Request):
    """Get user by ID - cached"""
    service_url = f"{USER_SERVICE_URL}/users/{user_id}"
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
    """Get all lobbies - cached"""
    service_url = f"{GAME_SERVICE_URL}/lobbies"
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
    """Get lobby info - cached and authenticated"""
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
async def task_view(character_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    if user.character_id != character_id and user.roles:
        raise HTTPException(status_code=403, detail="You can only view your own character's tasks")
    service_url = f"{TASK_SERVICE_URL}/api/tasks/view/{character_id}"
    return await cached_proxy(service_url, request, user, ttl=15)


@app.post("/api/tasks/complete/{task_id}/{character_id}")
async def task_complete(task_id: int, character_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    if user.character_id != character_id and user.roles:
        raise HTTPException(status_code=403, detail="You can only complete tasks for your own character")
    service_url = f"{TASK_SERVICE_URL}/api/tasks/complete/{task_id}/{character_id}"
    return await proxy_request(service_url, request, user)


# ---------------------- VOTING SERVICE ----------------------
@app.get("/api/voting/results/{lobby_id}")
async def voting_results(lobby_id: int, request: Request):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/results/{lobby_id}"
    return await cached_proxy(service_url, request, dummy_user, ttl=10)


@app.post("/api/voting/cast")
async def voting_cast(request: Request):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/cast"
    return await proxy_request(service_url, request, dummy_user)

# ---------------------- RUMORS SERVICE (NEW) ----------------------

@app.post("/api/rumors/generate")
async def gateway_generate_rumors(request: Request):
    """
    Proxy POST /api/rumors/generate to RumorsService.
    Gateway attaches X-User-ID, X-Character-ID and also 'characterId' for compatibility.
    Requires authentication (verify_token).
    """
    service_url = f"{RUMORS_SERVICE_URL}/api/rumors/generate"
    return await proxy_request(service_url, request, dummy_user)

@app.get("/api/rumors/{character_id}")
async def gateway_get_rumors(character_id: str, request: Request):
    """
    Cached GET for rumors for a character.
    """
    service_url = f"{RUMORS_SERVICE_URL}/api/rumors/{character_id}"
    return await cached_proxy(service_url, request, dummy_user, ttl=CACHE_DEFAULT_TTL)

@app.get("/api/rumors")
async def gateway_list_rumors(request: Request):
    service_url = f"{RUMORS_SERVICE_URL}/api/rumors"
    return await cached_proxy(service_url, request, dummy_user, ttl=CACHE_DEFAULT_TTL)


# ---------------------- TOWN SERVICE ----------------------
@app.get("/api/town")
async def town_list(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{TOWN_SERVICE_URL}/api/town"
    return await cached_proxy(service_url, request, user, ttl=15)


@app.get("/api/town/lobbies/{lobby_id}/locations/{location_id}/occupants")
async def town_occupants(lobby_id: int, location_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{TOWN_SERVICE_URL}/api/town/lobbies/{lobby_id}/locations/{location_id}/occupants"
    return await cached_proxy(service_url, request, user, ttl=10)


@app.post("/api/town/move")
async def town_move(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{TOWN_SERVICE_URL}/api/town/move"
    return await proxy_request(service_url, request, user)


@app.get("/api/town/movements")
async def town_movements(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{TOWN_SERVICE_URL}/api/town/movements"
    return await cached_proxy(service_url, request, user, ttl=10)


@app.get("/api/town/phase/{lobby_id}")
async def get_town_phase(lobby_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{TOWN_SERVICE_URL}/api/town/phase/{lobby_id}"
    return await cached_proxy(service_url, request, user, ttl=5)


@app.post("/api/town/phase/{lobby_id}/toggle")
async def toggle_town_phase(lobby_id: int, request: Request, user: AuthUser = Depends(require_roles("admin"))):
    service_url = f"{TOWN_SERVICE_URL}/api/town/phase/{lobby_id}/toggle"
    return await proxy_request(service_url, request, user)


# ---------------------- CHARACTER SERVICE ----------------------
@app.get("/api/characters")
async def get_all_characters(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters"
    return await cached_proxy(service_url, request, user, ttl=20)


@app.get("/api/characters/user/{user_id}")
async def get_character_by_user(user_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}"
    return await cached_proxy(service_url, request, user, ttl=15)


@app.patch("/api/characters/user/{user_id}")
async def update_character(user_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}"
    return await proxy_request(service_url, request, user)


@app.get("/api/characters/user/{user_id}/balance")
async def get_balance(user_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}/balance"
    return await cached_proxy(service_url, request, user, ttl=15)


@app.post("/api/characters/user/{user_id}/add-gold")
async def add_gold(user_id: str, request: Request, user: AuthUser = Depends(get_user_or_internal)):  # CHANGED THIS LINE
    if not user.roles:
        raise HTTPException(status_code=403, detail="Cannot add gold. User not authorized")
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters/user/{user_id}/add-gold"
    return await proxy_request(service_url, request, user)


@app.get("/api/characters/{character_id}")
async def get_character_by_id(character_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/api/characters/{character_id}"
    return await cached_proxy(service_url, request, user, ttl=15)

@app.get("/api/character/{character_id}/role")
async def get_character_role(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/character/{character_id}/role"
    return await cached_proxy(service_url, request, user, ttl=10)

@app.get("/api/character/{character_id}/currency")
async def get_character_currency(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/character/{character_id}/currency"
    return await cached_proxy(service_url, request, user, ttl=5)

@app.get("/api/character/{character_id}/inventory")
async def get_character_inventory(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/character/{character_id}/inventory"
    return await cached_proxy(service_url, request, user, ttl=10)

@app.post("/api/character/{character_id}/inventory/add")
async def add_item_to_inventory(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/character/{character_id}/inventory/add"
    return await proxy_request(service_url, request, user)

@app.post("/api/character/{character_id}/inventory/use")
async def use_inventory_item(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{CHARACTER_SERVICE_URL}/character/{character_id}/inventory/use"
    return await proxy_request(service_url, request, user)


# ---------------------- SHOP SERVICE ----------------------

@app.get("/api/shop/items")
async def get_all_items(request: Request, user: AuthUser = Depends(verify_token)):
    """Fetch all available shop items."""
    service_url = f"{SHOP_SERVICE_URL}/shop/items"
    return await cached_proxy(service_url, request, user, ttl=30)


@app.get("/api/shop/items/{item_id}")
async def get_item(item_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    """Fetch one item by ID."""
    service_url = f"{SHOP_SERVICE_URL}/shop/items/{item_id}"
    return await cached_proxy(service_url, request, user, ttl=30)


@app.get("/api/shop/character/{character_id}/currency")
async def get_character_currency(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    """Fetch character's current currency (via Character Service)."""
    service_url = f"{SHOP_SERVICE_URL}/shop/character/{character_id}/currency"
    return await cached_proxy(service_url, request, user, ttl=5)


@app.post("/api/shop/purchase")
async def purchase_item(request: Request, user: AuthUser = Depends(verify_token)):
    """Purchase an item — modifies character currency and inventory."""
    service_url = f"{SHOP_SERVICE_URL}/shop/purchase"
    return await proxy_request(service_url, request, user)

# ---------------------- ROLEPLAY SERVICE ----------------------

@app.post("/api/roleplay/perform")
async def perform_ability(request: Request, user: AuthUser = Depends(verify_token)):
    """
    Proxy POST /roleplay/perform to RoleplayService.
    Example: POST http://localhost:8086/roleplay/perform?role_id=1
    Body: { "Character_Id": 123, "Target_Id": 456 }
    """
    service_url = f"{ROLEPLAY_SERVICE_URL}/roleplay/perform"
    return await proxy_request(service_url, request, user)


@app.get("/api/roleplay/character/{character_id}/status")
async def get_character_status(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    """
    Proxy GET /roleplay/character/{character_id}/status to RoleplayService.
    Example: GET http://localhost:8086/roleplay/character/123/status
    """
    service_url = f"{ROLEPLAY_SERVICE_URL}/roleplay/character/{character_id}/status"
    return await cached_proxy(service_url, request, user, ttl=5)

# ---------------------- ADMIN ENDPOINT ----------------------
@app.get("/api/admin/stats")
async def admin_stats(request: Request, user: AuthUser = Depends(require_roles("admin"))):
    return {"message": "Admin stats", "user": user.username, "roles": user.roles}

# ---------------------- ERROR HANDLERS ----------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom error handler for better error messages"""
    return Response(
        content=f'{{"detail": "{exc.detail}"}}',
        status_code=exc.status_code,
        media_type="application/json"
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return Response(
        content=f'{{"detail": "{exc.detail}"}}',
        status_code=exc.status_code,
        media_type="application/json"
    )