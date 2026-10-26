# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the project domain over the fake Resolve."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from davinci_mcp.domains.project import ACTIONS
from tests.fakes.project import AppResolve, RichPM, make_project_resolve
from tests.helpers import call, confirm, mcp_client

if TYPE_CHECKING:
    from pathlib import Path

    from davinci_mcp.config import Settings


def _pm(r: AppResolve) -> RichPM:
    return r.project_manager  # type: ignore[return-value]


async def _w(r: AppResolve, settings: Settings, tool: str, op: dict[str, Any]):
    async with mcp_client(r, settings, ACTIONS) as client:
        return await call(client, tool, {"op": op})


async def test_reads(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "project_read", {"action": "list_projects"})
    assert not err
    res = out["result"]
    assert res["current_project"] == "Shot" and res["folders"] == ["Clients"]
    assert res["projects"][0]["last_modified"] == 1_700_000_000
    err, out = await _w(r, settings, "project_read", {"action": "get_current_project"})
    assert not err and out["result"]["unique_id"] == "uid-Shot"
    assert out["result"]["database"]["DbName"] == "Local Database"
    err, out = await _w(r, settings, "project_read", {"action": "list_databases"})
    assert not err and len(out["result"]["databases"]) == 2
    err, out = await _w(r, settings, "project_read", {"action": "list_settings_presets"})
    assert not err and out["result"]["presets"][0]["Name"] == "Preset A"
    op = {"action": "get_settings", "keys": ["timelineFrameRate", "nope"]}
    err, out = await _w(r, settings, "project_read", op)
    assert out["result"]["settings"] == {"timelineFrameRate": "24"}
    assert out["result"]["missing"] == ["nope"]


async def test_create_project_saves_current_and_verifies(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "project_write", {"action": "create_project", "name": "New"})
    assert not err and out["receipt"]["verified"] is True
    assert ("RichPM.SaveProject", ()) in r.all_calls


async def test_create_project_guards(settings: Settings) -> None:
    r = make_project_resolve(current="Untitled Project")
    op = {"action": "create_project", "name": "New"}
    err, out = await _w(r, settings, "project_write", op)
    assert err and out["code"] == "WRONG_STATE"
    err, out = await _w(r, settings, "project_write", {**op, "discard_untitled": True})
    assert not err
    r = make_project_resolve()
    _pm(r).stuck = True
    err, out = await _w(r, settings, "project_write", op)
    assert err and out["code"] == "INSTANCE_STUCK"
    r = make_project_resolve()
    _pm(r).modal = True
    err, out = await _w(r, settings, "project_write", op)
    assert err and out["code"] == "API_CALL_FAILED"
    err, out = await _w(r, settings, "project_write", {**op, "name": "Other"})
    assert err and out["code"] == "WRONG_STATE"


async def test_load_project(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "project_write", {"action": "load_project", "name": "Other"})
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(r, settings, "project_write", {"action": "load_project", "name": "Zzz"})
    assert err and out["code"] == "NOT_FOUND"


async def test_save_project_refuses_untitled(settings: Settings) -> None:
    r = make_project_resolve(current="Untitled Project")
    err, out = await _w(r, settings, "project_write", {"action": "save_project"})
    assert err and out["code"] == "WRONG_STATE"
    assert ("RichPM.SaveProject", ()) not in r.all_calls
    r = make_project_resolve()
    err, out = await _w(r, settings, "project_write", {"action": "save_project"})
    assert not err and out["receipt"].get("verified") is None


async def test_close_and_rename(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "project_write", {"action": "rename_project", "new_name": "X"})
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(
        r, settings, "project_write", {"action": "rename_project", "new_name": "Other"}
    )
    assert err and out["code"] == "WRONG_STATE"
    err, out = await _w(r, settings, "project_write", {"action": "close_project"})
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(r, settings, "project_write", {"action": "close_project"})
    assert err and out["code"] == "WRONG_STATE"


async def test_folders(settings: Settings) -> None:
    r = make_project_resolve()
    err, out = await _w(r, settings, "project_write", {"action": "create_folder", "name": "F"})
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(r, settings, "project_write", {"action": "open_folder", "name": "F"})
    assert not err and out["receipt"]["result"]["current_folder"] == "Root/F"
    err, out = await _w(r, settings, "project_write", {"action": "goto_folder", "to": "parent"})
    assert not err and out["receipt"]["result"]["current_folder"] == "Root"
    err, out = await _w(r, settings, "project_write", {"action": "goto_folder", "to": "root"})
    assert not err
    err, out = await _w(r, settings, "project_write", {"action": "open_folder", "name": "nope"})
    assert err and out["code"] == "NOT_FOUND"


async def test_set_database(settings: Settings) -> None:
    r = make_project_resolve()
    op = {"action": "set_database", "database": {"DbType": "Disk", "DbName": "B"}}
    err, out = await _w(r, settings, "project_write", op)
    assert not err and out["receipt"]["verified"] is True
    op["database"] = {"DbType": "Disk", "DbName": "missing"}
    err, out = await _w(r, settings, "project_write", op)
    assert err and out["code"] == "API_CALL_FAILED"


async def test_set_settings_reports_unstuck_keys(settings: Settings) -> None:
    r = make_project_resolve()
    project = _pm(r)._current
    project.lying_keys.add("timelineResolutionWidth")  # type: ignore[union-attr]
    op = {
        "action": "set_settings",
        "settings": {
            "timelineFrameRate": 25,
            "timelineResolutionWidth": 3840,
            "timelinePlaybackFrameRate": "30",
        },
    }
    err, out = await _w(r, settings, "project_write", op)
    assert not err
    res = out["receipt"]["result"]
    assert res["applied"] == {"timelineFrameRate": "25"}
    assert set(res["not_applied"]) == {"timelineResolutionWidth", "timelinePlaybackFrameRate"}
    assert out["receipt"]["verified"] is False
    err, out = await _w(
        r, settings, "project_write", {"action": "set_settings", "settings": {"zzz": 1}}
    )
    assert err and out["code"] == "API_CALL_FAILED"


async def test_settings_presets(settings: Settings, tmp_path: Path) -> None:
    r = make_project_resolve()
    for op in (
        {"action": "save_settings_preset", "name": "P2"},
        {"action": "update_settings_preset", "name": "P2"},
        {"action": "apply_settings_preset", "name": "P2"},
    ):
        err, out = await _w(r, settings, "project_write", op)
        assert not err, out
    err, out = await _w(
        r, settings, "project_write", {"action": "save_settings_preset", "name": "P2"}
    )
    assert err and out["code"] == "WRONG_STATE"
    dest = tmp_path / "p.preset"
    op = {"action": "export_settings_preset", "name": "P2", "path": str(dest)}
    err, out = await _w(r, settings, "project_write", op)
    assert not err and out["receipt"]["verified"] is True and dest.exists()
    err, out = await _w(r, settings, "project_write", op)
    assert err and out["code"] == "WRONG_STATE"
    op = {"action": "import_settings_preset", "path": str(dest), "name": "P3"}
    err, out = await _w(r, settings, "project_write", op)
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(r, settings, "project_write", {**op, "path": "/etc/passwd"})
    assert err and out["code"] == "PATH_NOT_ALLOWED"


async def test_apply_preset_refused_when_untitled(settings: Settings) -> None:
    r = make_project_resolve(current="Untitled Project")
    op = {"action": "apply_settings_preset", "name": "Preset A"}
    err, out = await _w(r, settings, "project_write", op)
    assert err and out["code"] == "WRONG_STATE"


async def test_import_export_restore_archive(settings: Settings, tmp_path: Path) -> None:
    r = make_project_resolve()
    drp = tmp_path / "a.drp"
    err, out = await _w(
        r,
        settings,
        "project_write",
        {"action": "export_project", "name": "Other", "path": str(drp)},
    )
    assert not err and out["receipt"]["verified"] is True
    r.project_manager.ExportProject = lambda *a: True  # type: ignore[method-assign]
    err, out = await _w(
        r,
        settings,
        "project_write",
        {"action": "export_project", "name": "Other", "path": str(tmp_path / "b.drp")},
    )
    assert not err and out["receipt"]["verified"] is False
    err, out = await _w(
        r, settings, "project_write", {"action": "import_project", "path": str(drp), "name": "Imp"}
    )
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(
        r, settings, "project_write", {"action": "import_project", "path": str(drp)}
    )
    assert not err and out["receipt"]["verified"] is True
    err, out = await _w(
        r,
        settings,
        "project_write",
        {"action": "restore_project", "path": str(drp), "name": "Rest"},
    )
    assert not err
    err, out = await _w(
        r,
        settings,
        "project_write",
        {"action": "import_project", "path": str(tmp_path / "missing.drp")},
    )
    assert err
    err, out = await _w(
        r,
        settings,
        "project_write",
        {"action": "export_project", "name": "Nope", "path": str(tmp_path / "c.drp")},
    )
    assert err and out["code"] == "NOT_FOUND"
    arch = {"action": "archive_project", "name": "Other", "path": str(tmp_path / "arch")}
    err, out = await _w(r, settings, "project_write", arch)
    assert err and out["code"] == "CAPABILITY_UNAVAILABLE"
    err, out = await _w(r, settings, "project_write", {**arch, "force": True})
    assert not err and out["receipt"]["verified"] is True


async def test_delete_project(settings: Settings) -> None:
    r = make_project_resolve()
    async with mcp_client(r, settings, ACTIONS) as client:
        cur = {"op": {"action": "delete_project", "name": "Shot"}}
        err, out = await call(client, "project_delete", cur)
        assert err and out["code"] == "WRONG_STATE"
        err, out = await confirm(
            client, "project_delete", {"op": {"action": "delete_project", "name": "Other"}}
        )
        assert not err and out["receipt"]["verified"] is True
        assert "Other" not in _pm(r).projects
        err, out = await call(
            client, "project_delete", {"op": {"action": "delete_project", "name": "Other"}}
        )
        assert err and out["code"] == "NOT_FOUND"


async def test_delete_folder_and_preset(settings: Settings) -> None:
    r = make_project_resolve()
    async with mcp_client(r, settings, ACTIONS) as client:
        err, out = await confirm(
            client, "project_delete", {"op": {"action": "delete_folder", "name": "Clients"}}
        )
        assert not err and out["receipt"]["verified"] is True
        err, out = await call(
            client, "project_delete", {"op": {"action": "delete_folder", "name": "Clients"}}
        )
        assert err and out["code"] == "NOT_FOUND"
        op = {"op": {"action": "delete_settings_preset", "name": "Preset A"}}
        err, out = await confirm(client, "project_delete", op)
        assert not err and out["receipt"]["verified"] is True


async def test_no_project_open(settings: Settings) -> None:
    r = make_project_resolve(current=None)
    err, out = await _w(r, settings, "project_read", {"action": "get_current_project"})
    assert err and out["code"] == "WRONG_STATE"
    err, out = await _w(r, settings, "project_read", {"action": "list_projects"})
    assert not err and out["result"]["current_project"] is None
