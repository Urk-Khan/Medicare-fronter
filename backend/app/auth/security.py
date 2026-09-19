"""Password hashing, JWT tokens, and the 'must be logged in' dependency used by every API route."""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger

from app.config import settings
from app.db import repo

_bearer = HTTPBearer(auto_error=False)

SUPER_ADMIN = "super_admin"
ADMIN = "admin"
ROLES = (SUPER_ADMIN, ADMIN)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode())
    except Exception:
        return False


def create_token(user: dict) -> str:
    payload = {
        "sub": str(user["id"]),
        "usr": user["username"],
        "role": user.get("role") or "admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm="HS256")


async def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Please log in.")
    try:
        payload = jwt.decode(creds.credentials, settings.app_secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Your session expired. Please log in again.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Please log in.")
    user = await repo.get_user_by_id(payload.get("sub", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Please log in.")
    if not user.get("enabled", True):
        raise HTTPException(status_code=403, detail="This account has been disabled.")
    return user


def is_super_admin(user: dict) -> bool:
    return (user.get("role") or "") == SUPER_ADMIN


async def require_super_admin(user: dict = Depends(current_user)) -> dict:
    """Guard for everything only the super admin may see or change. Checked on the server,
    so hiding a page in the dashboard isn't the only thing protecting it."""
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Only the super admin can do this.")
    return user


async def seed_admin() -> None:
    """Create the super admin from ADMIN_USERNAME / ADMIN_PASSWORD in backend/.env.

    The super admin can then add ordinary admin logins on the Users page. If a database from an
    older version only has plain admins, the first one is promoted so nobody is locked out.
    """
    if await repo.count_users() == 0:
        if len(settings.admin_password or "") < 8:
            logger.error("No dashboard users exist and ADMIN_PASSWORD in backend/.env is missing or shorter than 8 "
                         "characters — set it and restart to create the super admin login.")
            return
        await repo.insert_user({
            "username": settings.admin_username.strip() or "admin",
            "password_hash": hash_password(settings.admin_password),
            "full_name": "Super admin",
            "role": SUPER_ADMIN,
            "created_by": "system",
        })
        logger.info(f"Created super admin login '{settings.admin_username}'")
        return
    if await repo.count_users_with_role(SUPER_ADMIN) == 0:
        existing = await repo.get_user_by_username(settings.admin_username.strip() or "admin")
        if not existing:
            everyone = await repo.list_users()
            existing = everyone[0] if everyone else None
        if existing:
            await repo.update_user(existing["id"], {"role": SUPER_ADMIN})
            logger.info(f"Promoted '{existing['username']}' to super admin")
