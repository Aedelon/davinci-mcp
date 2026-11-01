# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Command line: ``davinci-mcp serve | doctor | install | uninstall``."""

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


def _install(args: argparse.Namespace, *, remove: bool) -> int:
    from davinci_mcp.install import TARGETS, install  # noqa: PLC0415

    targets = TARGETS if args.target == "all" else (args.target,)
    reports = install(targets, name=args.name, dry_run=args.dry_run, remove=remove)
    if args.bridge and not remove and not args.dry_run:
        from davinci_mcp.bridge.install import install_bridge_script  # noqa: PLC0415

        print(f"bridge script: {install_bridge_script()}")
    for r in reports:
        print(f"{r.target:15} {r.status:14} {r.detail}")
    return 1 if any(r.status == "failed" for r in reports) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="davinci-mcp")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run the MCP server (default).")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    sub.add_parser("doctor", help="Check the connection to DaVinci Resolve.")
    for command, helptext in (
        ("install", "Register the server in Claude Code and Claude Desktop."),
        ("uninstall", "Remove the server from Claude Code and Claude Desktop."),
    ):
        p = sub.add_parser(command, help=helptext)
        p.add_argument("--target", choices=("all", "claude-code", "claude-desktop"), default="all")
        p.add_argument("--name", default="davinci-mcp", help="Server name in the clients.")
        p.add_argument("--dry-run", action="store_true")
        if command == "install":
            p.add_argument(
                "--bridge",
                action="store_true",
                help="Also install the in-app bridge script (free edition up to 21.0).",
            )
        else:
            p.set_defaults(bridge=False)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return asyncio.run(_doctor())
    if args.command in {"install", "uninstall"}:
        return _install(args, remove=args.command == "uninstall")
    return _serve(getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    sys.exit(main())
