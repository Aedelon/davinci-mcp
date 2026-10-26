# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the app domain over the fake Resolve."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from davinci_mcp.domains.app import ACTIONS
from tests.fakes.project import AppResolve, make_project_resolve
from tests.helpers import call, confirm, mcp_client

if TYPE_CHECKING:
    from pathlib import Path

    from davinci_mcp.config import Settings


async def _w(r: AppResolve, settings: Settings, tool: str, op: dict[str, Any]):
    async with mcp_client(r, settings, ACTIONS) as client:
        return await call(client, tool, {"op": op})


async def test_info_and_presets(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "app_read", {"action": "get_app_info"})
    assert not err
    res = out["result"]
    assert res["is_studio"] is True and res["version"] == "21.1.1.10"
    assert res["current_page"] == "edit" and res["keyframe_mode"] == "all"
    err, out = await _w(r, settings, "app_read", {"action": "list_presets", "kind": "keyboard"})
    assert out["result"]["presets"] == ["Resolve"] and out["result"]["current"] == "Resolve"
    err, out = await _w(r, settings, "app_read", {"action": "list_presets", "kind": "fairlight"})
    assert out["result"]["presets"] == ["Fairlight A"]


async def test_open_page_and_keyframe(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "app_write", {"action": "open_page", "page": "color"})
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(r, settings, "app_write", {"action": "open_page", "page": "bogus"})
    assert err and out["code"] == "INVALID_PARAMS"
    err, out = await _w(r, settings, "app_write", {"action": "set_keyframe_mode", "mode": "sizing"})
    assert not err and out["receipt"]["verified"] is True and r.keyframe == 2


async def test_preset_write_ops(settings: Settings, tmp_path: Path) -> None:
    r = make_project_resolve()
    ops = [
        {"action": "save_preset", "kind": "layout", "name": "L2"},
        {"action": "update_preset", "kind": "layout", "name": "L2"},
        {"action": "load_preset", "kind": "layout", "name": "L2"},
        {"action": "load_preset", "kind": "keyboard", "name": "Resolve"},
        {"action": "save_preset", "kind": "user_preferences", "name": "U2"},
    ]
    for op in ops:
        err, out = await _w(r, settings, "app_write", op)
        assert not err, (op, out)
    dest = tmp_path / "l.preset"
    op = {"action": "export_preset", "kind": "layout", "name": "L2", "path": str(dest)}
    err, out = await _w(r, settings, "app_write", op)
    assert not err and out["receipt"]["verified"] is True
    op = {"action": "import_preset", "kind": "layout", "path": str(dest), "name": "L3"}
    err, out = await _w(r, settings, "app_write", op)
    assert not err and out["receipt"]["verified"] is True
    op = {"action": "import_preset", "kind": "burn_in", "path": str(dest), "name": "ignored"}
    err, out = await _w(r, settings, "app_write", op)
    assert not err and out["receipt"]["verified"] is True


async def test_preset_errors(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(
        r, settings, "app_write", {"action": "save_preset", "kind": "keyboard", "name": "K"}
    )
    assert err and out["code"] == "INVALID_PARAMS"
    err, out = await _w(
        r, settings, "app_write", {"action": "load_preset", "kind": "layout", "name": "zz"}
    )
    assert err and out["code"] == "NOT_FOUND"
    op = {"action": "import_preset", "kind": "layout", "path": "/etc/hosts"}
    err, out = await _w(r, settings, "app_write", op)
    assert err and out["code"] == "PATH_NOT_ALLOWED"


async def test_keyboard_needs_21_1(settings: Settings, monkeypatch: Any) -> None:
    r = make_project_resolve()
    from tests.fakes import fake_resolve  # noqa: PLC0415

    live = {
        **fake_resolve.LIVE_DIR,
        "Resolve": [m for m in fake_resolve.LIVE_DIR["Resolve"] if m != "GetKeyboardPresetList"],
    }
    monkeypatch.setattr(fake_resolve, "LIVE_DIR", live)
    err, out = await _w(r, settings, "app_read", {"action": "list_presets", "kind": "keyboard"})
    assert err and out["code"] == "CAPABILITY_UNAVAILABLE"


async def test_set_high_priority_sends_real_bool(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "app_write", {"action": "set_high_priority", "enabled": True})
    assert not err and r.high_priority is True and out["receipt"].get("verified") is None


async def test_destructive(settings: Settings) -> None:
    r = make_project_resolve()
    async with mcp_client(r, settings, ACTIONS) as client:
        op = {"op": {"action": "delete_preset", "kind": "layout", "name": "Default"}}
        err, out = await confirm(client, "app_delete", op)
        assert not err and out["receipt"]["verified"] is True and r.presets["layout"] == []
        err, out = await call(client, "app_delete", op)
        assert err and out["code"] == "NOT_FOUND"
        err, planned = await call(
            client, "app_delete", {"op": {"action": "disable_background_tasks"}}
        )
        assert planned["status"] == "planned" and not r.bg_disabled
        err, out = await confirm(
            client, "app_delete", {"op": {"action": "disable_background_tasks"}}
        )
        assert not err and r.bg_disabled
        err, planned = await call(client, "app_delete", {"op": {"action": "quit_resolve"}})
        assert any("Unsaved" in e for e in planned["plan"]["effects"]) and not r.quit_called
        err, out = await confirm(client, "app_delete", {"op": {"action": "quit_resolve"}})
        assert not err and r.quit_called
