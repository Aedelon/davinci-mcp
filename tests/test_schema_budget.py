# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Compact schema mode: token budget guard, describe_action and error schemas."""

from __future__ import annotations

import json

from davinci_mcp.config import Settings
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, mcp_client

# ~4 chars per token. Raise deliberately (and say why in the commit) when domains grow.
# 76k: workflow_write ships its full schema; base tools list shortcuts and the batch_run
# hint. Tools are deferred by clients, so this is paid per loaded tool, not per turn.
MAX_INPUT_CHARS = 76_000
# 22k: batch_run returns the RecipeRun schema (per-step outcomes).
MAX_OUTPUT_CHARS = 22_000


async def test_tool_list_stays_within_budget(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings) as client:
        tools = await client.list_tools()
        inputs = sum(len(json.dumps(t.input_schema)) + len(t.description or "") for t in tools)
        outputs = sum(len(json.dumps(t.output_schema or {})) for t in tools)
    assert inputs < MAX_INPUT_CHARS, inputs
    assert outputs < MAX_OUTPUT_CHARS, outputs


async def test_describe_action_returns_full_schemas(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings) as client:
        err, out = await call(
            client, "describe_action", {"tool": "timeline_write", "action": "add_marker"}
        )
        assert not err
        info = out["actions"][0]
        assert "frame" in info["params_schema"]["properties"] and info["kind"] == "write"
        err, out = await call(
            client,
            "describe_action",
            {"actions": ["timeline_write.add_marker", "color_write.set_cdl"]},
        )
        assert not err and [a["action"] for a in out["actions"]] == ["add_marker", "set_cdl"]
        text = json.dumps(out)
        assert '"title"' not in text and "$defs" not in text and "result_schema" not in text
        err, out = await call(client, "describe_action", {"tool": "gallery_write"})
        assert not err and any(a.startswith("grab_still(") for a in out["actions"])
        err, out = await call(client, "describe_action", {"tool": "timeline_write", "action": "x"})
        assert err and out["code"] == "NOT_FOUND"
        err, out = await call(client, "describe_action", {})
        assert err and out["code"] == "INVALID_PARAMS"


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
    assert "oneOf" in json.dumps(workflow.input_schema)  # full schema, no describe_action needed
    # No tool is always-loaded: it would stop Claude Code from deferring the whole server.
    assert not any((t.meta or {}).get("anthropic/alwaysLoad") for t in tools.values())
    assert "workflow_write.deliver" in (tools["render_write"].description or "")
    assert "workflow_write.conform_from_edl" in (tools["media_write"].description or "")


async def test_action_called_on_the_wrong_tool_is_redirected(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings) as client:
        op = {"action": "set_audio_properties", "item": "x", "properties": {"AudioVolume": -8}}
        err, out = await call(client, "item_write", {"op": op})
    assert err and "set_audio_properties is an action of audio_write" in out["hint"]
