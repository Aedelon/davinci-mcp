# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the gallery domain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from davinci_mcp.config import Settings
from davinci_mcp.domains.gallery import ACTIONS
from tests.fakes.color import install_color
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, confirm, mcp_client


def _setup() -> tuple[Any, dict[str, Any]]:
    resolve = make_resolve()
    return resolve, install_color(resolve)


async def test_albums(settings: Settings) -> None:
    resolve, state = _setup()
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(c, "gallery_read", {"op": {"action": "list_albums"}})
        assert not err
        albums = out["result"]["albums"]
        assert [a["kind"] for a in albums] == ["still", "powergrade"] and albums[0]["current"]
        err, out = await call(
            c, "gallery_write", {"op": {"action": "create_album", "kind": "still"}}
        )
        assert not err and out["receipt"]["verified"] is True
        cur = {"action": "set_current_album", "album": {"name": "Stills 2"}}
        err, out = await call(c, "gallery_write", {"op": cur})
        assert out["receipt"]["verified"] is True and state["gallery"].current.name == "Stills 2"
        ren = {
            "action": "rename_album",
            "album": {"kind": "powergrade", "index": 1},
            "new_name": "PG",
        }
        err, out = await call(c, "gallery_write", {"op": ren})
        assert out["receipt"]["verified"] is True
        bad = {"action": "set_current_album", "album": {"name": "zzz"}}
        err, out = await call(c, "gallery_write", {"op": bad})
        assert err and out["code"] == "NOT_FOUND"
        err, _ = await call(
            c,
            "gallery_write",
            {"op": {"action": "rename_album", "album": {"kind": "powergrade"}, "new_name": "x"}},
        )
        assert err  # powergrade needs a selector


async def test_grab_and_list(settings: Settings) -> None:
    resolve, state = _setup()
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(c, "gallery_write", {"op": {"action": "grab_still"}})
        assert not err and out["receipt"]["verified"] is True and resolve.page == "edit"
        op = {"action": "grab_still", "scope": "all", "frame_source": "middle"}
        err, out = await call(c, "gallery_write", {"op": op})
        assert out["receipt"]["result"]["grabbed"] == 3
        err, out = await call(c, "gallery_read", {"op": {"action": "list_stills"}})
        assert len(out["result"]["stills"]) == 4
        assert state["gallery"].current.stills


async def test_import_label_delete(settings: Settings, tmp_path: Path) -> None:
    resolve, state = _setup()
    f = tmp_path / "look.drx"
    f.write_text("x")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(
            c, "gallery_write", {"op": {"action": "import_stills", "paths": [str(f)]}}
        )
        assert not err and out["receipt"]["verified"] is True
        err, out = await call(
            c, "gallery_write", {"op": {"action": "import_stills", "paths": ["/etc/hosts"]}}
        )
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        lab = {"action": "set_still_label", "still": 1, "label": "Hero"}
        err, out = await call(c, "gallery_write", {"op": lab})
        assert out["receipt"]["verified"] is True
        err, out = await call(c, "gallery_write", {"op": {**lab, "still": 5}})
        assert err and out["code"] == "NOT_FOUND"
        dele = {"action": "delete_stills", "stills": ["Hero"]}
        err, planned = await call(c, "gallery_delete", {"op": dele})
        assert planned["status"] == "planned" and state["gallery"].current.stills
        err, out = await confirm(c, "gallery_delete", {"op": dele})
        assert not err and out["receipt"]["verified"] is True
        assert state["gallery"].current.stills == []


async def test_export_stills(settings: Settings, tmp_path: Path) -> None:
    resolve, state = _setup()
    album = state["gallery"].current
    album.add("a")
    album.add("b")
    op = {
        "action": "export_stills",
        "folder": str(tmp_path / "exp"),
        "prefix": "s",
        "format": "png",
    }
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await call(c, "gallery_write", {"op": op})
        assert not err and out["receipt"]["verified"] is True
        assert out["receipt"]["result"]["files"] == ["s_1.png", "s_2.png"]
        album.export_ok = False
        err, out = await call(c, "gallery_write", {"op": {**op, "stills": [1]}})
        assert err and out["code"] == "WRONG_STATE" and out["hint"]
        err, out = await call(c, "gallery_write", {"op": {**op, "folder": "/etc/x"}})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
