# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the media domain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from davinci_mcp.config import Settings
from davinci_mcp.domains.media import ACTIONS
from tests.fakes.fake_resolve import FakeResolve
from tests.fakes.media import MPool, add_clip, make_media_resolve
from tests.helpers import call, confirm, mcp_client


def _pool(resolve: FakeResolve) -> MPool:
    project = resolve.project_manager._current
    assert project is not None
    assert isinstance(project.media_pool, MPool)
    return project.media_pool


async def _w(c: Any, op: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    r = await call(c, "media_write", {"op": op})
    return r


async def _r(c: Any, op: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    r = await call(c, "media_read", {"op": op})
    return r


def _verified(out: dict[str, Any]) -> bool:
    return bool(out["receipt"]["verified"])


async def test_read_storage_and_tree(settings: Settings, tmp_path: Path) -> None:
    resolve = make_media_resolve()
    (tmp_path / "a.mov").write_text("x")
    (tmp_path / "sub").mkdir()
    pool = _pool(resolve)
    sub = pool.AddSubFolder(pool.root_folder, "Shots")
    add_clip(resolve, "one", sub)
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await _r(c, {"action": "list_volumes"})
        assert not err and out["result"]["volumes"] == ["/Volumes/A", "/Volumes/B"]
        err, out = await _r(c, {"action": "browse_storage", "path": str(tmp_path)})
        assert (
            not err and out["result"]["total_files"] == 1 and len(out["result"]["subfolders"]) == 2
        )
        err, out = await _r(c, {"action": "browse_storage", "path": "/etc"})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await _r(c, {"action": "media_pool_tree"})
        folders = out["result"]["folders"]
        assert [f["path"] for f in folders] == ["Master", "Master/Shots"]
        assert folders[1]["clips"] == 1
        err, out = await _r(c, {"action": "media_pool_tree", "max_depth": 0})
        assert len(out["result"]["folders"]) == 1


async def test_list_clips_filters_and_pagination(settings: Settings) -> None:
    resolve = make_media_resolve()
    pool = _pool(resolve)
    sub = pool.AddSubFolder(pool.root_folder, "Shots")
    a = add_clip(resolve, "Alpha")
    b = add_clip(resolve, "Beta")
    add_clip(resolve, "Alpine", sub)
    a.color = "Orange"
    b.flags.append("Red")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await _r(c, {"action": "list_clips"})
        assert out["result"]["total"] == 2
        err, out = await _r(c, {"action": "list_clips", "recursive": True, "name_contains": "alp"})
        assert out["result"]["total"] == 2
        err, out = await _r(c, {"action": "list_clips", "clip_color": "Orange"})
        assert [x["name"] for x in out["result"]["clips"]] == ["Alpha"]
        err, out = await _r(c, {"action": "list_clips", "flag": "Red", "keys": ["Type"]})
        assert out["result"]["clips"][0]["properties"] == {"Type": "Video"}
        err, out = await _r(c, {"action": "list_clips", "limit": 1, "offset": 1})
        assert out["result"]["total"] == 2 and len(out["result"]["clips"]) == 1
        err, out = await _r(
            c, {"action": "list_clips", "folder": {"path": "Master/Shots"}, "type": "audio"}
        )
        assert out["result"]["total"] == 0
        err, out = await _r(c, {"action": "list_clips", "folder": {"path": "nope"}})
        assert err and out["code"] == "NOT_FOUND"


async def test_get_clip_and_selection(settings: Settings) -> None:
    resolve = make_media_resolve()
    clip = add_clip(resolve, "Alpha")
    clip.metadata["Comments"] = "hi"
    clip.AddMarker(5, "Blue", "m", "", 1, "cd")
    _pool(resolve).selected = [clip]
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await _r(c, {"action": "get_clip", "clip": {"name": "Alpha"}})
        res = out["result"]
        assert not err and res["folder"] == "Master" and res["marker_count"] == 1
        assert res["metadata"] == {"Comments": "hi"} and res["properties"]["Type"] == "Video"
        err, out = await _r(c, {"action": "get_clip", "clip": {"name": "Alpha"}, "keys": ["Type"]})
        assert out["result"]["properties"] == {"Type": "Video"}
        err, out = await _r(
            c, {"action": "get_clip", "clip": {"name": "Alpha"}, "all_properties": True}
        )
        assert len(out["result"]["properties"]) > 10
        err, out = await _r(c, {"action": "get_clip_markers", "clip": {"id": clip._uid}})
        assert out["result"]["markers"][0]["custom_data"] == "cd"
        err, out = await _r(c, {"action": "get_selection"})
        assert out["result"]["selected_clips"][0]["id"] == clip._uid
        assert out["result"]["current_folder"]["path"] == "Master"
        err, out = await _r(c, {"action": "get_audio_mapping", "clip": {"id": clip._uid}})
        assert out["result"]["parsed"] == {"embedded_audio_channels": 2}
        err, out = await _r(c, {"action": "get_clip", "clip": {"name": "zzz"}})
        assert err and out["code"] == "NOT_FOUND"
        err, out = await _r(c, {"action": "list_mattes", "clip": {"id": clip._uid}})
        assert not err and out["result"]["clip_mattes"] == []


async def test_import_media_into_folder(settings: Settings, tmp_path: Path) -> None:
    resolve = make_media_resolve()
    pool = _pool(resolve)
    pool.AddSubFolder(pool.root_folder, "Day1")
    f = tmp_path / "a.mov"
    f.write_text("x")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        op = {
            "action": "import_media",
            "items": [{"path": str(f)}],
            "folder": {"path": "Master/Day1"},
        }
        err, out = await _w(c, op)
        assert not err and _verified(out)
        assert pool.root_folder.subfolders[0].clips[0].GetName() == "a.mov"
        assert pool._current is pool.root_folder  # restored
        assert out["receipt"]["result"]["unmatched_paths"] == []
        op2 = {"action": "import_media", "items": [{"path": str(f)}], "via_media_storage": True}
        err, out = await _w(c, op2)
        assert not err and len(pool.root_folder.clips) == 1
        err, out = await _w(
            c, {"action": "import_media", "items": [{"path": str(tmp_path / "x.mov")}]}
        )
        assert (err and out["code"] == "PATH_NOT_ALLOWED") or err
        err, out = await _w(c, {"action": "import_media", "items": [{"path": "/etc/hosts"}]})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        seq = {
            "action": "import_media",
            "items": [{"path": str(tmp_path / "s.%04d.exr"), "start_index": 1, "end_index": 9}],
        }
        err, out = await _w(c, seq)
        assert not err and _verified(out)


async def test_folders_and_moves(settings: Settings) -> None:
    resolve = make_media_resolve()
    pool = _pool(resolve)
    clip = add_clip(resolve, "Alpha")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        err, out = await _w(c, {"action": "create_folder", "name": "A", "set_current": True})
        assert not err and _verified(out) and out["receipt"]["result"]["path"] == "Master/A"
        assert pool._current.GetName() == "A"
        err, out = await _w(c, {"action": "create_folder", "name": "A"})
        assert err and out["code"] == "WRONG_STATE"
        err, out = await _w(
            c, {"action": "create_folder", "name": "B", "parent": {"path": "Master/A"}}
        )
        assert not err
        err, out = await _w(c, {"action": "set_current_folder", "folder": {"path": "Master"}})
        assert _verified(out) and pool._current is pool.root_folder
        err, out = await _w(
            c,
            {
                "action": "move_clips",
                "clips": [{"id": clip._uid}],
                "target": {"path": "Master/A/B"},
            },
        )
        assert _verified(out) and pool.root_folder.clips == []
        mv = {
            "action": "move_folders",
            "folders": [{"path": "Master/A/B"}],
            "target": {"path": "Master"},
        }
        err, out = await _w(c, mv)
        assert _verified(out) and len(pool.root_folder.subfolders) == 2
        bad = {
            "action": "move_folders",
            "folders": [{"path": "Master"}],
            "target": {"path": "Master/A"},
        }
        err, out = await _w(c, bad)
        assert err and out["code"] == "INVALID_PARAMS"
        pool.noop.add("MoveClips")
        err, out = await _w(
            c, {"action": "move_clips", "clips": [{"id": clip._uid}], "target": {"path": "Master"}}
        )
        assert not err and not _verified(out)


async def test_properties_metadata_name_color_flags(settings: Settings) -> None:
    resolve = make_media_resolve()
    clip = add_clip(resolve, "Alpha")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        ref = {"id": clip._uid}
        err, out = await _w(
            c,
            {
                "action": "set_clip_properties",
                "clip": ref,
                "properties": {"Reel Name": "R1", "Nope": "x"},
            },
        )
        assert not err and out["receipt"]["result"]["rejected"] == ["Nope"]
        assert clip._props["Reel Name"] == "R1"
        clip.noop.add("Reel Name")
        err, out = await _w(
            c, {"action": "set_clip_properties", "clip": ref, "properties": {"Reel Name": "R2"}}
        )
        assert not _verified(out)
        err, out = await _w(
            c, {"action": "set_clip_properties", "clip": ref, "properties": {"Nope": "x"}}
        )
        assert err and out["code"] == "API_CALL_FAILED"
        err, out = await _w(
            c, {"action": "set_clip_metadata", "clip": ref, "metadata": {"Comments": "hi"}}
        )
        assert _verified(out) and clip.metadata == {"Comments": "hi"}
        err, out = await _w(
            c,
            {
                "action": "set_clip_metadata",
                "clip": ref,
                "metadata": {"k": "v"},
                "third_party": True,
            },
        )
        assert _verified(out) and clip.third == {"k": "v"}
        err, out = await _w(c, {"action": "set_clip_name", "clip": ref, "name": "Gamma"})
        assert _verified(out) and clip.GetName() == "Gamma"
        clip.noop.add("SetName")
        err, out = await _w(c, {"action": "set_clip_name", "clip": ref, "name": "Delta"})
        assert not _verified(out)
        err, out = await _w(c, {"action": "set_clip_color", "clip": ref, "color": "Teal"})
        assert _verified(out) and clip.color == "Teal"
        err, out = await _w(c, {"action": "set_clip_color", "clip": ref, "color": "Cyan"})
        assert err  # marker-palette name rejected by schema
        clip.noop.add("SetClipColor")
        err, out = await _w(c, {"action": "set_clip_color", "clip": ref, "color": "Navy"})
        assert not _verified(out)
        err, out = await _w(c, {"action": "set_clip_color", "clip": ref})
        assert _verified(out) and clip.color == ""
        err, out = await _w(c, {"action": "add_flag", "clip": ref, "color": "Red"})
        assert _verified(out) and clip.flags == ["Red"]
        err, out = await _w(c, {"action": "clear_flags", "clip": ref})
        assert _verified(out) and clip.flags == []


async def test_clip_markers_and_inout(settings: Settings) -> None:
    resolve = make_media_resolve()
    clip = add_clip(resolve, "Alpha")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        ref = {"id": clip._uid}
        add = {"action": "add_clip_marker", "clip": ref, "frame": 10, "color": "Red", "name": "n"}
        err, out = await _w(c, add)
        assert not err and _verified(out)
        err, out = await _w(c, add)
        assert err and out["code"] == "WRONG_STATE"
        upd = {"action": "update_marker_custom_data", "clip": ref, "frame": 10, "custom_data": "z"}
        err, out = await _w(c, upd)
        assert _verified(out) and clip.markers[10]["customData"] == "z"
        clip.noop.add("UpdateMarkerCustomData")
        err, out = await _w(c, {**upd, "custom_data": "q"})
        assert not _verified(out)
        err, out = await _w(c, {**upd, "frame": 99})
        assert err and out["code"] == "NOT_FOUND"
        err, out = await _w(
            c, {"action": "set_mark_in_out", "clip": ref, "mark_in": 1, "mark_out": 9}
        )
        assert _verified(out)
        err, out = await _w(
            c, {"action": "set_mark_in_out", "clip": ref, "mark_in": 9, "mark_out": 1}
        )
        assert err and out["code"] == "INVALID_PARAMS"
        err, out = await _w(c, {"action": "clear_mark_in_out", "clip": ref})
        assert _verified(out) and clip.inout == {}
        err, out = await _w(c, {"action": "select_clip", "clip": ref})
        assert _verified(out)


async def test_delete_clip_markers(settings: Settings) -> None:
    resolve = make_media_resolve()
    clip = add_clip(resolve, "Alpha")
    for frame, color, data in ((1, "Red", "a"), (2, "Red", "b"), (3, "Blue", "c")):
        clip.AddMarker(frame, color, "", "", 1, data)
    async with mcp_client(resolve, settings, ACTIONS) as c:
        ref = {"id": clip._uid}
        err, out = await call(
            c, "media_delete", {"op": {"action": "delete_clip_markers", "clip": ref}}
        )
        assert err  # one selector required
        by_frame = {"action": "delete_clip_markers", "clip": ref, "frame": 1}
        err, out = await confirm(c, "media_delete", {"op": by_frame})
        assert not err and _verified(out) and 1 not in clip.markers
        err, out = await confirm(
            c, "media_delete", {"op": {**by_frame, "frame": None, "color": "Red"}}
        )
        assert _verified(out) and list(clip.markers) == [3]
        err, out = await confirm(
            c, "media_delete", {"op": {**by_frame, "frame": None, "custom_data": "c"}}
        )
        assert _verified(out) and clip.markers == {}
        err, out = await call(c, "media_delete", {"op": {**by_frame, "frame": 77}})
        assert err and out["code"] == "NOT_FOUND"
        clip.AddMarker(4, "Blue", "", "", 1, "d")
        clip.noop.add("DeleteMarkerAtFrame")
        err, out = await confirm(c, "media_delete", {"op": {**by_frame, "frame": 4}})
        assert not err and not _verified(out)


async def test_proxy_replace_relink(settings: Settings, tmp_path: Path) -> None:
    resolve = make_media_resolve()
    old = tmp_path / "old.mov"
    old.write_text("x")
    clip = add_clip(resolve, "Alpha", path=str(old))
    proxy = tmp_path / "p.mov"
    proxy.write_text("x")
    new = tmp_path / "new.mov"
    new.write_text("x")
    folder = tmp_path / "moved"
    folder.mkdir()
    async with mcp_client(resolve, settings, ACTIONS) as c:
        ref = {"id": clip._uid}
        err, out = await _w(c, {"action": "link_proxy_media", "clip": ref, "path": str(proxy)})
        assert not err and _verified(out)
        err, out = await _w(c, {"action": "link_proxy_media", "clip": ref, "path": "/etc/hosts"})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await _w(
            c, {"action": "link_proxy_media", "clip": ref, "path": str(tmp_path / "ghost.mov")}
        )
        assert err
        err, out = await _w(
            c, {"action": "link_full_resolution_media", "clip": ref, "path": str(new)}
        )
        assert not err and "verified" not in out["receipt"]
        err, out = await _w(c, {"action": "replace_clip", "clip": ref, "path": str(new)})
        assert _verified(out) and clip._props["File Path"] == str(new.resolve())
        clip.noop.add("ReplaceClip")
        err, out = await _w(c, {"action": "replace_clip", "clip": ref, "path": str(old)})
        assert not _verified(out)
        rl = {"action": "relink_clips", "clips": [ref], "folder_path": str(folder)}
        err, out = await _w(c, rl)
        assert _verified(out)
        err, out = await _w(c, {**rl, "folder_path": "/etc"})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await confirm(
            c, "media_delete", {"op": {"action": "unlink_proxy_media", "clip": ref}}
        )
        assert not err and _verified(out) and clip._props["Proxy Media Path"] == ""
        err, out = await call(
            c, "media_delete", {"op": {"action": "unlink_proxy_media", "clip": ref}}
        )
        assert err and out["code"] == "WRONG_STATE"
        err, out = await confirm(
            c, "media_delete", {"op": {"action": "unlink_clips", "clips": [ref]}}
        )
        assert not err and "verified" not in out["receipt"]


async def test_multicam_sync_stereo(settings: Settings) -> None:
    resolve = make_media_resolve()
    a, b = add_clip(resolve, "A"), add_clip(resolve, "B")
    pool = _pool(resolve)
    refs = [{"id": a._uid}, {"id": b._uid}]
    async with mcp_client(resolve, settings, ACTIONS) as c:
        mc = {
            "action": "create_multicam_clip",
            "clips": refs,
            "options": {
                "name": "MC",
                "angleSyncMode": "MULTICAM_ANGLE_SYNC_AUDIO",
                "channelConfig": 2,
            },
        }
        err, out = await _w(c, mc)
        assert not err and _verified(out)
        assert pool.last_options == {"name": "MC", "angleSyncMode": 11, "channelConfig": 2}
        bad = {**mc, "options": {"angleSyncMode": "MULTICAM_ANGLE_SYNC_MARKER"}}
        err, out = await _w(c, bad)
        assert err and out["code"] == "CAPABILITY_UNAVAILABLE"
        sync = {
            "action": "auto_sync_audio",
            "clips": refs,
            "settings": {
                "syncMode": "AUDIO_SYNC_WAVEFORM",
                "channelNumber": "AUDIO_SYNC_CHANNEL_MIX",
            },
        }
        err, out = await _w(c, sync)
        assert not err and _verified(out)
        assert pool.last_sync == {"syncMode": 7, "channelNumber": 9}
        err, out = await _w(c, {"action": "auto_sync_audio", "clips": refs[:1]})
        assert err
        st = {"action": "create_stereo_clip", "left": refs[0], "right": refs[1]}
        err, out = await _w(c, st)
        assert not err and _verified(out)


async def test_mattes_and_exports(settings: Settings, tmp_path: Path) -> None:
    resolve = make_media_resolve()
    clip = add_clip(resolve, "A")
    matte = tmp_path / "m.png"
    matte.write_text("x")
    async with mcp_client(resolve, settings, ACTIONS) as c:
        ref = {"id": clip._uid}
        err, out = await _w(c, {"action": "add_clip_mattes", "clip": ref, "paths": [str(matte)]})
        assert not err and _verified(out)
        err, out = await _r(c, {"action": "list_mattes", "clip": ref})
        assert len(out["result"]["clip_mattes"]) == 1
        err, out = await _w(c, {"action": "add_timeline_mattes", "paths": [str(matte)]})
        assert not err and _verified(out)
        err, out = await confirm(
            c,
            "media_delete",
            {"op": {"action": "delete_clip_mattes", "clip": ref, "paths": [str(matte.resolve())]}},
        )
        assert not err and _verified(out)
        csv = tmp_path / "meta.csv"
        err, out = await _w(c, {"action": "export_metadata", "path": str(csv)})
        assert not err and _verified(out) and csv.exists()
        err, out = await _w(c, {"action": "export_metadata", "path": str(tmp_path / "x.txt")})
        assert err and out["code"] == "INVALID_PARAMS"
        drb = tmp_path / "f.drb"
        err, out = await _w(
            c, {"action": "export_folder_drb", "folder": {"path": "Master"}, "path": str(drb)}
        )
        assert not err and _verified(out)
        err, out = await _w(c, {"action": "import_folder_drb", "path": str(drb)})
        assert not err and "verified" not in out["receipt"]
        err, out = await _w(c, {"action": "refresh_folders"})
        assert not err
        err, out = await _w(c, {"action": "reveal_in_storage", "path": str(tmp_path)})
        assert not err
        err, out = await _w(c, {"action": "monitor_growing_file", "clip": ref})
        assert not err


async def test_delete_clips_and_folders(settings: Settings) -> None:
    resolve = make_media_resolve()
    pool = _pool(resolve)
    sub = pool.AddSubFolder(pool.root_folder, "Old")
    keep = add_clip(resolve, "keep")
    gone = add_clip(resolve, "gone", sub)
    gone._props["Usage"] = "2"
    async with mcp_client(resolve, settings, ACTIONS) as c:
        dc = {"action": "delete_clips", "clips": [{"id": gone._uid}]}
        err, planned = await call(c, "media_delete", {"op": dc})
        assert planned["status"] == "planned" and gone in sub.clips
        assert "used in timelines" in " ".join(planned["plan"]["effects"])
        err, out = await confirm(c, "media_delete", {"op": dc})
        assert not err and _verified(out) and sub.clips == []
        pool.noop.add("DeleteClips")
        err, out = await confirm(
            c, "media_delete", {"op": {"action": "delete_clips", "clips": [{"id": keep._uid}]}}
        )
        assert not err and not _verified(out)
        err, out = await call(
            c, "media_delete", {"op": {"action": "delete_folders", "folders": [{"path": "Master"}]}}
        )
        assert err and out["code"] == "INVALID_PARAMS"
        df = {"action": "delete_folders", "folders": [{"path": "Master/Old"}]}
        err, out = await confirm(c, "media_delete", {"op": df})
        assert not err and _verified(out) and pool.root_folder.subfolders == []


async def test_clear_metadata(settings: Settings) -> None:
    resolve = make_media_resolve()
    clip = add_clip(resolve, "A")
    clip.metadata["Comments"] = "keep me?"
    async with mcp_client(resolve, settings, ACTIONS) as c:
        op = {"action": "clear_clip_metadata", "clip": {"id": clip._uid}, "keys": ["Comments"]}
        _err, planned = await call(c, "media_delete", {"op": op})
        assert planned["status"] == "planned" and clip.metadata["Comments"] == "keep me?"
        _err, out = await confirm(c, "media_delete", {"op": op})
        assert _verified(out) and clip.metadata["Comments"] == ""


async def test_clone_media_job(settings: Settings, tmp_path: Path) -> None:
    resolve = make_media_resolve()
    src = tmp_path / "src"
    src.mkdir()
    async with mcp_client(resolve, settings, ACTIONS) as c:
        op = {
            "action": "clone_media",
            "source_dir": str(src),
            "target_dirs": [str(tmp_path / "t1"), str(tmp_path / "t2")],
            "checksum_type": "CLONE_CHECKSUM_TYPE_SHA256",
            "preserve_folder_name": True,
        }
        err, out = await _w(c, op)
        assert not err and out["status"] == "started"
        err, waited = await call(c, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 10})
        assert not err and waited["state"] == "succeeded"
        storage = resolve.media_storage
        assert storage.clone_settings == {"PreserveFolderName": True, "ChecksumType": 5}
        assert len(storage.clone_started[1]) == 2
        err, out = await _r(c, {"action": "get_clone_status"})
        assert out["result"]["completion_percentage"] == 100.0
        err, out = await _w(c, {**op, "source_dir": "/etc"})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
