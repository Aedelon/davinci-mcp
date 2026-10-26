# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Compact schema mode: token budget guard, describe_action and error schemas."""

from __future__ import annotations

import json

from davinci_mcp.config import Settings
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, mcp_client

# ~4 chars per token. Raise deliberately (and say why in the commit) when domains grow.
# 72k: workflow_write ships its full schema (always loaded) and base tools list shortcuts.
MAX_INPUT_CHARS = 72_000
# 22k: batch_run returns the RecipeRun schema (per-step outcomes).
MAX_OUTPUT_CHARS = 22_000


async def test_tool_list_stays_within_budget(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings) as client:
        tools = await client.list_tools()
        inputs = sum(len(json.dumps(t.input_schema)) + len(t.description or "") for t in tools)
        outputs = sum(len(json.dumps(t.output_schema or {})) for t in tools)
    assert inputs < MAX_INPUT_CHARS, inputs
    assert outputs < MAX_OUTPUT_CHARS, outputs


async def test_describe_action_returns_full_schema(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings) as client:
        err, out = await call(
            client, "describe_action", {"tool": "timeline_write", "action": "add_marker"}
        )
        assert not err
        assert "frame" in out["params_schema"]["properties"]
        assert out["kind"] == "write"
        err, out = await call(client, "describe_action", {"tool": "timeline_write", "action": "x"})
        assert err and out["code"] == "NOT_FOUND"


async def test_invalid_params_carry_the_action_schema(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings) as client:
        err, out = await call(
            client, "timeline_write", {"op": {"action": "add_marker", "frame": "not-a-number"}}
        )
        assert err and out["code"] == "INVALID_PARAMS"
        assert "frame*" in out["hint"]
        assert "properties" in out["details"]["schema"]


async def test_full_mode_exposes_discriminated_unions(settings: Settings) -> None:
    full = settings.model_copy(
        update={"server": settings.server.model_copy(update={"schema_mode": "full"})}
    )
    async with mcp_client(make_resolve(), full) as client:
        tools = {t.name: t for t in await client.list_tools()}
        op = tools["timeline_write"].input_schema["properties"]["op"]
        assert "oneOf" in op or "discriminator" in op or "$ref" in op


async def test_composites_are_featured_and_advertised(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings) as client:
        tools = {t.name: t for t in await client.list_tools()}
    workflow = tools["workflow_write"]
    assert (workflow.meta or {}).get("anthropic/alwaysLoad") is True
    assert "oneOf" in json.dumps(workflow.input_schema)  # full schema, no describe_action needed
    assert not (tools["render_write"].meta or {}).get("anthropic/alwaysLoad")
    assert "workflow_write.deliver" in (tools["render_write"].description or "")
    assert "workflow_write.conform_from_edl" in (tools["media_write"].description or "")
