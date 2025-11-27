from uuid import uuid4
from fastapi import Header
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
import zipfile
import io
import subprocess
from fastapi.responses import StreamingResponse
import logging
from brokerClient import brokerClient
from contextlib import asynccontextmanager

# ---------------------- CONFIG ----------------------
BROKER_URL = os.getenv("BROKER_URL", "http://localhost:8001")
SERVICE_NAME = os.getenv("SERVICE_NAME", "gateway-service")
BACKEND_TIMEOUT = 5

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

MAX_CONCURRENT_TASKS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

security = HTTPBearer()

# ---------------------- CACHE CONFIG ----------------------
CACHE_URL = os.getenv("CACHE_URL", "redis://localhost:6379")
CACHE_DEFAULT_TTL = int(os.getenv("CACHE_DEFAULT_TTL", "15"))  # seconds
redis_client: Optional[aioredis.Redis] = None


# ---------------------- LIFESPAN (DEFINE BEFORE APP) ----------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    global redis_client
    
    # Startup
    print("[GATEWAY] Starting up...")
    
    # Redis
    try:
        redis_client = aioredis.from_url(CACHE_URL, decode_responses=False)
        await redis_client.ping()
        print("[REDIS] Connected successfully")
    except Exception as e:
        print(f"[REDIS] NOT connected: {e}")
        redis_client = None

    # Custom Message Broker
    try:
        await brokerClient.connect()
        print("[BROKER] Connected successfully")
    except Exception as e:
        print(f"[BROKER] Connection failed: {e}")
    
    yield  # Application runs here
    

# ---------------------- APP INITIALIZATION ----------------------
app = FastAPI(
    title="Gateway Service",
    lifespan=lifespan
)

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
               if k.lower() not in {"connection", "keep-alive", "transfer-encoding", "te", "trailers"}}
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


dummy_user = AuthUser("public", "public", [])


# For "authenticating service-to-service requests"
async def get_user_or_internal(
        request: Request,
        x_internal_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))
) -> AuthUser:
    """Get user from JWT token OR allow internal service requests"""

    # Check for internal service token
    if x_internal_token and INTERNAL_SERVICE_TOKEN and x_internal_token == INTERNAL_SERVICE_TOKEN:
        return AuthUser(
            user_id="internal-service",
            username="internal-service",
            roles=["service"],
            character_id=None
        )

    # Otherwise require JWT
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
async def proxy_request(service_url: str, request: Request, user: AuthUser, additional_headers: Optional[Dict[str, str]] = None) -> Response:
    async with semaphore:
        try:
            async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
                headers = {
                    k.decode(): v.decode()
                    for k, v in request.headers.raw
                    if k.decode().lower() not in ["host", "authorization"]
                }
                # standard headers attached by gateway
                headers["X-User-ID"] = str(user.user_id)
                headers["X-Username"] = user.username
                headers["X-User-Roles"] = ",".join(user.roles)

                # attach both X-Character-ID and short 'characterId' for compatibility
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


def get_running_services():
    """
    Automatically discover all running Docker services.
    Returns a dict of service_name -> list of container_ids
    """
    try:
        # Get all running containers with their names
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return {}

        container_names = [name.strip() for name in result.stdout.strip().split('\n') if name.strip()]

        # Group containers by service name
        services = {}
        for container_name in container_names:
            # Try to extract service name (handle different naming patterns)
            # Pattern 1: service_name_replica_1 -> service_name
            # Pattern 2: service-name-1 -> service-name
            # Pattern 3: service_name -> service_name

            # Remove common suffixes like _1, -1, _replica_1, etc.
            import re
            service_name = re.sub(r'[-_](replica[-_])?\d+$', '', container_name)

            # If no suffix was found, use the full container name as service name
            if service_name == container_name or not service_name:
                service_name = container_name

            if service_name not in services:
                services[service_name] = []

            services[service_name].append(container_name)

        return services

    except Exception as e:
        print(f"Error discovering services: {e}")
        return {}


def get_container_ids_for_service(service_name: str):
    try:
        # Try exact name match first
        result = subprocess.run(
            ["docker", "ps", "-q", "-f", f"name={service_name}"],
            capture_output=True,
            text=True,
            timeout=5
        )

        container_ids = [cid.strip() for cid in result.stdout.strip().split('\n') if cid.strip()]
        return container_ids

    except Exception as e:
        print(f"Error getting container IDs for {service_name}: {e}")
        return []


@app.get("/api/logs/download")
async def download_logs(
    request: Request,
    # Remove user parameter entirely
    services: Optional[str] = None,
    lines: int = 1000,
    since: Optional[str] = None
):
    # Limit lines to prevent huge downloads
    lines = min(lines, 10000)

    # Auto-discover all running services
    all_services = get_running_services()

    if not all_services:
        raise HTTPException(
            status_code=503,
            detail="Unable to discover running services. Is Docker accessible?"
        )

    # Determine which services to fetch
    if services:
        requested_services = [s.strip() for s in services.split(",")]
        services_to_fetch = {}

        for req_service in requested_services:
            # Try exact match first
            if req_service in all_services:
                services_to_fetch[req_service] = all_services[req_service]
            else:
                # Try partial match (case-insensitive)
                matches = {k: v for k, v in all_services.items()
                           if req_service.lower() in k.lower()}
                if matches:
                    services_to_fetch.update(matches)

        if not services_to_fetch:
            available = ', '.join(all_services.keys())
            raise HTTPException(
                status_code=400,
                detail=f"No matching services found. Available services: {available}"
            )
    else:
        services_to_fetch = all_services

    # Create ZIP file in memory
    zip_buffer = io.BytesIO()

    try:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Add metadata file
            metadata = f"""Log Export
Generated: {datetime.utcnow().isoformat()}
Services: {', '.join(services_to_fetch.keys())}
Total containers: {sum(len(containers) for containers in services_to_fetch.values())}
Lines per container: {lines}
Since: {since if since else 'All available logs'}
"""
            zip_file.writestr("_metadata.txt", metadata)

            # Fetch logs from each service
            for service_name, container_names in services_to_fetch.items():
                try:
                    # Get container IDs for this service
                    container_ids = get_container_ids_for_service(service_name)

                    if not container_ids:
                        zip_file.writestr(
                            f"{service_name}.log",
                            f"Service '{service_name}' has no running containers.\n"
                        )
                        continue

                    # Fetch logs from each container/replica
                    for idx, container_id in enumerate(container_ids):
                        try:
                            # Build docker logs command
                            cmd = ["docker", "logs"]

                            if since:
                                cmd.extend(["--since", since])

                            cmd.extend(["--tail", str(lines), container_id])

                            result = subprocess.run(
                                cmd,
                                capture_output=True,
                                text=True,
                                timeout=30
                            )

                            # Combine stdout and stderr
                            logs = result.stdout + result.stderr

                            if not logs:
                                logs = f"No logs available for {service_name} (container: {container_id})\n"

                            # Save to ZIP with replica number if multiple containers
                            if len(container_ids) > 1:
                                filename = f"{service_name}_replica_{idx + 1}.log"
                            else:
                                filename = f"{service_name}.log"

                            zip_file.writestr(filename, logs)

                        except subprocess.TimeoutExpired:
                            zip_file.writestr(
                                f"{service_name}_replica_{idx + 1}.log",
                                f"Error: Timeout fetching logs for container {container_id}\n"
                            )
                        except Exception as e:
                            zip_file.writestr(
                                f"{service_name}_replica_{idx + 1}.log",
                                f"Error: {str(e)}\n"
                            )

                except Exception as e:
                    zip_file.writestr(
                        f"{service_name}.log",
                        f"Error fetching logs for service '{service_name}': {str(e)}\n"
                    )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate logs archive: {str(e)}"
        )

    # Prepare ZIP for download
    zip_buffer.seek(0)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"system_logs_{timestamp}.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "X-Log-Services": ",".join(services_to_fetch.keys()),
            "X-Log-Containers": str(sum(len(c) for c in services_to_fetch.values())),
            "X-Log-Lines": str(lines)
        }
    )

@app.get("/api/logs/services")  # Changed path
async def list_log_services(
    request: Request
    # Remove user parameter
):
    try:
        # Auto-discover services
        services_dict = get_running_services()

        if not services_dict:
            return {
                "services": [],
                "total_services": 0,
                "total_containers": 0,
                "message": "No running services found or Docker is not accessible"
            }

        # Get detailed info for each service
        services_list = []
        for service_name, container_names in services_dict.items():
            # Get container IDs
            container_ids = get_container_ids_for_service(service_name)

            # Get status for each container
            containers_info = []
            for container_name in container_names:
                try:
                    # Get container status
                    result = subprocess.run(
                        ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    status = result.stdout.strip() if result.returncode == 0 else "unknown"
                except:
                    status = "unknown"

                containers_info.append({
                    "name": container_name,
                    "status": status
                })

            services_list.append({
                "name": service_name,
                "replica_count": len(container_names),
                "containers": containers_info
            })

        # Sort by service name
        services_list.sort(key=lambda x: x["name"])

        return {
            "services": services_list,
            "total_services": len(services_list),
            "total_containers": sum(s["replica_count"] for s in services_list)
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list services: {str(e)}"
        )


# ---------------------- USER SERVICE ----------------------
@app.post("/api/users")
async def create_user(request: Request):
    body_bytes = await request.body()
    payload = json.loads(body_bytes.decode("utf-8"))

    message = {
        "type": "CREATE_USER",
        "data": payload,
        "metadata": {"request_id": str(uuid4())}
    }

    response = await brokerClient.publish_and_wait(
        queue="gateway.user-service.request",
        message=message,
        timeout=BACKEND_TIMEOUT
    )

    return Response(
        content=json.dumps(response.get("data", {})),
        status_code=response.get("status_code", 200),
        media_type="application/json"
    )

@app.get("/api/users")
async def get_users(request: Request):
    message = {
        "type": "GET_USERS",
        "data": {},
        "metadata": {"request_id": str(uuid4())}
    }
    response = await brokerClient.publish_and_wait(
        queue="gateway.user-service.request",
        message=message,
        timeout=BACKEND_TIMEOUT
    )
    return Response(
        content=json.dumps(response.get("data", [])),
        status_code=response.get("status_code", 200),
        media_type="application/json"
    )


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

@app.post("/api/lobbies/{lobby_id}/start")
async def start_game(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/start"
    return await proxy_request(service_url, request, user)

@app.patch("/api/lobbies/{lobby_id}/state")
async def update_lobby_state(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/state"
    return await proxy_request(service_url, request, user)

@app.get("/api/lobbies/{lobby_id}")
async def get_lobby(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    """Get lobby info - cached and authenticated"""
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}"
    return await cached_proxy(service_url, request, user, ttl=10)

@app.get("/api/lobbies/{lobby_id}/character/{character_id}")
async def get_character(lobby_id: str, character_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/character/{character_id}"
    return await cached_proxy(service_url, request, user, ttl=10)

@app.get("/api/lobbies/{lobby_id}/phase")
async def get_phase(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/phase"
    return await cached_proxy(service_url, request, user, ttl=5)

@app.post("/api/lobbies/{lobby_id}/phase/next")
async def force_phase(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/phase/next"
    return await proxy_request(service_url, request, user)

@app.post("/api/lobbies/{lobby_id}/announcement")
async def send_announcement(lobby_id: str, request: Request):
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/announcement"
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

@app.get("/api/lobbies/character/{target_id}/role")
async def get_character_role(target_id: str, request: Request):
    service_url = f"{GAME_SERVICE_URL}/lobbies/character/{target_id}/role"
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        backend_response = await client.request(
            method=request.method,
            url=service_url,
            headers={k.decode(): v.decode() for k, v in request.headers.raw if k.decode().lower() != "host"},
            params=request.query_params,
        )
        return Response(
            content=backend_response.content,
            status_code=backend_response.status_code,
            headers=dict(backend_response.headers),
            media_type=backend_response.headers.get("content-type"),
        )

@app.post("/api/lobbies/character/{target_id}/status")
async def update_character_status(target_id: str, request: Request):
    service_url = f"{GAME_SERVICE_URL}/lobbies/character/{target_id}/status"
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
async def voting_results(lobby_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/results/{lobby_id}"
    return await cached_proxy(service_url, request, user, ttl=10)


@app.post("/api/voting/cast")
async def voting_cast(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{VOTING_SERVICE_URL}/api/voting/cast"
    return await proxy_request(service_url, request, user)

# ---------------------- RUMORS SERVICE (NEW) ----------------------

@app.post("/api/rumors/generate")
async def gateway_generate_rumors(request: Request, user: AuthUser = Depends(verify_token)):
    """
    Proxy POST /api/rumors/generate to RumorsService.
    Gateway attaches X-User-ID, X-Character-ID and also 'characterId' for compatibility.
    Requires authentication (verify_token).
    """
    service_url = f"{RUMORS_SERVICE_URL}/api/rumors/generate"
    return await proxy_request(service_url, request, user)

@app.get("/api/rumors/{character_id}")
async def gateway_get_rumors(character_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    """
    Cached GET for rumors for a character.
    """
    service_url = f"{RUMORS_SERVICE_URL}/api/rumors/{character_id}"
    return await cached_proxy(service_url, request, user, ttl=CACHE_DEFAULT_TTL)

@app.get("/api/rumors")
async def gateway_list_rumors(request: Request, user: AuthUser = Depends(verify_token)):
    service_url = f"{RUMORS_SERVICE_URL}/api/rumors"
    return await cached_proxy(service_url, request, user, ttl=CACHE_DEFAULT_TTL)

# ---------------------- ADMIN ENDPOINTS (EXAMPLE) ----------------------

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
