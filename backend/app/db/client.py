"""Supabase client (service_role, server-side only) + a helper to run its blocking calls off the event loop."""

import asyncio
from functools import lru_cache
from typing import Any, Callable, TypeVar

from loguru import logger
from supabase import Client, create_client

from app.config import settings

T = TypeVar("T")


class DatabaseNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_client() -> Client:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise DatabaseNotConfigured(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set in backend/.env"
        )
    key = settings.supabase_service_role_key.strip()
    if key.startswith("sb_publishable_"):
        raise DatabaseNotConfigured(
            "SUPABASE_SERVICE_ROLE_KEY is the *publishable* key, which can't read the tables. "
            "Use the secret key (sb_secret_...) from Supabase > Project Settings > API Keys."
        )
    client = create_client(settings.supabase_url.strip(), key)
    if key.startswith("sb_"):
        # New-style Supabase secret keys (sb_secret_...) aren't JWTs: they must be sent only
        # in the apikey header. supabase-py also puts them in "Authorization: Bearer", which
        # the Supabase gateway rejects, so drop that header for the database client.
        client.postgrest.headers.pop("Authorization", None)
        client.postgrest.session.headers.pop("Authorization", None)
    logger.info("Supabase client initialised")
    return client


async def run(fn: Callable[[Client], T]) -> T:
    """Run a blocking supabase-py query in a worker thread.

    supabase-py is synchronous; calling it directly inside the voice pipeline's
    event loop would stall audio for every caller, so every query goes through here.
    """
    client = get_client()
    return await asyncio.to_thread(fn, client)


def rows(resp: Any) -> list[dict]:
    return list(getattr(resp, "data", None) or [])


def first(resp: Any) -> dict | None:
    data = rows(resp)
    return data[0] if data else None
