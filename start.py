"""
Medicare VoiceOps — one-click launcher (same idea as the Ashad agent's start_agent.py).

  1. Starts a Cloudflare tunnel and grabs its public https:// URL
  2. Points your Telnyx Call Control Application's webhooks at that URL
  3. Starts the backend (which also serves the dashboard) with PUBLIC_BASE_URL set

Run:  python start.py
Then open the dashboard at the https://....trycloudflare.com address it prints
(or http://localhost:8000 on this computer).

Options:
  python start.py --no-tunnel     use PUBLIC_BASE_URL from .env as-is (real server)
  python start.py --host 0.0.0.0  bind address (default: 0.0.0.0)
  python start.py --port 8000     port (default: 8000)
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend" if (ROOT_DIR / "backend").is_dir() else ROOT_DIR

# Auto-re-execute using venv Python if running with unactivated system Python
if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
    for candidate in (
        BACKEND_DIR / "venv" / "Scripts" / "python.exe",
        ROOT_DIR / "venv" / "Scripts" / "python.exe",
        BACKEND_DIR / "venv" / "bin" / "python",
        ROOT_DIR / "venv" / "bin" / "python",
    ):
        if candidate.is_file():
            sys.exit(subprocess.call([str(candidate), str(Path(__file__).resolve())] + sys.argv[1:]))

# Run from BACKEND_DIR and ensure both directories are on sys.path
os.chdir(BACKEND_DIR)
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Force IPv4 resolution (prevents Windows IPv6 handshake timeouts) — Ashad fix.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
    from dotenv import dotenv_values
except ImportError as e:
    print(f"[!] Missing required dependency: {e.name}")
    print("[!] Please activate your virtual environment or install dependencies:")
    print("      pip install -r backend/requirements.txt")
    sys.exit(1)

# Check for .env in root or backend; root overrides backend if both are present
ENV: dict = {}
if (BACKEND_DIR / ".env").is_file():
    ENV.update(dotenv_values(BACKEND_DIR / ".env"))
if (ROOT_DIR / ".env").is_file():
    ENV.update(dotenv_values(ROOT_DIR / ".env"))

# Export to os.environ so settings/config picks them up
for k, v in ENV.items():
    if k not in os.environ and v is not None:
        os.environ[k] = v


def find_cloudflared() -> str:
    candidates = (
        ROOT_DIR / "cloudflared.exe",
        ROOT_DIR / "cloudflared",
        BACKEND_DIR / "cloudflared.exe",
        BACKEND_DIR / "cloudflared",
        ROOT_DIR.parent / "cloudflared.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "cloudflared"


def start_tunnel(port: int):
    exe = find_cloudflared()
    print("[*] Starting Cloudflare tunnel...")
    try:
        proc = subprocess.Popen([exe, "tunnel", "--url", f"http://localhost:{port}"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    except FileNotFoundError:
        print("[!] cloudflared was not found. Put cloudflared.exe in the project folder or install it.")
        sys.exit(1)
    pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        match = pattern.search(line or "")
        if match:
            print(f"[OK] Tunnel ready: {match.group(0)}")
            return match.group(0), proc
    print("[!] Could not get a tunnel URL within 30 seconds.")
    proc.terminate()
    sys.exit(1)


def point_telnyx_at(public_url: str) -> None:
    key = os.environ.get("TELNYX_API_KEY") or ENV.get("TELNYX_API_KEY")
    app_id = os.environ.get("TELNYX_CONNECTION_ID") or ENV.get("TELNYX_CONNECTION_ID")
    if not key or not app_id:
        print("[!] TELNYX_API_KEY / TELNYX_CONNECTION_ID missing in .env — skipping webhook update.")
        return
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"https://api.telnyx.com/v2/call_control_applications/{app_id}"
    try:
        current = requests.get(url, headers=headers, timeout=15)
        if current.ok:
            previous = current.json()["data"].get("webhook_event_url")
            if previous and "trycloudflare.com" not in previous:
                print(f"[i] Telnyx app webhook was: {previous}  (save this if another system used it)")
        resp = requests.patch(url, headers=headers, timeout=15,
                              json={"webhook_event_url": f"{public_url}/webhooks/telnyx", "webhook_api_version": "2"})
        if resp.ok:
            print(f"[OK] Telnyx webhooks -> {public_url}/webhooks/telnyx")
        else:
            print(f"[!] Telnyx webhook update failed (HTTP {resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        print(f"[!] Telnyx webhook update error: {e}")


def main() -> None:
    default_port = int(os.environ.get("PORT", 8000))
    parser = argparse.ArgumentParser(description="Medicare VoiceOps Launcher")
    parser.add_argument("--no-tunnel", action="store_true", help="use PUBLIC_BASE_URL from .env as-is (real server)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=default_port, help=f"port to bind (default: {default_port})")
    args = parser.parse_args()

    print("=" * 64)
    print("   MEDICARE VOICEOPS — LAUNCHER")
    print("=" * 64)

    tunnel = None
    if args.no_tunnel:
        # Detect PUBLIC_BASE_URL from env, with fallbacks to Coolify automatic FQDN variables
        coolify_fqdn = os.environ.get("COOLIFY_FQDN") or os.environ.get("FQDN") or os.environ.get("COOLIFY_URL")
        public_url = (
            os.environ.get("PUBLIC_BASE_URL")
            or ENV.get("PUBLIC_BASE_URL")
            or coolify_fqdn
            or ""
        ).rstrip("/")

        # Auto-prepend https:// if scheme is missing
        if public_url and not public_url.startswith(("http://", "https://")):
            public_url = f"https://{public_url}"

        if not public_url:
            print("[!] Warning: PUBLIC_BASE_URL is not set in Coolify environment variables.")
            print("[!] Telnyx webhooks require a public https:// URL to receive calls.")
            print(f"[!] Defaulting to http://localhost:{args.port} for dashboard access.")
            public_url = f"http://localhost:{args.port}"
        elif not public_url.startswith("https://"):
            print(f"[!] Warning: PUBLIC_BASE_URL is '{public_url}' (not https://).")
            print("[!] Telnyx webhooks may fail to deliver unless a secure https:// address is used.")
        else:
            print(f"[OK] Public base URL: {public_url}")

        if public_url.startswith("https://"):
            point_telnyx_at(public_url)
        else:
            print("[i] Skipping automatic Telnyx webhook update (requires https://).")
    else:
        public_url, tunnel = start_tunnel(args.port)
        point_telnyx_at(public_url)
    os.environ["PUBLIC_BASE_URL"] = public_url

    dist_candidates = (
        ROOT_DIR / "frontend" / "dist" / "index.html",
        BACKEND_DIR.parent / "frontend" / "dist" / "index.html",
    )
    if not any(d.is_file() for d in dist_candidates):
        print("[i] Dashboard not built yet — run 'npm install' then 'npm run build' in the frontend folder.")

    caller_id = os.environ.get("TELNYX_FROM_NUMBER") or ENV.get("TELNYX_FROM_NUMBER", "(not set)")

    print("=" * 64)
    print(f"  Dashboard (anywhere):     {public_url}")
    print(f"  Dashboard (this machine): http://localhost:{args.port}")
    print(f"  Calls go out from:        {caller_id}")
    print("  Press Ctrl+C to stop.")
    print("=" * 64)

    try:
        import uvicorn
    except ImportError:
        print("[!] uvicorn is not installed. Install requirements with:")
        print("      pip install -r backend/requirements.txt")
        sys.exit(1)

    try:
        uvicorn.run("app.main:app", host=args.host, port=args.port, app_dir=str(BACKEND_DIR), log_level="info",
                    proxy_headers=True, forwarded_allow_ips="*")
    finally:
        if tunnel:
            print("[*] Closing tunnel...")
            tunnel.terminate()


if __name__ == "__main__":
    main()
