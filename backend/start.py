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
  python start.py --port 8000
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

# Force IPv4 resolution (prevents Windows IPv6 handshake timeouts) — Ashad fix.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

ENV = dotenv_values(HERE / ".env")


def find_cloudflared() -> str:
    for candidate in (HERE / "cloudflared.exe", HERE / "cloudflared", HERE.parent / "cloudflared.exe"):
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
        print("[!] cloudflared was not found. Put cloudflared.exe in the backend folder or install it.")
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
    key, app_id = ENV.get("TELNYX_API_KEY"), ENV.get("TELNYX_CONNECTION_ID")
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-tunnel", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    print("=" * 64)
    print("   MEDICARE VOICEOPS — LAUNCHER")
    print("=" * 64)

    tunnel = None
    if args.no_tunnel:
        public_url = (ENV.get("PUBLIC_BASE_URL") or "").rstrip("/")
        if not public_url.startswith("https://"):
            print("[!] --no-tunnel needs PUBLIC_BASE_URL=https://... in .env")
            sys.exit(1)
    else:
        public_url, tunnel = start_tunnel(args.port)
        point_telnyx_at(public_url)
    os.environ["PUBLIC_BASE_URL"] = public_url

    if not (HERE.parent / "frontend" / "dist" / "index.html").is_file():
        print("[i] Dashboard not built yet — run 'npm install' then 'npm run build' in the frontend folder.")

    print("=" * 64)
    print(f"  Dashboard (anywhere):     {public_url}")
    print(f"  Dashboard (this PC):      http://localhost:{args.port}")
    print(f"  Calls go out from:        {ENV.get('TELNYX_FROM_NUMBER', '(not set)')}")
    print("  Press Ctrl+C to stop.")
    print("=" * 64)

    import uvicorn

    try:
        uvicorn.run("app.main:app", host="0.0.0.0", port=args.port, log_level="info", proxy_headers=True,
                    forwarded_allow_ips="*")
    finally:
        if tunnel:
            print("[*] Closing tunnel...")
            tunnel.terminate()


if __name__ == "__main__":
    main()
