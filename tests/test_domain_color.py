# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the color domain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.config import Settings
from davinci_mcp.domains import color
from davinci_mcp.domains.color import ACTIONS
from tests.fakes.color import install_color
from tests.fakes.fake_resolve import FakeResolve, make_resolve
from tests.helpers import call, confirm, mcp_client

ITEM = {"track": {"type": "video", "index": 1}, "position": 1}
ITEM2 = {"track": {"type": "video", "index": 1}, "position": 2}
TARGET = {"item": ITEM}


def _setup() -> tuple[FakeResolve, dict[str, Any]]:
    resolve = make_resolve()
    return resolve, install_color(resolve)


def _graph(state: dict[str, Any], position: int = 0) -> Any:
    tl = state["project"].timelines[0]
    return state["graphs"][tl.tracks[0][position]._uid]


async def test_get_node_graph(settings: Settings) -> None:
    resolve, state = _setup()
    _graph(state).luts[1] = "a.cube"
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(
            c, "color_read", {"op": {"action": "get_node_graph", "target": TARGET}}
        )
        assert not err and out["result"]["node_count"] == 3
        assert out["result"]["nodes"][1]["lut"] == "a.cube"
        assert out["result"]["nodes"][0]["cache_mode"] == "disabled"
        err, out = await call(c, "color_read", {"op": {"action": "get_node_graph"}})
        assert not err and out["result"]["node_count"] == 2  # timeline graph
        both = {"action": "get_node_graph", "target": {"item": ITEM, "group": "x"}}
        err, _ = await call(c, "color_read", {"op": both})
        assert err


async def test_set_node_lut(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "LUT"
    monkeypatch.setattr(color, "lut_root", lambda: root)  # never touch Resolve's real folder
    resolve, state = _setup()
    lut = tmp_path / "look.cube"
    lut.write_text("x")
    op = {"action": "set_node_lut", "target": TARGET, "node": 2, "lut_path": str(lut)}
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(c, "color_write", {"op": op})
        assert not err and out["receipt"]["verified"] is True
        assert _graph(state).luts[1] == "MCP/look.cube"  # installed under the LUT root
        assert (root / "MCP" / "look.cube").read_text() == "x"
        _graph(state).lie = True
        err, out = await call(c, "color_write", {"op": {**op, "node": 1}})
        assert out["receipt"]["verified"] is False
        err, out = await call(c, "color_write", {"op": {**op, "node": 9}})
        assert err and out["code"] == "INVALID_PARAMS"
        outside = {**op, "lut_path": "/etc/passwd"}
        err, out = await call(c, "color_write", {"op": outside})
        assert err and out["code"] == "PATH_NOT_ALLOWED"


async def test_node_enabled_and_cache(settings: Settings) -> None:
    resolve, state = _setup()
    async with mcp_client(resolve, settings, ACTIONS) as c:
        op = {"action": "set_node_enabled", "target": TARGET, "node": 1, "enabled": False}
        err, out = await call(c, "color_write", {"op": op})
        assert not err and out["receipt"].get("verified") is None
        assert _graph(state).enabled[0] is False
        op = {"action": "set_node_cache_mode", "target": TARGET, "node": 3, "mode": "enabled"}
        err, out = await call(c, "color_write", {"op": op})
        assert not err and out["receipt"]["verified"] is True
        assert _graph(state).cache[2] == 1
        _graph(state).lie = True
        op["mode"] = "auto"
        err, out = await call(c, "color_write", {"op": op})
        assert out["receipt"]["verified"] is False


async def test_apply_drx_and_reset(settings: Settings, tmp_path: Path) -> None:
    resolve, state = _setup()
    drx = tmp_path / "g.drx"
    drx.write_text("x")
    op = {"action": "apply_grade_from_drx", "target": TARGET, "path": str(drx), "reset_first": True}
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(c, "color_write", {"op": op})
        assert not err and out["receipt"]["verified"] is True
        assert _graph(state).reset_calls == 1 and resolve.page == "edit"
        _graph(state).drx_ok = False
        err, out = await call(c, "color_write", {"op": op})
        assert err and out["code"] == "API_CALL_FAILED"
        missing = {**op, "path": str(tmp_path / "nope.drx")}
        err, out = await call(c, "color_write", {"op": missing})
        assert err and out["code"] == "NOT_FOUND"

        _graph(state).luts[0] = "a.cube"
        err, out = await confirm(
            c, "color_delete", {"op": {"action": "reset_all_grades", "target": TARGET}}
        )
        assert not err and out["receipt"]["verified"] is True
        assert _graph(state).luts == ["", "", ""]


async def test_cdl_and_node_colors(settings: Settings) -> None:
    resolve, state = _setup()
    item = state["project"].timelines[0].tracks[0][0]
    async with mcp_client(resolve, settings, ACTIONS) as c:
        op = {
            "action": "set_cdl",
            "item": ITEM,
            "node": 2,
            "slope": [1.1, 1, 0.9],
            "saturation": 0.8,
        }
        err, out = await call(c, "color_write", {"op": op})
        assert not err and out["receipt"].get("verified") is None
        assert item.cdl_calls[0]["Slope"] == "1.1 1 0.9" and item.cdl_calls[0]["NodeIndex"] == 2
        err, out = await call(c, "color_write", {"op": {**op, "node": 7}})
        assert err and out["code"] == "INVALID_PARAMS"
        err, _ = await call(c, "color_write", {"op": {"action": "reset_node_colors", "item": ITEM}})
        assert not err


async def test_versions(settings: Settings) -> None:
    resolve, state = _setup()
    item = state["project"].timelines[0].tracks[0][0]
    async with mcp_client(resolve, settings, ACTIONS) as c:
        add = {"action": "add_version", "item": ITEM, "name": "alt"}
        err, out = await call(c, "color_write", {"op": add})
        assert not err and out["receipt"]["verified"] is True
        assert resolve.page == "edit"  # page restored
        err, out = await call(c, "color_write", {"op": add})
        assert err and out["code"] == "WRONG_STATE"
        err, out = await call(c, "color_read", {"op": {"action": "list_versions", "item": ITEM}})
        assert out["result"]["local"] == ["Version 1", "alt"]
        assert out["result"]["current_name"] == "Version 1"
        load = {"action": "load_version", "item": ITEM, "name": "alt"}
        err, out = await call(c, "color_write", {"op": load})
        assert out["receipt"]["verified"] is True
        ren = {"action": "rename_version", "item": ITEM, "name": "alt", "new_name": "beta"}
        err, out = await call(c, "color_write", {"op": ren})
        assert out["receipt"]["verified"] is True
        err, out = await call(c, "color_write", {"op": {**load, "name": "ghost"}})
        assert err and out["code"] == "NOT_FOUND"
        delete = {"action": "delete_version", "item": ITEM, "name": "beta"}
        err, out = await confirm(c, "color_delete", {"op": delete})
        assert not err and out["receipt"]["verified"] is True
        item.lie_versions = True
        resolve.page = "color"
        item.AddVersion("again", 0)
        resolve.page = "edit"
        delete["name"] = "again"
        err, out = await confirm(c, "color_delete", {"op": delete})
        assert out["receipt"]["verified"] is False


async def test_export_lut(settings: Settings, tmp_path: Path) -> None:
    resolve, state = _setup()
    item = state["project"].timelines[0].tracks[0][0]
    out_path = tmp_path / "out.cube"
    op = {"action": "export_lut", "item": ITEM, "path": str(out_path), "lut_type": "65pt_cube"}
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(c, "color_write", {"op": op})
        assert not err and out["receipt"]["verified"] is True and out_path.exists()
        out_path.unlink()
        item.exported = False
        err, out = await call(c, "color_write", {"op": op})
        assert out["receipt"]["verified"] is False


async def test_copy_grades(settings: Settings) -> None:
    resolve, state = _setup()
    src = state["project"].timelines[0].tracks[0][0]
    op = {"action": "copy_grades", "source": ITEM, "targets": [ITEM2]}
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, planned = await call(c, "color_delete", {"op": op})
        assert not err and planned["status"] == "planned" and not src.copied_to
        err, out = await confirm(c, "color_delete", {"op": op})
        assert not err and out["status"] == "applied"
        assert len(src.copied_to) == 1


async def test_color_groups(settings: Settings) -> None:
    resolve, state = _setup()
    item = state["project"].timelines[0].tracks[0][0]
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(
            c, "color_write", {"op": {"action": "create_color_group", "name": "G"}}
        )
        assert not err and out["receipt"]["verified"] is True
        err, out = await call(
            c, "color_write", {"op": {"action": "create_color_group", "name": "G"}}
        )
        assert err and out["code"] == "WRONG_STATE"
        assign = {"action": "assign_to_color_group", "group": "G", "items": [ITEM]}
        err, out = await call(c, "color_write", {"op": assign})
        assert not err and out["receipt"]["verified"] is True
        err, out = await call(c, "color_read", {"op": {"action": "list_color_groups"}})
        assert out["result"]["groups"][0]["clip_ids"] == [item._uid]
        g_target = {"group": "G", "stage": "pre"}
        err, out = await call(
            c, "color_read", {"op": {"action": "get_node_graph", "target": g_target}}
        )
        assert not err and out["result"]["node_count"] == 1
        ren = {"action": "rename_color_group", "name": "G", "new_name": "H"}
        err, out = await call(c, "color_write", {"op": ren})
        assert out["receipt"]["verified"] is True
        rem = {"action": "remove_from_color_group", "items": [ITEM]}
        err, out = await call(c, "color_write", {"op": rem})
        assert out["receipt"]["verified"] is True
        err, out = await call(c, "color_write", {"op": {**assign, "group": "ghost"}})
        assert err and out["code"] == "NOT_FOUND"
        err, out = await confirm(
            c, "color_delete", {"op": {"action": "delete_color_group", "name": "H"}}
        )
        assert not err and out["receipt"]["verified"] is True
        assert state["groups"] == []


async def test_dctl(settings: Settings, tmp_path: Path) -> None:
    resolve, state = _setup()
    src = tmp_path / "a.dctl"
    src.write_text("// dctl")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(c, "color_read", {"op": {"action": "validate_dctl", "source": "x"}})
        assert not err and out["result"]["valid"] is True
        state["validate_error"] = "no return value"
        err, out = await call(
            c, "color_read", {"op": {"action": "validate_dctl", "path": str(src)}}
        )
        assert out["result"] == {"valid": False, "error": "no return value"}
        err, _ = await call(c, "color_read", {"op": {"action": "validate_dctl"}})
        assert err
        enc = {
            "action": "encrypt_dctl",
            "input_path": str(src),
            "output_folder": str(tmp_path / "o"),
        }
        err, out = await call(c, "color_write", {"op": enc})
        assert not err and out["receipt"]["verified"] is True
        assert out["receipt"]["result"]["files"] == ["a.dctl"]
        state["encrypt_ok"] = False
        err, out = await call(c, "color_write", {"op": {**enc, "name": "b.dctl"}})
        assert err and out["code"] == "API_CALL_FAILED"
