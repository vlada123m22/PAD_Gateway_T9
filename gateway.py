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
# Please do not change the logic and contents of this method in any circumstances
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
