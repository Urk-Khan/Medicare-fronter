"""Routes that don't need a login: sign-in and a basic liveness check."""

import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth.security import create_token, verify_password
from app.db import repo

router = APIRouter(prefix="/api")

_ATTEMPTS: dict[str, deque] = defaultdict(deque)
_WINDOW = 15 * 60
_MAX_ATTEMPTS = 10


class Login(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: Login, request: Request):
    key = f"{request.client.host if request.client else 'unknown'}:{body.username.lower()}"
    now = time.time()
    attempts = _ATTEMPTS[key]
    while attempts and attempts[0] < now - _WINDOW:
        attempts.popleft()
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
    user = await repo.get_user_by_username(body.username.strip())
    if not user or not verify_password(body.password, user["password_hash"]):
        attempts.append(now)
        raise HTTPException(401, "Wrong username or password.")
    if not user.get("enabled", True):
        raise HTTPException(403, "This account has been disabled. Ask your super admin to switch it back on.")
    attempts.clear()
    from datetime import datetime, timezone

    await repo.update_user(user["id"], {"last_login_at": datetime.now(timezone.utc).isoformat()})
    await repo.audit(user["username"], "login")
    return {"access_token": create_token(user), "token_type": "bearer", "username": user["username"],
            "role": user.get("role") or "admin"}


@router.get("/health")
async def health():
    return {"ok": True, "database": await repo.schema_ready()}
