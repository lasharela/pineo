"""Run the Pineo hub: `.venv/bin/python -m hub` from pineo/ (or hub/run.sh)."""
from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

from aiohttp import web

from .server import create_app


def lan_addresses() -> list[str]:
    found: list[str] = []
    try:  # every interface (Wi-Fi, Tailscale, ...)
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        found += re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    except Exception:
        pass
    try:  # the default-route address, in case ifconfig is unavailable
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            found.append(sock.getsockname()[0])
    except Exception:
        pass
    return [ip for ip in dict.fromkeys(found) if not ip.startswith(("127.", "169.254."))]


def main() -> None:
    parser = argparse.ArgumentParser(description="Pineo hub")
    parser.add_argument("--host", default=os.environ.get("PINEO_HUB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PINEO_HUB_PORT", "8787")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    data_dir = os.environ.get("PINEO_HUB_DATA")  # default: hub/data
    app = create_app(data_dir=Path(data_dir) if data_dir else None)
    print(f"Pineo hub on port {args.port}")
    print(f"  Dashboard (this laptop): http://localhost:{args.port}/dashboard/")
    addresses = lan_addresses()
    for ip in addresses:
        print(f"  Phone app (same network): http://{ip}:{args.port}/")
    if not addresses:
        print("  No LAN address found: connect the laptop to a network or use a tunnel.")
    sys.stdout.flush()
    try:
        web.run_app(app, host=args.host, port=args.port, print=None)
    except OSError as exc:
        sys.exit(f"Could not start the hub on port {args.port}: {exc}. Is another hub already running?")


if __name__ == "__main__":
    main()
