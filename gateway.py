from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx
import os
import asyncio
import jwt
from typing import Optional, Dict, Any
from datetime import datetime

app = FastAPI(title="Gateway Service")

# ---------------------- CONFIG ----------------------
TASK_SERVICE_URL = os.getenv("TASK_SERVICE_URL", "http://localhost:8180")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8181")

# JWT Configuration
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Timeout in seconds for backend requests
BACKEND_TIMEOUT = 5

# Maximum number of concurrent requests
MAX_CONCURRENT_TASKS = 10
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Security scheme
security = HTTPBearer()

# ---------------------- AUTHORIZATION ----------------------

class AuthUser:
    """Represents an authenticated user"""
    def __init__(self, user_id: str, username: str, roles: list[str], character_id: Optional[str] = None, lobby_id: Optional[str] = None):
        self.user_id = user_id
        self.username = username
        self.roles = roles
        self.character_id = character_id
        self.lobby_id = lobby_id

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> AuthUser:
    """
    Verify JWT token and extract user information.
    This function validates the Authorization header and does NOT forward it to downstream services.
    """
    token = credentials.credentials
    
    try:
        # Decode and verify the JWT token
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        
        # Check token expiration
        exp = payload.get("exp")
        if exp and datetime.utcnow().timestamp() > exp:
            raise HTTPException(status_code=401, detail="Token has expired")
        
        # Extract user information
        user_id = payload.get("user_id")
        username = payload.get("username")
        roles = payload.get("roles", [])
        character_id = payload.get("character_id")
        
        if not user_id or not username:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        
        return AuthUser(
            user_id=user_id,
            username=username,
            roles=roles,
            character_id=character_id
        )
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authorization failed: {str(e)}")

def require_roles(*required_roles: str):
    """
    Dependency to check if user has required roles.
    Usage: @app.get("/endpoint", dependencies=[Depends(require_roles("admin"))])
    """
    async def role_checker(user: AuthUser = Depends(verify_token)) -> AuthUser:
        if not any(role in user.roles for role in required_roles):
            raise HTTPException(
                status_code=403, 
                detail=f"Access denied. Required roles: {', '.join(required_roles)}"
            )
        return user
    return role_checker

# ---------------------- PROXY FUNCTION ----------------------

async def proxy_request(
    service_url: str, 
    request: Request, 
    user: AuthUser,
    additional_headers: Optional[Dict[str, str]] = None
) -> Response:
    """
    Generic proxy that forwards the request to a backend service.
    Implements timeout and concurrency limit.
    NOTE: Authorization header is NOT forwarded. User context is passed via custom headers.
    """
    async with semaphore:
        try:
            async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
                # Prepare headers - exclude Authorization header
                headers = {
                    k.decode(): v.decode() 
                    for k, v in request.headers.raw 
                    if k.decode().lower() not in ["host", "authorization"]
                }
                
                # Add user context headers for downstream services
                headers["X-User-ID"] = str(user.user_id)
                headers["X-Username"] = user.username
                headers["X-User-Roles"] = ",".join(user.roles)
                if user.character_id:
                    headers["X-Character-ID"] = str(user.character_id)
                
                # Add any additional headers
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

# ---------------------- HEALTH CHECK (NO AUTH) ----------------------

@app.get("/health")
async def health_check():
    """Public health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# ---------------------- USER SERVICE ----------------------
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user_service:3000")

# Public endpoints - no authentication required
@app.post("/api/users")
async def create_user(request: Request):
    """Create user - public endpoint"""
    service_url = f"{USER_SERVICE_URL}/users"  # Note: /users not /api/users
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
    """Get all users - public endpoint"""
    service_url = f"{USER_SERVICE_URL}/users"
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

@app.get("/api/users/{user_id}")
async def get_user(user_id: str, request: Request):
    """Get user by ID - public endpoint"""
    service_url = f"{USER_SERVICE_URL}/users/{user_id}"
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

# ---------------------- GAME SERVICE ----------------------
GAME_SERVICE_URL = os.getenv("GAME_SERVICE_URL", "http://game_service:3005")

@app.post("/api/lobbies")
async def create_lobby(request: Request):
    """Create lobby - public endpoint"""
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
    """Get all lobbies - public endpoint"""
    service_url = f"{GAME_SERVICE_URL}/lobbies"
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

@app.post("/api/lobbies/{lobby_id}/join")
async def join_lobby(lobby_id: str, request: Request):
    """Join lobby - public endpoint, returns JWT token"""
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
    """Get lobby info - requires authentication"""
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}"
    return await proxy_request(service_url, request, user)

@app.patch("/api/lobbies/{lobby_id}/state")
async def update_lobby_state(lobby_id: str, request: Request, user: AuthUser = Depends(verify_token)):
    """Update lobby state - requires authentication"""
    service_url = f"{GAME_SERVICE_URL}/lobbies/{lobby_id}/state"
    return await proxy_request(service_url, request, user)

# ---------------------- TASK SERVICE ----------------------

@app.post("/api/tasks/assign")
async def task_assign(request: Request, user: AuthUser = Depends(verify_token)):
    """Assign tasks - requires authentication"""
    service_url = f"{TASK_SERVICE_URL}/api/tasks/assign"
    return await proxy_request(service_url, request, user)

@app.get("/api/tasks/view/{character_id}")
async def task_view(character_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    """View tasks - requires authentication and ownership verification"""
    # Optional: Verify user owns this character or has admin role
    if user.character_id != character_id and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="You can only view your own character's tasks")
    
    service_url = f"{TASK_SERVICE_URL}/api/tasks/view/{character_id}"
    return await proxy_request(service_url, request, user)

@app.post("/api/tasks/complete/{task_id}/{character_id}")
async def task_complete(
    task_id: int, 
    character_id: int, 
    request: Request, 
    user: AuthUser = Depends(verify_token)
):
    """Complete task - requires authentication and ownership verification"""
    # Optional: Verify user owns this character or has admin role
    if user.character_id != character_id and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="You can only complete tasks for your own character")
    
    service_url = f"{TASK_SERVICE_URL}/api/tasks/complete/{task_id}/{character_id}"
    return await proxy_request(service_url, request, user)

# ---------------------- VOTING SERVICE ----------------------

@app.get("/api/voting/results/{lobby_id}")
async def voting_results(lobby_id: int, request: Request, user: AuthUser = Depends(verify_token)):
    """Get voting results - requires authentication"""
    service_url = f"{VOTING_SERVICE_URL}/api/voting/results/{lobby_id}"
    return await proxy_request(service_url, request, user)

@app.post("/api/voting/cast")
async def voting_cast(request: Request, user: AuthUser = Depends(verify_token)):
    """Cast a vote - requires authentication"""
    service_url = f"{VOTING_SERVICE_URL}/api/voting/cast"
    return await proxy_request(service_url, request, user)

# ---------------------- ADMIN ENDPOINTS (EXAMPLE) ----------------------

@app.get("/api/admin/stats")
async def admin_stats(request: Request, user: AuthUser = Depends(require_roles("admin"))):
    """Admin-only endpoint - requires admin role"""
    # This would call an admin service
    return {"message": "Admin stats", "user": user.username}

# ---------------------- ERROR HANDLERS ----------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom error handler for better error messages"""
    return Response(
        content=f'{{"detail": "{exc.detail}"}}',
        status_code=exc.status_code,
        media_type="application/json"
    )

