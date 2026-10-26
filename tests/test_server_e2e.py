# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""End-to-end tests through a real MCP client against the fake Resolve."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastmcp import Client

from davinci_mcp.config import Settings
from davinci_mcp.connection.backend import Backend
from davinci_mcp.server import build_server
from tests.conftest import RESOLVE_ARGV, FakeBackend
from tests.fakes.fake_resolve import FakeResolve, make_resolve


@pytest.fixture
def resolve() -> FakeResolve:
    return make_resolve(timelines=2)


@pytest.fixture
async def client(resolve: FakeResolve, settings: Settings):
    server = build_server(
        settings,
        backends=[cast("Backend", FakeBackend(resolve))],
        process_probe=lambda: list(RESOLVE_ARGV),
    )
    async with Client(server) as c:
        yield c


def _project(resolve: FakeResolve) -> Any:
    project = resolve.GetProjectManager().GetCurrentProject()
    assert project is not None
    return project


def _timeline(resolve: FakeResolve) -> Any:
    return _project(resolve).GetCurrentTimeline()


async def _call(client: Client, tool: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    result = await client.call_tool(tool, args, raise_on_error=False)
    return result.is_error, result.structured_content or {}


async def test_tool_list_is_deterministic_and_annotated(client: Client) -> None:
    tools = {t.name: t for t in await client.list_tools()}
    registry = [n for n in tools if n.endswith(("_read", "_write", "_delete"))]
    assert registry == sorted(registry)  # deterministic order (spec 2026-07-28)
    read, write, delete = (tools[f"timeline_{k}"].annotations for k in ("read", "write", "delete"))
    assert read is not None and write is not None and delete is not None
    assert read.read_only_hint is True
    assert delete.destructive_hint is True
    assert write.read_only_hint is False
    for tool in tools.values():
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "oneOf" not in schema and "anyOf" not in schema  # Claude rejects top-level unions
    assert (tools["resolve_status"].meta or {}).get("anthropic/alwaysLoad") is True


async def test_status_and_list(client: Client, resolve: FakeResolve) -> None:
    err, status = await _call(client, "resolve_status", {})
    assert not err and status["connected"] and status["project"] == "Test Project"
    err, listed = await _call(client, "timeline_read", {"op": {"action": "list_timelines"}})
    assert not err
    timelines = listed["result"]["timelines"]
    assert [t["index"] for t in timelines] == [1, 2]
    assert listed["result"]["current_id"] == timelines[0]["id"]


async def test_add_marker_receipt_and_audit(
    client: Client, resolve: FakeResolve, settings: Settings
) -> None:
    op = {"action": "add_marker", "frame": 10, "color": "Green", "name": "beat"}
    err, out = await _call(client, "timeline_write", {"op": op})
    assert not err and out["status"] == "applied"
    receipt = out["receipt"]
    assert receipt["verified"] is True and receipt["result"]["frame"] == 10
    assert 10 in _timeline(resolve).GetMarkers()
    audit = (settings.state_dir / "audit.jsonl").read_text().splitlines()
    assert json.loads(audit[-1])["action"] == "timeline.add_marker"


async def test_dry_run_does_not_apply(client: Client, resolve: FakeResolve) -> None:
    op = {"action": "add_marker", "frame": 5}
    err, out = await _call(client, "timeline_write", {"op": op, "dry_run": True})
    assert not err and out["status"] == "planned"
    assert 5 not in _timeline(resolve).GetMarkers()


async def test_verification_failure_is_reported(
    client: Client, resolve: FakeResolve, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeline = _timeline(resolve)
    monkeypatch.setattr(timeline, "AddMarker", lambda *a, **k: True)  # lies, like Resolve can
    err, out = await _call(client, "timeline_write", {"op": {"action": "add_marker", "frame": 3}})
    assert not err
    assert out["receipt"]["verified"] is False
    assert any("VERIFICATION_FAILED" in w for w in out["receipt"]["warnings"])


async def test_destructive_requires_consent(client: Client, resolve: FakeResolve) -> None:
    timeline = _timeline(resolve)
    timeline.AddMarker(20, "Red", "cut", "", 1, "")
    call = {"op": {"action": "delete_marker", "frame": 20}}

    err, planned = await _call(client, "timeline_delete", call)
    assert not err and planned["status"] == "planned"
    assert 20 in timeline.GetMarkers()
    token = planned["plan"]["token"]

    err, _ = await _call(
        client,
        "timeline_delete",
        {"op": {"action": "delete_marker", "frame": 21}, "confirm_token": token},
    )
    assert err  # different params → invalid (or not found before consent)

    err, done = await _call(client, "timeline_delete", {**call, "confirm_token": token})
    assert not err and done["status"] == "applied" and done["receipt"]["verified"] is True
    assert 20 not in timeline.GetMarkers()


async def test_stale_consent(client: Client, resolve: FakeResolve) -> None:
    timeline = _timeline(resolve)
    timeline.AddMarker(30, "Red", "a", "", 1, "")
    call = {"op": {"action": "delete_marker", "frame": 30}}
    _, planned = await _call(client, "timeline_delete", call)
    timeline.DeleteMarkerAtFrame(30)
    timeline.AddMarker(30, "Blue", "changed", "", 1, "")  # state changed since the plan
    err, out = await _call(
        client, "timeline_delete", {**call, "confirm_token": planned["plan"]["token"]}
    )
    assert err and out["code"] == "CONFIRMATION_STALE"


async def test_invalid_params(client: Client) -> None:
    err, out = await _call(
        client, "timeline_write", {"op": {"action": "add_marker", "frame": 1, "bogus": 1}}
    )
    assert err and out["code"] == "INVALID_PARAMS"
    err, out = await _call(client, "timeline_read", {"op": {"action": "missing"}})
    assert err and "list_markers" in out["hint"]


async def test_snapshot_timeline_and_list(client: Client, resolve: FakeResolve) -> None:
    err, out = await _call(
        client, "snapshot_write", {"op": {"action": "snapshot_timeline", "label": "before-edit"}}
    )
    assert not err and out["status"] == "applied"
    err, listed = await _call(client, "snapshot_list", {})
    assert not err and listed["snapshots"][0]["kind"] == "timeline"
    assert _project(resolve).GetTimelineCount() == 3


async def test_status_when_unreachable(settings: Settings) -> None:
    server = build_server(
        settings,
        backends=[cast("Backend", FakeBackend(None))],
        process_probe=lambda: list(RESOLVE_ARGV),
    )
    async with Client(server) as c:
        content = (await c.call_tool("resolve_status", {})).structured_content
        assert content is not None
        assert content["connected"] is False
        assert content["error"]["code"] == "NOT_CONNECTED"
