# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Command line: ``davinci-mcp serve | doctor``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from davinci_mcp.config import load_settings
from davinci_mcp.errors import DavinciError


def _serve(transport: str) -> int:
    from davinci_mcp.server import build_server, check_http_exposure  # noqa: PLC0415

    settings = load_settings()
    server = build_server(settings)
    if transport == "http":
        check_http_exposure(settings)
        server.run(
            transport="http", host=settings.http.host, port=settings.http.port, show_banner=False
        )
    else:
        server.run(transport="stdio", show_banner=False)
    return 0


async def _doctor() -> int:
    from davinci_mcp.server import Services  # noqa: PLC0415

    services = Services.create(load_settings())
    try:
        session = await services.connection.session()
        report = {"ok": True, **session.caps.info().model_dump(mode="json")}
    except DavinciError as err:
        report = {"ok": False, "error": err.to_payload().model_dump(mode="json")}
    finally:
        await services.aclose()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="davinci-mcp")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run the MCP server (default).")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    sub.add_parser("doctor", help="Check the connection to DaVinci Resolve.")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return asyncio.run(_doctor())
    return _serve(getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    sys.exit(main())
