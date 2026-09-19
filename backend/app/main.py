"""FastAPI application: dashboard API, Telnyx webhooks, media WebSocket, and the built dashboard itself."""

import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import BACKEND_DIR, settings

# Force IPv4 DNS resolution (Ashad fix for Cartesia/AWS WebSocket handshake timeouts on Windows).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger.remove()
logger.add(sys.stderr, level=settings.log_level.upper())

from app.api import public, routes, webhooks  # noqa: E402
from app.core.seed import seed_defaults  # noqa: E402
from app.dialer.engine import dialer  # noqa: E402
from app.telephony.telnyx import TELNYX  # noqa: E402
from app.voice.bot import warm_vad  # noqa: E402

FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", str(BACKEND_DIR.parent / "frontend" / "dist")))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.app_secret_key in ("", "CHANGE_ME") or len(settings.app_secret_key) < 32:
        logger.warning("APP_SECRET_KEY is missing or short — set a long random value in backend/.env")
    warm_vad()
    try:
        await seed_defaults()
    except Exception as e:
        logger.error(f"Startup database setup failed: {e}")
    dialer.boot()
    logger.info(f"Medicare VoiceOps ready. Public URL: {settings.public_base_url}")
    yield
    await dialer.shutdown()
    await TELNYX.close()


app = FastAPI(title="Medicare VoiceOps", lifespan=lifespan, docs_url=None if settings.is_production else "/docs",
              redoc_url=None, openapi_url=None if settings.is_production else "/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(public.router)
app.include_router(routes.router)
app.include_router(webhooks.router)


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception):
    logger.exception(f"Unhandled error: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on the server. Check the backend window for details."})


# Serve the built dashboard (frontend/dist) from the same address as the API, so one
# Cloudflare tunnel URL gives you both the dashboard and the Telnyx endpoints.
if (FRONTEND_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith(("api/", "webhooks/", "ws/")):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def root():
        return {"message": "Backend is running. Build the dashboard with 'npm run build' in the frontend folder."}
