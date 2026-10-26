# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Near-miss parameter repair: cases taken from real agent mistakes in benchmark v5."""

from __future__ import annotations

from typing import Any

import pytest

from davinci_mcp.actions.registry import ActionRegistry, ToolDefinition, shape
from davinci_mcp.actions.repair import repair, unknown_keys
from davinci_mcp.domains import all_actions

UUID = "41a00d49-9924-4aad-a55a-076452edac6f"


@pytest.fixture(scope="module")
def tools() -> dict[str, ToolDefinition]:
    registry = ActionRegistry()
    registry.extend(all_actions())
    return {d.name: d for d in registry.tool_definitions()}


def _fix(tools: dict[str, ToolDefinition], tool: str, op: dict[str, Any]) -> tuple[Any, list[str]]:
    notes: list[str] = []
    fixed = repair(tools[tool].op_models[op["action"]], op, notes)
    call = tools[tool].call_adapter.validate_python({"op": fixed})
    return call.op, notes


@pytest.mark.parametrize(
    ("tool", "op", "field", "expected"),
    [
        (
            "item_write",
            {"action": "set_fades", "items": [{"id": UUID}], "fades": {"out": 50}},
            "fades",
            {"FadeOut": 50},
        ),
        (
            "item_write",
            {"action": "set_fades", "items": [{"id": UUID}], "fades": {"fade_out": 50}},
            "fades",
            {"FadeOut": 50},
        ),
        (
            "audio_write",
            {"action": "set_audio_properties", "item": UUID, "properties": {"Volume": -8}},
            "properties",
            {"AudioVolume": -8.0},
        ),
        (
            "gallery_write",
            {"action": "rename_album", "album": "Stills 2", "new_name": "Client Review"},
            "album",
            {"name": "Stills 2"},
        ),
    ],
)
def test_benchmark_near_misses_are_repaired(
    tools: dict[str, ToolDefinition], tool: str, op: dict[str, Any], field: str, expected: Any
) -> None:
    fixed, notes = _fix(tools, tool, op)
    value = getattr(fixed, field).model_dump(exclude_none=True, exclude_defaults=True)
    assert expected.items() <= value.items()
    assert notes  # every rewrite is reported


def test_unclear_keys_are_left_for_validation(tools: dict[str, ToolDefinition]) -> None:
    notes: list[str] = []
    op = {"action": "set_fades", "items": [{"id": UUID}], "fades": {"x": 1}}
    fixed = repair(tools["item_write"].op_models["set_fades"], op, notes)
    assert fixed["fades"] == {"x": 1} and not notes


def test_shape_lists_nested_fields(tools: dict[str, ToolDefinition]) -> None:
    hint = shape(tools["item_write"].op_models["set_fades"])
    assert "fades*: {FadeIn, FadeOut}" in hint
    assert "items*: [{" in hint


def test_bare_id_wraps_into_the_first_declared_target(tools: dict[str, ToolDefinition]) -> None:
    fixed, notes = _fix(tools, "color_read", {"action": "get_node_graph", "target": {"id": UUID}})
    assert fixed.target.item.id == UUID and notes
    notes = []
    op = {"action": "get_node_graph", "target": {"name": "Shot"}}
    assert repair(tools["color_read"].op_models["get_node_graph"], op, notes) == op
    assert not notes  # a name could be a clip or a timeline: left for validation


def test_unknown_keys_get_suggestions(tools: dict[str, ToolDefinition]) -> None:
    lines = unknown_keys(
        tools["item_write"].op_models["set_fades"],
        {"action": "set_fades", "items": [], "fades": {"FadeOutt": 1, "zzz": 2}},
    )
    assert "op.fades.FadeOutt: unknown, did you mean FadeOut" in lines[0]
    assert lines[1].startswith("op.fades.zzz: unknown, expected FadeIn, FadeOut")
