# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the item domain over the fake Resolve."""

from __future__ import annotations

from typing import Any

from davinci_mcp.config import Settings
from davinci_mcp.domains.item import ACTIONS
from tests.fakes.item import first_items, make_item_resolve
from tests.helpers import call, confirm, mcp_client

POS1 = {"track": {"type": "video", "index": 1}, "position": 1}
POS2 = {"track": {"type": "video", "index": 1}, "position": 2}


async def _op(
    settings: Settings, resolve: Any, tool: str, op: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    async with mcp_client(resolve, settings, ACTIONS) as client:
        return await call(client, tool, {"op": op})


def _verified(out: dict[str, Any]) -> bool | None:
    return out["receipt"].get("verified")


async def test_get_item_and_reads(settings: Settings) -> None:
    resolve = make_item_resolve()
    err, out = await _op(settings, resolve, "item_read", {"action": "get_item", "item": POS1})
    assert not err
    data = out.get("result", out)
    assert data["name"] == "item_0_0"
    assert data["type"] == "video"
    assert data["track_index"] == 1
    assert data["left_offset"] == 10
    for name in ("get_speed", "get_fades", "get_takes", "list_item_markers", "get_output_blanking"):
        err, _ = await _op(settings, resolve, "item_read", {"action": name, "item": POS1})
        assert not err, name


async def test_get_properties_reports_constant_names(settings: Settings) -> None:
    resolve = make_item_resolve()
    first_items(resolve)[0]._props["CompositeMode"] = resolve.COMPOSITE_ADD
    err, out = await _op(settings, resolve, "item_read", {"action": "get_properties", "item": POS1})
    assert not err
    assert out.get("result", out)["properties"]["CompositeMode"] == "COMPOSITE_ADD"


async def test_item_not_found(settings: Settings) -> None:
    err, _ = await _op(
        settings,
        make_item_resolve(),
        "item_read",
        {"action": "get_item", "item": {"track": {"type": "video", "index": 1}, "position": 9}},
    )
    assert err


async def test_set_properties_batch_with_constants(settings: Settings) -> None:
    resolve = make_item_resolve()
    op = {
        "action": "set_properties",
        "items": [POS1, POS2],
        "properties": {"Opacity": 50, "CompositeMode": "COMPOSITE_ADD"},
    }
    err, out = await _op(settings, resolve, "item_write", op)
    assert not err
    assert _verified(out) is True
    for item in first_items(resolve)[:2]:
        assert item._props["Opacity"] == 50
        assert item._props["CompositeMode"] == resolve.COMPOSITE_ADD
    assert out["receipt"]["result"]["ok_count"] == 2


async def test_set_properties_unverified_when_resolve_lies(settings: Settings) -> None:
    resolve = make_item_resolve()
    first_items(resolve)[1].lies.add("SetProperties")
    op = {"action": "set_properties", "items": [POS1, POS2], "properties": {"Opacity": 10}}
    err, out = await _op(settings, resolve, "item_write", op)
    assert not err
    assert _verified(out) is False
    assert out["receipt"]["result"]["failed_count"] == 1


async def test_set_properties_rejects_unknown_key_and_empty(settings: Settings) -> None:
    resolve = make_item_resolve()
    for props in ({"Bogus": 1}, {}):
        op = {"action": "set_properties", "items": [POS1], "properties": props}
        err, _ = await _op(settings, resolve, "item_write", op)
        assert err


async def test_copy_properties_takes_no_auto_snapshot(settings: Settings) -> None:
    resolve = make_item_resolve()
    first_items(resolve)[0]._props["Opacity"] = 33.0
    op = {"action": "copy_properties", "source": POS1, "targets": [POS2], "keys": ["Opacity"]}
    err, out = await _op(settings, resolve, "item_write", op)
    assert not err
    assert _verified(out) is True
    assert first_items(resolve)[1]._props["Opacity"] == 33.0
    assert "snapshot_id" not in out["receipt"]  # writes take no auto snapshot by default
    bad = {**op, "keys": ["Nope"]}
    err, _ = await _op(settings, resolve, "item_write", bad)
    assert err


async def test_set_speed_and_lie(settings: Settings) -> None:
    resolve = make_item_resolve()
    op = {"action": "set_speed", "items": [POS1], "speed": {"Percentage": 200}}
    err, out = await _op(settings, resolve, "item_write", op)
    assert not err
    assert _verified(out) is True
    assert first_items(resolve)[0].speed["Percentage"] == 200
    first_items(resolve)[1].lies.add("SetSpeed")
    err, out = await _op(settings, resolve, "item_write", {**op, "items": [POS2]})
    assert _verified(out) is False


async def test_set_fades(settings: Settings) -> None:
    resolve = make_item_resolve()
    op = {"action": "set_fades", "items": [POS1], "fades": {"FadeIn": 12}}
    err, out = await _op(settings, resolve, "item_write", op)
    assert not err
    assert _verified(out) is True
    first_items(resolve)[1].lies.add("SetFades")
    _, out = await _op(settings, resolve, "item_write", {**op, "items": [POS2]})
    assert _verified(out) is False


async def test_near_miss_params_are_repaired_and_reported(settings: Settings) -> None:
    resolve = make_item_resolve()
    op = {"action": "set_fades", "items": [POS1], "fades": {"fade_out": 12}}
    err, out = await _op(settings, resolve, "item_write", op)
    assert not err and _verified(out) is True
    assert out["notes"] == ["op.fades: read 'fade_out' as 'FadeOut'"]
    err, out = await _op(settings, resolve, "item_write", {**op, "fades": {"x": 1}})
    assert err and out["code"] == "INVALID_PARAMS"
    assert "fades*: {FadeIn, FadeOut}" in out["hint"]


async def test_add_transition(settings: Settings) -> None:
    resolve = make_item_resolve()
    op = {
        "action": "add_transition",
        "item": POS1,
        "options": {"type": "Cross Dissolve", "position": "end"},
    }
    err, out = await _op(settings, resolve, "item_write", op)
    assert not err
    assert _verified(out) is True
    assert len(first_items(resolve)) == 4
    # Returned item not listed in the timeline: verification fails.
    first_items(resolve)[1].lies.add("AddTransition")
    _, out = await _op(settings, resolve, "item_write", {**op, "item": POS2})
    assert _verified(out) is False
    # No handles: None returned.
    first_items(resolve)[2].no_handles = True
    err, _ = await _op(
        settings, resolve, "item_write", {**op, "item": {"track": POS1["track"], "position": 3}}
    )
    assert err


async def test_enable_color_flags_name(settings: Settings) -> None:
    resolve = make_item_resolve()
    items = [POS1, POS2]
    err, out = await _op(
        settings, resolve, "item_write", {"action": "set_enabled", "items": items, "enabled": False}
    )
    assert not err
    assert _verified(out) is True
    assert not first_items(resolve)[0].enabled

    _, out = await _op(
        settings,
        resolve,
        "item_write",
        {"action": "set_clip_color", "items": items, "color": "Blue"},
    )
    assert _verified(out) is True
    _, out = await _op(
        settings, resolve, "item_write", {"action": "set_clip_color", "items": items}
    )
    assert _verified(out) is True
    assert first_items(resolve)[0].color == ""

    _, out = await _op(
        settings, resolve, "item_write", {"action": "add_flag", "items": items, "color": "Red"}
    )
    assert _verified(out) is True
    _, out = await _op(settings, resolve, "item_write", {"action": "clear_flags", "items": items})
    assert _verified(out) is True
    assert first_items(resolve)[0].flags == []

    err, out = await _op(
        settings, resolve, "item_write", {"action": "set_name", "item": POS1, "name": "renamed"}
    )
    assert not err
    assert _verified(out) is True
    assert first_items(resolve)[0]._name == "renamed"


async def test_state_writes_flag_lies(settings: Settings) -> None:
    resolve = make_item_resolve()
    item = first_items(resolve)[0]
    item.lies.update({"SetClipEnabled", "AddFlag", "SetName"})
    for op in (
        {"action": "set_enabled", "items": [POS1], "enabled": False},
        {"action": "add_flag", "items": [POS1], "color": "Red"},
        {"action": "set_name", "item": POS1, "name": "zzz"},
    ):
        _, out = await _op(settings, resolve, "item_write", op)
        assert _verified(out) is False, op["action"]


async def test_stabilize(settings: Settings) -> None:
    resolve = make_item_resolve()
    err, out = await _op(settings, resolve, "item_write", {"action": "stabilize", "items": [POS1]})
    assert not err
    assert _verified(out) is None


async def test_item_markers_lifecycle(settings: Settings) -> None:
    resolve = make_item_resolve()
    add = {"action": "add_item_marker", "item": POS1, "frame": 5, "color": "Green", "name": "m"}
    err, out = await _op(settings, resolve, "item_write", add)
    assert not err
    assert _verified(out) is True
    err, _ = await _op(settings, resolve, "item_write", add)  # duplicate frame
    assert err
    upd = {"action": "update_marker_custom_data", "item": POS1, "frame": 5, "custom_data": "x"}
    err, out = await _op(settings, resolve, "item_write", upd)
    assert not err
    assert _verified(out) is True
    err, _ = await _op(settings, resolve, "item_write", {**upd, "frame": 99})
    assert err
    _, listed = await _op(
        settings, resolve, "item_read", {"action": "list_item_markers", "item": POS1}
    )
    assert listed.get("result", listed)["markers"][0]["custom_data"] == "x"


async def test_delete_item_markers(settings: Settings) -> None:
    resolve = make_item_resolve()
    item = first_items(resolve)[0]
    for frame, color in ((1, "Red"), (2, "Red"), (3, "Blue")):
        item.AddMarker(frame, color, "n", "", 1, f"d{frame}")
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await confirm(
            client,
            "item_delete",
            {"op": {"action": "delete_item_markers", "item": POS1, "color": "Red"}},
        )
        assert not err
        assert _verified(out) is True
        assert list(item.markers) == [3]
        err, _ = await confirm(
            client,
            "item_delete",
            {"op": {"action": "delete_item_markers", "item": POS1, "custom_data": "d3"}},
        )
        assert not err
        assert item.markers == {}
        err, _ = await call(
            client,
            "item_delete",
            {"op": {"action": "delete_item_markers", "item": POS1, "frame": 1, "color": "Red"}},
        )
        assert err  # exactly one selector
        err, _ = await call(
            client,
            "item_delete",
            {"op": {"action": "delete_item_markers", "item": POS1, "frame": 7}},
        )
        assert err  # nothing to delete


async def test_takes(settings: Settings) -> None:
    resolve = make_item_resolve()
    item = first_items(resolve)[0]
    add = {"action": "add_take", "item": POS1, "clip": {"name": "take.mov"}}
    err, out = await _op(settings, resolve, "item_write", add)
    assert not err, out
    assert _verified(out) is True
    await _op(settings, resolve, "item_write", add)
    assert len(item.takes) == 2
    err, out = await _op(
        settings, resolve, "item_write", {"action": "select_take", "item": POS1, "index": 2}
    )
    assert _verified(out) is True
    err, _ = await _op(
        settings, resolve, "item_write", {"action": "select_take", "item": POS1, "index": 5}
    )
    assert err
    _, got = await _op(settings, resolve, "item_read", {"action": "get_takes", "item": POS1})
    assert got.get("result", got)["selected_index"] == 2

    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await confirm(
            client, "item_delete", {"op": {"action": "delete_take", "item": POS1, "index": 1}}
        )
        assert not err
        assert _verified(out) is True
        assert len(item.takes) == 1

    err, out = await _op(
        settings, resolve, "item_write", {"action": "finalize_takes", "item": POS1}
    )
    assert not err
    err, _ = await _op(settings, resolve, "item_write", {"action": "finalize_takes", "item": POS1})
    assert err  # no longer a take selector

    item.lies.add("AddTake")
    _, out = await _op(settings, resolve, "item_write", {**add, "item": POS1})
    assert _verified(out) is False


async def test_take_clip_not_found(settings: Settings) -> None:
    err, _ = await _op(
        settings,
        make_item_resolve(),
        "item_write",
        {"action": "add_take", "item": POS1, "clip": {"name": "missing.mov"}},
    )
    assert err


async def test_output_blanking(settings: Settings) -> None:
    resolve = make_item_resolve()
    set_op = {"action": "set_output_blanking", "item": POS1, "blanking": {"Top": 20, "Bottom": 20}}
    # Timeline blanking still in use: reads back empty, so verification fails.
    _, out = await _op(settings, resolve, "item_write", set_op)
    assert _verified(out) is False
    err, out = await _op(
        settings,
        resolve,
        "item_write",
        {"action": "set_use_timeline_blanking", "item": POS1, "use_timeline": False},
    )
    assert not err
    assert _verified(out) is True
    _, out = await _op(settings, resolve, "item_write", set_op)
    assert _verified(out) is True
    _, got = await _op(
        settings, resolve, "item_read", {"action": "get_output_blanking", "item": POS1}
    )
    assert got.get("result", got)["blanking"]["Top"] == 20
    err, _ = await _op(
        settings,
        resolve,
        "item_write",
        {"action": "set_output_blanking", "item": POS1, "blanking": {}},
    )
    assert err


async def test_multicam(settings: Settings) -> None:
    resolve = make_item_resolve()
    item = first_items(resolve)[0]
    err, out = await _op(
        settings,
        resolve,
        "item_write",
        {
            "action": "flatten_multicam",
            "item": POS1,
            "grade": "FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE",
        },
    )
    assert not err
    assert item.multicam_calls[0] == (
        "flatten",
        resolve.FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE,
    )
    assert "snapshot_id" not in out["receipt"]  # writes take no auto snapshot by default
    err, _ = await _op(
        settings,
        resolve,
        "item_write",
        {
            "action": "smart_switch",
            "item": POS1,
            "settings": {
                "minEditDuration": 2.0,
                "quality": "SMART_SWITCH_QUALITY_BETTER",
                "analysisMode": "SMART_SWITCH_ANALYSIS_MODE_AUDIO_ONLY",
            },
        },
    )
    assert not err
    kind, sent = item.multicam_calls[1]
    assert kind == "smart_switch"
    assert sent["quality"] == resolve.SMART_SWITCH_QUALITY_BETTER
    assert sent["minEditDuration"] == 2.0
