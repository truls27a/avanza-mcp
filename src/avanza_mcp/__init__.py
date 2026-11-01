"""Avanza MCP server: read-only Avanza account and market data, with BankID login."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "avanza-mcp"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="avanza-mcp", description=__doc__)
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                   help="stdio for local clients (default), http for a streamable HTTP endpoint at /mcp")
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    p.add_argument("--data-dir", default=os.environ.get("AVANZA_MCP_DATA_DIR") or str(default_data_dir()),
                   help="where the session and snapshot are stored (env AVANZA_MCP_DATA_DIR)")
    p.add_argument("--allow-unauthenticated", action="store_true",
                   help="allow HTTP on a non-loopback address without AVANZA_MCP_TOKEN, e.g. on a private "
                        "container network behind a tunnel. Anyone who can reach the port can read your "
                        "Avanza data.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("AVANZA_MCP_TOKEN") or None
    if args.transport == "http" and not token and not _is_loopback(args.host) and not args.allow_unauthenticated:
        p.error(f"refusing to serve {args.host} without authentication: set AVANZA_MCP_TOKEN, "
                "bind to 127.0.0.1, or pass --allow-unauthenticated")

    from .server import create_server

    mcp = create_server(args.data_dir, token=token if args.transport == "http" else None)
    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(transport="http", host=args.host, port=args.port, path="/mcp", show_banner=False)
