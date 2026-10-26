# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Timeline domain tests over the fake Resolve."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.config import Settings
from davinci_mcp.domains.timeline import ACTIONS
from tests.fakes.fake_resolve import FakeResolve
from tests.fakes.timeline import RichTimeline, add_pool_clips, make_timeline_resolve
from tests.helpers import call, confirm, mcp_client


def _tl(resolve: FakeResolve, i: int = 0) -> RichTimeline:
    project = resolve.project_manager._current
    assert project is not None
    return project.timelines[i]  # type: ignore[return-value]


async def _write(
    settings: Settings, resolve: FakeResolve, op: dict[str, Any], tool: str = "timeline_write"
) -> tuple[bool, dict[str, Any]]:
    async with mcp_client(resolve, settings, ACTIONS) as client:
        return await call(client, tool, {"op": op})


async def _read(settings: Settings, resolve: FakeResolve, op: dict[str, Any]) -> dict[str, Any]:
    err, out = await _write(settings, resolve, op, "timeline_read")
    assert not err, out
    return out["result"]


def _verified(out: dict[str, Any]) -> bool:
    return out["status"] == "applied" and out["receipt"]["verified"] is True


# -- reads -------------------------------------------------------------------------


async def test_info_tracks_items(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    info = await _read(settings, resolve, {"action": "timeline_info"})
    assert info["name"] == "Timeline 1" and info["resolution"] == "1920x1080"
    assert info["track_counts"]["video"] == 1 and info["start_timecode"] == "01:00:00:00"
    tracks = await _read(settings, resolve, {"action": "list_tracks"})
    assert tracks["tracks"][0]["item_count"] == 3
    page = await _read(settings, resolve, {"action": "list_items", "limit": 2, "offset": 1})
    assert page["total"] == 3 and len(page["items"]) == 2 and page["items"][0]["position"] == 2
    one = await _read(
        settings, resolve, {"action": "list_items", "track": {"type": "video", "index": 1}}
    )
    assert one["total"] == 3
    first = page["items"][0]["name"]
    found = await _read(settings, resolve, {"action": "list_items", "name_contains": first.upper()})
    assert found["total"] >= 1 and all(first.lower() in i["name"].lower() for i in found["items"])


async def test_misc_reads(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    tl = _tl(resolve)
    tl.selected = [tl.tracks[0][1]]
    assert (await _read(settings, resolve, {"action": "current_timecode"}))["timecode"]
    assert (await _read(settings, resolve, {"action": "current_video_item"}))["item"]
    sel = await _read(settings, resolve, {"action": "selected_clips"})
    assert len(sel["items"]) == 1
    cfg = await _read(settings, resolve, {"action": "get_settings", "keys": ["timelineFrameRate"]})
    assert cfg["settings"] == {"timelineFrameRate": 24.0}
    blank = await _read(settings, resolve, {"action": "get_output_blanking"})
    assert blank["top"] == 0
    assert (await _read(settings, resolve, {"action": "get_mark_in_out"}))["marks"] == {}


async def test_marker_by_custom_data(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "add_marker", "frame": 4, "custom_data": "k1"}},
        )
        assert not err and _verified(out)
        err, out = await call(
            client,
            "timeline_read",
            {"op": {"action": "get_marker_by_custom_data", "custom_data": "k1"}},
        )
        assert not err and out["result"]["frame"] == 4
        err, out = await call(
            client,
            "timeline_read",
            {"op": {"action": "get_marker_by_custom_data", "custom_data": "no"}},
        )
        assert err and out["code"] == "NOT_FOUND"
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "update_marker_custom_data", "frame": 4, "custom_data": "k2"}},
        )
        assert not err and _verified(out)
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "add_markers", "markers": [{"frame": 4}, {"frame": 9}]}},
        )
        assert not err and _verified(out)
        assert out["receipt"]["result"]["added"] == [9]
        assert out["receipt"]["result"]["skipped"] == [4]


# -- timeline writes -----------------------------------------------------------------


async def test_create_rename_duplicate_current(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client, "timeline_write", {"op": {"action": "create_timeline", "name": "New"}}
        )
        assert not err and _verified(out)
        err, out = await call(
            client, "timeline_write", {"op": {"action": "create_timeline", "name": "New"}}
        )
        assert err and out["code"] == "WRONG_STATE"
        current_before = resolve.project_manager._current._current  # type: ignore[union-attr]
        err, out = await call(
            client,
            "timeline_write",
            {
                "op": {
                    "action": "duplicate_timeline",
                    "timeline": {"name": "Timeline 1"},
                    "name": "Dup",
                }
            },
        )
        assert not err and _verified(out)
        assert resolve.project_manager._current._current is current_before  # type: ignore[union-attr]
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "rename_timeline", "timeline": {"name": "Dup"}, "name": "Dup2"}},
        )
        assert not err and _verified(out)
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "set_current_timeline", "timeline": {"name": "Dup2"}}},
        )
        assert not err and _verified(out)


async def test_rename_lie_and_start_timecode(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    resolve.lie = {"SetName", "SetStartTimecode"}  # type: ignore[attr-defined]
    err, out = await _write(settings, resolve, {"action": "rename_timeline", "name": "X"})
    assert not err and out["receipt"]["verified"] is False
    err, out = await _write(
        settings, resolve, {"action": "set_start_timecode", "timecode": "00:00:00:00"}
    )
    assert not err and out["receipt"]["verified"] is False
    resolve.lie = set()  # type: ignore[attr-defined]
    err, out = await _write(
        settings, resolve, {"action": "set_start_timecode", "timecode": "00:00:00:00"}
    )
    assert not err and _verified(out)
    err, out = await _write(settings, resolve, {"action": "set_start_timecode", "timecode": "bad"})
    assert err and out["code"] == "INVALID_PARAMS"


async def test_settings(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    err, out = await _write(
        settings,
        resolve,
        {"action": "set_settings", "settings": {"timelineResolutionWidth": "1280"}},
    )
    assert not err and _verified(out)
    err, out = await _write(
        settings, resolve, {"action": "set_settings", "settings": {"timelineFrameRate": "25"}}
    )
    assert err and out["code"] == "API_CALL_FAILED"
    resolve.lie = {"SetSettings"}  # type: ignore[attr-defined]
    err, out = await _write(
        settings,
        resolve,
        {"action": "set_settings", "settings": {"timelineResolutionWidth": "640"}},
    )
    assert not err and out["receipt"]["verified"] is False


async def test_tracks(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    tl = _tl(resolve)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        for op in (
            {"action": "add_track", "type": "audio", "sub_type": "stereo"},
            {"action": "add_track", "type": "video"},
            {"action": "rename_track", "track": {"type": "video", "index": 2}, "name": "FX"},
            {
                "action": "set_track_enabled",
                "track": {"type": "video", "index": 1},
                "enabled": False,
            },
            {"action": "set_track_locked", "track": {"type": "audio", "index": 1}, "locked": True},
        ):
            err, out = await call(client, "timeline_write", {"op": op})
            assert not err and _verified(out), (op, out)
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "rename_track", "track": {"type": "video", "index": 9}, "name": "x"}},
        )
        assert err and out["code"] == "NOT_FOUND"
        tl.lie_check = True  # type: ignore[attr-defined]
        resolve.lie = {"AddTrack"}  # type: ignore[attr-defined]
        err, out = await call(
            client, "timeline_write", {"op": {"action": "add_track", "type": "video"}}
        )
        assert not err and out["receipt"]["verified"] is False
        resolve.lie = set()  # type: ignore[attr-defined]
        err, out = await confirm(
            client,
            "timeline_delete",
            {"op": {"action": "delete_track", "track": {"type": "video", "index": 2}}},
        )
        assert not err and _verified(out)


async def test_append_clips(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    add_pool_clips(resolve, "A", "B")
    tl = _tl(resolve)
    op = {
        "action": "append_clips",
        "clips": [
            {"clip": {"name": "A"}, "start_frame": 0, "end_frame": 50, "record_frame": 1000},
            {"clip": {"name": "B"}, "track_index": 1, "record_frame": 2000},
        ],
    }
    err, out = await _write(settings, resolve, op)
    assert not err and _verified(out), out
    infos = resolve.project_manager._current.media_pool.append_infos  # type: ignore[union-attr]
    assert infos[0]["recordFrame"] == tl._start_frame + 1000 and infos[0]["endFrame"] == 50
    resolve.lie = {"AppendToTimeline"}  # type: ignore[attr-defined]
    err, out = await _write(settings, resolve, op)
    assert not err and out["receipt"]["verified"] is False
    err, out = await _write(
        settings,
        resolve,
        {
            "action": "append_clips",
            "clips": [{"clip": {"name": "A"}, "start_frame": 10, "end_frame": 5}],
        },
    )
    assert err and out["code"] == "INVALID_PARAMS"
    err, out = await _write(
        settings, resolve, {"action": "append_clips", "clips": [{"clip": {"name": "ZZ"}}]}
    )
    assert err and out["code"] == "NOT_FOUND"


async def test_append_to_other_timeline_restores_current(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    add_pool_clips(resolve, "A")
    err, out = await _write(
        settings,
        resolve,
        {
            "action": "append_clips",
            "timeline": {"name": "Timeline 2"},
            "clips": [{"clip": {"name": "A"}}],
        },
    )
    assert not err and _verified(out)
    assert resolve.project_manager._current._current is _tl(resolve, 0)  # type: ignore[union-attr]


async def test_create_from_clips_and_import_export(settings: Settings, tmp_path: Path) -> None:
    resolve = make_timeline_resolve()
    add_pool_clips(resolve, "A")
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client,
            "timeline_write",
            {
                "op": {
                    "action": "create_timeline_from_clips",
                    "name": "FromClips",
                    "clips": [{"clip": {"name": "A"}}],
                }
            },
        )
        assert not err and _verified(out)
        err, out = await call(
            client,
            "timeline_write",
            {
                "op": {
                    "action": "create_timeline_from_clips",
                    "name": "FromClips",
                    "clips": [{"clip": {"name": "A"}}],
                }
            },
        )
        assert err and out["code"] == "WRONG_STATE"
        target = tmp_path / "out.edl"
        err, out = await call(
            client,
            "timeline_write",
            {
                "op": {
                    "action": "export_timeline",
                    "path": str(target),
                    "format": "edl",
                    "subtype": "none",
                }
            },
        )
        assert not err and _verified(out) and target.exists()
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "export_timeline", "path": str(target), "format": "edl"}},
        )
        assert err and out["code"] == "WRONG_STATE"
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "export_timeline", "path": "/etc/x.edl", "format": "edl"}},
        )
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        src = tmp_path / "in.edl"
        src.write_text("x")
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "import_timeline", "path": str(src), "timeline_name": "Imp"}},
        )
        assert not err and _verified(out)
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "import_timeline", "path": str(tmp_path / "no.edl")}},
        )
        assert err and out["code"] == "NOT_FOUND"
        # No timeline_name: fake returns the existing "Imported" timeline after the first import.
        for _ in range(2):
            err, out = await call(
                client, "timeline_write", {"op": {"action": "import_timeline", "path": str(src)}}
            )
        assert err and out["code"] == "WRONG_STATE"


async def test_import_into_timeline(settings: Settings, tmp_path: Path) -> None:
    resolve = make_timeline_resolve()
    src = tmp_path / "a.aaf"
    src.write_text("x")
    err, out = await _write(settings, resolve, {"action": "import_into_timeline", "path": str(src)})
    assert not err and _verified(out)


async def test_insert_and_compound(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "insert_generated", "kind": "title", "name": "Text+"}},
        )
        assert not err and _verified(out)
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "insert_generated", "kind": "fusion_composition"}},
        )
        assert not err and _verified(out)
        err, out = await call(
            client, "timeline_write", {"op": {"action": "insert_generated", "kind": "title"}}
        )
        assert err and out["code"] == "INVALID_PARAMS"
        refs = [{"track": {"type": "video", "index": 1}, "position": p} for p in (1, 2)]
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "create_compound_clip", "items": refs, "name": "Cmp"}},
        )
        assert not err and _verified(out)
        _tl(resolve).track_locked[("video", 1)] = True
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "insert_generated", "kind": "generator", "name": "Solid"}},
        )
        assert err and out["code"] == "API_CALL_FAILED"
        # Fusion title is not implemented by the fake: Resolve proxies return None.
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "insert_generated", "kind": "fusion_title", "name": "x"}},
        )
        assert err


async def test_links_align_stereo(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    refs = [{"track": {"type": "video", "index": 1}, "position": p} for p in (1, 2)]
    err, out = await _write(
        settings, resolve, {"action": "set_clips_linked", "items": refs, "linked": True}
    )
    assert not err and out["status"] == "applied" and out["receipt"].get("verified") is None
    err, out = await _write(
        settings, resolve, {"action": "auto_align_clips", "items": refs, "sync_using": "waveform"}
    )
    assert not err and _tl(resolve).align_options["SyncUsing"] == 200  # type: ignore[attr-defined]
    err, out = await _write(settings, resolve, {"action": "convert_to_stereo"})
    assert not err and out["status"] == "applied"


async def test_playhead_marks_blanking(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        for op in (
            {"action": "set_current_timecode", "timecode": "01:00:10:00"},
            {"action": "set_mark_in_out", "mark_in": 10, "mark_out": 50},
            {"action": "clear_mark_in_out", "type": "video"},
            {"action": "set_output_blanking", "top": 10, "bottom": 10},
        ):
            err, out = await call(client, "timeline_write", {"op": op})
            assert not err and _verified(out), (op, out)
        err, out = await call(
            client,
            "timeline_write",
            {"op": {"action": "set_mark_in_out", "mark_in": 9, "mark_out": 2}},
        )
        assert err and out["code"] == "INVALID_PARAMS"
        resolve.lie = {"SetMarkInOut", "SetOutputBlanking", "SetCurrentTimecode"}  # type: ignore[attr-defined]
        for op in (
            {"action": "set_current_timecode", "timecode": "01:00:20:00"},
            {"action": "set_mark_in_out", "mark_in": 1, "mark_out": 5},
            {"action": "set_output_blanking", "top": 99},
        ):
            err, out = await call(client, "timeline_write", {"op": op})
            assert not err and out["receipt"]["verified"] is False, op


# -- destructive -------------------------------------------------------------------------


async def test_delete_timelines(settings: Settings) -> None:
    resolve = make_timeline_resolve(timelines=3)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        op = {
            "action": "delete_timelines",
            "timelines": [{"name": "Timeline 2"}, {"name": "Timeline 3"}],
        }
        err, out = await confirm(client, "timeline_delete", {"op": op})
        assert not err and _verified(out)
        err, out = await call(client, "timeline_read", {"op": {"action": "list_timelines"}})
        assert [t["name"] for t in out["result"]["timelines"]] == ["Timeline 1"]
        err, out = await call(
            client, "timeline_delete", {"op": {"action": "delete_timelines", "timelines": [{}]}}
        )
        assert err and out["code"] == "INVALID_PARAMS"


async def test_delete_timelines_lie_and_stale(settings: Settings) -> None:
    resolve = make_timeline_resolve(timelines=2)
    resolve.lie = {"DeleteTimelines"}  # type: ignore[attr-defined]
    async with mcp_client(resolve, settings, ACTIONS) as client:
        op = {"action": "delete_timelines", "timelines": [{"name": "Timeline 2"}]}
        err, out = await confirm(client, "timeline_delete", {"op": op})
        assert not err and out["receipt"]["verified"] is False
        err, planned = await call(client, "timeline_delete", {"op": op})
        _tl(resolve, 1).tracks[0].pop()
        err, out = await call(
            client, "timeline_delete", {"op": op, "confirm_token": planned["plan"]["token"]}
        )
        assert err and out["code"] == "CONFIRMATION_STALE"


async def test_delete_markers_and_clips(settings: Settings) -> None:
    resolve = make_timeline_resolve()
    tl = _tl(resolve)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        for frame, color in ((1, "Red"), (2, "Blue")):
            await call(
                client,
                "timeline_write",
                {"op": {"action": "add_marker", "frame": frame, "color": color}},
            )
        err, out = await confirm(
            client, "timeline_delete", {"op": {"action": "delete_markers_by_color", "color": "Red"}}
        )
        assert not err and _verified(out) and list(tl.markers) == [2]
        err, out = await call(
            client,
            "timeline_delete",
            {"op": {"action": "delete_markers_by_color", "color": "Green"}},
        )
        assert err and out["code"] == "NOT_FOUND"
        err, out = await confirm(
            client, "timeline_delete", {"op": {"action": "delete_markers_by_color", "color": "All"}}
        )
        assert not err and _verified(out) and not tl.markers
        op = {
            "action": "delete_clips",
            "items": [{"track": {"type": "video", "index": 1}, "position": 1}],
        }
        err, planned = await call(client, "timeline_delete", {"op": op})
        assert planned["status"] == "planned"
        assert any("Linked" in e for e in planned["plan"]["effects"])
        err, out = await confirm(client, "timeline_delete", {"op": op})
        assert not err and _verified(out) and len(tl.tracks[0]) == 2
        resolve.lie = {"DeleteClips"}  # type: ignore[attr-defined]
        err, out = await confirm(client, "timeline_delete", {"op": op})
        assert not err and out["receipt"]["verified"] is False


@pytest.mark.parametrize("action_name", ["delete_track"])
async def test_delete_track_missing(settings: Settings, action_name: str) -> None:
    resolve = make_timeline_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client,
            "timeline_delete",
            {"op": {"action": action_name, "track": {"type": "audio", "index": 3}}},
        )
        assert err and out["code"] == "NOT_FOUND"
