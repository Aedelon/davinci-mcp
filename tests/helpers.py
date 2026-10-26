# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Helpers for domain tests: an in-memory MCP client over the fake Resolve.

Usage in a domain test::

    async def test_x(settings):
        resolve = make_resolve()
        async with mcp_client(resolve, settings, ACTIONS) as client:
            err, out = await call(client, "media_read", {"op": {"action": "list_clips"}})
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

from fastmcp import Client

from davinci_mcp.actions.spec import ActionSpec
from davinci_mcp.config import Settings
from davinci_mcp.connection.backend import Backend
from davinci_mcp.server import build_server
from tests.conftest import RESOLVE_ARGV, FakeBackend


@asynccontextmanager
async def mcp_client(
    resolve: Any, settings: Settings, actions: Sequence[ActionSpec] | None = None
) -> AsyncGenerator[Client[Any]]:
    """Client over a server restricted to ``actions`` (all domains when None)."""
    server = build_server(
        settings,
        backends=[cast("Backend", FakeBackend(resolve))],
        process_probe=lambda: list(RESOLVE_ARGV),
        actions=actions,
    )
    async with Client(server) as client:
        yield client


async def call(client: Client[Any], tool: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Call a tool; return (is_error, structured_content). Output schemas are validated."""
    result = await client.call_tool(tool, args, raise_on_error=False)
    return result.is_error, result.structured_content or {}


async def confirm(
    client: Client[Any], tool: str, args: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Run a destructive call through its plan → confirm_token round trip."""
    err, planned = await call(client, tool, args)
    if err or planned.get("status") != "planned":
        return err, planned
    return await call(client, tool, {**args, "confirm_token": planned["plan"]["token"]})
