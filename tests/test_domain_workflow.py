# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the workflow domain (one call per multi-step job)."""

from __future__ import annotations

import os
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from davinci_mcp.config import Settings
from davinci_mcp.domains import color, workflow
from davinci_mcp.domains.workflow import ACTIONS
from tests.fakes.workflow import WfItem, World
from tests.helpers import call, mcp_client

EDL = """TITLE: conform
FCM: NON-DROP FRAME

001  A001     V     C        01:00:10:00 01:00:15:00 01:00:00:00 01:00:05:00
* FROM CLIP NAME: A001.mov
002  A002     V     C        01:00:20:00 01:00:25:00 01:00:05:00 01:00:10:00
* FROM CLIP NAME: A002.mov
"""


@pytest.fixture(autouse=True)
def fast_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real waiting: a fake clock that advances by the requested sleeps."""
    now = [1000.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(
        workflow,
        "time",
        SimpleNamespace(sleep=sleep, monotonic=lambda: now[0], time=real_time.time),
    )
    monkeypatch.setattr(workflow, "_probe", lambda _path: None)


async def _write(client: Any, action: str, **params: Any) -> tuple[bool, dict[str, Any]]:
    return await call(client, "workflow_write", {"op": {"action": action, **params}})


def _media(tmp_path: Path, *names: str) -> list[Path]:
    folder = tmp_path / "media"
    folder.mkdir(exist_ok=True)
    out: list[Path] = []
    for name in names:
        path = folder / name
        path.write_bytes(b"media")
        out.append(path)
    return out


def _edl(tmp_path: Path, text: str = EDL, name: str = "cut.edl") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# -- conform_from_edl ------------------------------------------------------------------


async def test_conform_fixes_offline_events_with_start_tc(
    settings: Settings, tmp_path: Path
) -> None:
    world = World()
    a, b = _media(tmp_path, "A001.mov", "A002.mov")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c,
            "conform_from_edl",
            media=[str(a), str(b)],
            edl=str(_edl(tmp_path)),
            timeline_name="Conform",
        )
    assert not err and out["receipt"]["verified"] is True
    r = out["receipt"]["result"]
    assert (r["items"], r["linked"], r["offline"]) == (2, 2, 0)
    assert r["reimported"] is True and len(r["imported_clip_ids"]) == 2
    assert {(f["clip"], f["old"], f["new"]) for f in r["start_tc_fixes"]} == {
        ("A001.mov", "00:00:00:00", "01:00:00:00"),
        ("A002.mov", "00:00:00:00", "01:00:00:00"),
    }
    assert world.edl_imports == 2 and world.deleted == 1
    assert [t._name for t in world.project.timelines].count("Conform") == 1


async def test_conform_leaves_good_start_tc_alone(settings: Settings, tmp_path: Path) -> None:
    world = World()
    a, b = _media(tmp_path, "A001.mov", "A002.mov")
    world.add_clip(a, start_tc="01:00:00:00")
    world.add_clip(b, start_tc="01:00:00:00")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c,
            "conform_from_edl",
            media=[str(a), str(b)],
            edl=str(_edl(tmp_path)),
            timeline_name="Conform",
        )
    assert not err and out["receipt"]["verified"] is True
    r = out["receipt"]["result"]
    assert r["start_tc_fixes"] == [] and r["reimported"] is False and r["imported_clip_ids"] == []
    assert world.import_calls == [] and world.edl_imports == 1  # already in the pool


async def test_conform_imports_folders(settings: Settings, tmp_path: Path) -> None:
    world = World()
    _media(tmp_path, "A001.mov", "A002.mov", ".hidden.mov")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c,
            "conform_from_edl",
            media=[str(tmp_path / "media")],
            edl=str(_edl(tmp_path)),
            timeline_name="Conform",
        )
    assert not err and len(out["receipt"]["result"]["imported_clip_ids"]) == 2


async def test_conform_without_fix_reports_offline(settings: Settings, tmp_path: Path) -> None:
    world = World()
    a, b = _media(tmp_path, "A001.mov", "A002.mov")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c,
            "conform_from_edl",
            media=[str(a), str(b)],
            edl=str(_edl(tmp_path)),
            timeline_name="Conform",
            fix_start_tc=False,
        )
    assert not err
    receipt = out["receipt"]
    assert receipt["verified"] is False and receipt["result"]["offline"] == 2
    assert "offline" in receipt["verify_note"]


async def test_conform_unmatched_reels_are_reported(settings: Settings, tmp_path: Path) -> None:
    world = World()
    (a,) = _media(tmp_path, "OTHER.mov")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c, "conform_from_edl", media=[str(a)], edl=str(_edl(tmp_path)), timeline_name="Conform"
        )
    assert not err and out["receipt"]["verified"] is False
    assert any("reel" in w for w in out["receipt"]["result"]["warnings"])
    assert world.deleted == 0


async def test_conform_start_tc_that_does_not_stick_fails_before_deleting(
    settings: Settings, tmp_path: Path
) -> None:
    world = World()
    a, b = _media(tmp_path, "A001.mov", "A002.mov")
    for path in (a, b):
        world.add_clip(path).lie_start_tc = True
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c,
            "conform_from_edl",
            media=[str(a), str(b)],
            edl=str(_edl(tmp_path)),
            timeline_name="X",
        )
    assert err and out["code"] == "API_CALL_FAILED"
    assert world.deleted == 0


async def test_conform_validates_before_mutating(settings: Settings, tmp_path: Path) -> None:
    world = World()
    (a,) = _media(tmp_path, "A001.mov")
    outside = tmp_path.parent / "elsewhere.mov"
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        edl = str(_edl(tmp_path))
        err, out = await _write(
            c, "conform_from_edl", media=[str(outside)], edl=edl, timeline_name="X"
        )
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await _write(
            c, "conform_from_edl", media=[str(a)], edl=str(outside), timeline_name="X"
        )
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await _write(
            c,
            "conform_from_edl",
            media=[str(a)],
            edl=str(_edl(tmp_path, "TITLE: empty\n", "empty.edl")),
            timeline_name="X",
        )
        assert err and out["code"] == "INVALID_PARAMS"
        err, out = await _write(
            c, "conform_from_edl", media=[str(a)], edl=edl, timeline_name="Timeline 1"
        )
        assert err and out["code"] == "WRONG_STATE"
    assert world.import_calls == [] and world.edl_imports == 0


# -- deliver ---------------------------------------------------------------------------


def _deliver_args(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return {
        "target_dir": str(out),
        "outputs": [{"name": "master", "preset": "H.264 Master", "width": 1080, "height": 1920}],
        **extra,
    }


async def test_deliver_reports_the_real_mov_name(settings: Settings, tmp_path: Path) -> None:
    world = World()
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **_deliver_args(tmp_path))
    assert not err and out["receipt"]["verified"] is True
    (o,) = out["receipt"]["result"]["outputs"]
    assert o["path"] == str(tmp_path / "out" / "master.mov") and o["exists"] and o["fresh"]
    assert world.project.loaded == ["H.264 Master"]
    assert world.project.applied[0]["FormatWidth"] == 1080
    assert world.project.applied[0]["SelectAllFrames"] is True
    assert world.resolve.page == "edit"  # page restored after AddRenderJob/StartRendering


async def test_deliver_two_outputs_with_format_override(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World()
    monkeypatch.setattr(
        workflow, "_probe", lambda _p: {"codec_name": "h264", "width": 1280, "height": 720}
    )
    args = _deliver_args(tmp_path)
    args["outputs"] = [
        {"name": "master", "preset": "ProRes 422 HQ"},
        {"name": "proxy", "format": "mp4", "codec": "H264", "width": 1280, "height": 720},
    ]
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **args)
    assert not err and out["receipt"]["verified"] is True
    paths = [o["path"] for o in out["receipt"]["result"]["outputs"]]
    assert [Path(p).name for p in paths] == ["master.mov", "proxy.mp4"]
    assert out["receipt"]["result"]["outputs"][1]["codec"] == "h264"
    assert len(out["receipt"]["result"]["job_ids"]) == 2


async def test_deliver_checks_dimensions_with_ffprobe(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World()
    monkeypatch.setattr(workflow, "_probe", lambda _p: {"width": 1920, "height": 1080})
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **_deliver_args(tmp_path))
    assert not err and out["receipt"]["verified"] is False
    assert "width" in str(out["receipt"]["observed"])


async def test_deliver_without_wait_does_not_check_files(
    settings: Settings, tmp_path: Path
) -> None:
    world = World()
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **_deliver_args(tmp_path, wait=False))
    assert not err and out["receipt"]["verified"] is True
    assert out["receipt"]["result"]["outputs"] == [] and world.project.rendering


async def test_deliver_missing_file_fails_verification(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.project.skip_files = True
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **_deliver_args(tmp_path))
    assert not err and out["receipt"]["verified"] is False
    assert out["receipt"]["result"]["outputs"][0]["exists"] is False


async def test_deliver_stale_file_fails_verification(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.project.skip_files = True
    args = _deliver_args(tmp_path)
    old = tmp_path / "out" / "master.mov"
    old.write_bytes(b"old")
    os.utime(old, (1_000_000, 1_000_000))
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **args)
    assert not err and out["receipt"]["verified"] is False
    assert out["receipt"]["result"]["outputs"][0]["fresh"] is False


async def test_deliver_job_error_is_raised(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.project.errors["job1"] = "disk full"
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **_deliver_args(tmp_path))
    assert err and out["code"] == "API_CALL_FAILED" and "disk full" in out["message"]


async def test_deliver_timeout_is_reported_not_raised(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.project.stall = True
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "deliver", **_deliver_args(tmp_path, timeout_s=5))
    assert not err and out["receipt"]["result"]["timed_out"] is True
    assert out["receipt"]["verified"] is False


async def test_deliver_refusals(settings: Settings, tmp_path: Path) -> None:
    world = World()
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        world.project.rendering = True
        err, out = await _write(c, "deliver", **_deliver_args(tmp_path))
        assert err and out["code"] == "WRONG_STATE"
        world.project.rendering = False
        bad_preset = _deliver_args(tmp_path)
        bad_preset["outputs"] = [{"name": "x", "preset": "Nope"}]
        err, out = await _write(c, "deliver", **bad_preset)
        assert err and out["code"] == "NOT_FOUND"
        bad_format = _deliver_args(tmp_path)
        bad_format["outputs"] = [{"name": "x", "format": "mp4", "codec": "Nope"}]
        err, out = await _write(c, "deliver", **bad_format)
        assert err and out["code"] == "NOT_FOUND"
        err, out = await _write(c, "deliver", **{**_deliver_args(tmp_path), "target_dir": "/etc"})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        traversal = _deliver_args(tmp_path)
        traversal["outputs"] = [{"name": "../evil"}]
        err, _ = await _write(c, "deliver", **traversal)
        assert err
        dup = _deliver_args(tmp_path)
        dup["outputs"] = [{"name": "a"}, {"name": "a"}]
        err, out = await _write(c, "deliver", **dup)
        assert err and out["code"] == "INVALID_PARAMS"
    assert world.project.jobs == {}  # nothing was queued


# -- build_vertical_reel ---------------------------------------------------------------


def _reel_args(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    clips = _media(tmp_path, "one.mp4", "two.mp4", "three.mp4")
    return {"clips": [str(p) for p in clips], "timeline_name": "Reel", **extra}


async def test_reel_happy_path_with_subtitles_and_render(
    settings: Settings, tmp_path: Path
) -> None:
    world = World()
    args = _reel_args(tmp_path, render={"name": "reel", "preset": "H.264 Master"})
    args["target_dir"] = str(tmp_path / "out")
    (tmp_path / "out").mkdir()
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "build_vertical_reel", **args)
    assert not err and out["receipt"]["verified"] is True
    r = out["receipt"]["result"]
    assert (r["width"], r["height"], r["resolution_via"]) == (1080, 1920, "timeline")
    assert r["clip_order"] == ["one.mp4", "two.mp4", "three.mp4"]
    assert r["subtitle_count"] == 3 and len(r["imported_clip_ids"]) == 3
    assert Path(r["outputs"][0]["path"]).name == "reel.mov"
    reel = next(t for t in world.project.timelines if t._name == "Reel")
    assert reel.GetSetting("timelineResolutionWidth") == "1080"
    assert world.project._settings["timelineResolutionWidth"] == "1920"  # project untouched


async def test_reel_falls_back_to_project_resolution(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.timeline_settings_stick = False
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "build_vertical_reel", **_reel_args(tmp_path, subtitles=False))
    assert not err and out["receipt"]["verified"] is True
    r = out["receipt"]["result"]
    assert r["resolution_via"] == "project" and r["subtitle_count"] == 0
    assert any("PROJECT resolution" in w for w in r["warnings"])
    assert world.project._settings["timelineResolutionWidth"] == "1080"
    assert world.created.count("Reel") == 2 and world.deleted == 1


async def test_reel_extras_missing(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.subtitle_mode = "extras"
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "build_vertical_reel", **_reel_args(tmp_path))
    assert err and out["code"] == "EXTRAS_MISSING"


async def test_reel_subtitle_job_failure(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.subtitle_mode = "failed"
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "build_vertical_reel", **_reel_args(tmp_path))
    assert err and out["code"] == "API_CALL_FAILED"


async def test_reel_wrong_clip_order_fails_verification(settings: Settings, tmp_path: Path) -> None:
    world = World()
    world.reverse_append = True
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "build_vertical_reel", **_reel_args(tmp_path, subtitles=False))
    assert not err and out["receipt"]["verified"] is False
    assert out["receipt"]["observed"]["checks"]["clip_order"] is False


async def test_reel_validation(settings: Settings, tmp_path: Path) -> None:
    world = World()
    args = _reel_args(tmp_path, subtitles=False)
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, _ = await _write(c, "build_vertical_reel", **{**args, "render": {"name": "r"}})
        assert err  # render without target_dir
        err, out = await _write(c, "build_vertical_reel", **{**args, "clips": ["/etc/hosts"]})
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await _write(c, "build_vertical_reel", **{**args, "timeline_name": "Timeline 1"})
        assert err and out["code"] == "WRONG_STATE"
        world.project.rendering = True
        (tmp_path / "out").mkdir()
        err, out = await _write(
            c,
            "build_vertical_reel",
            **{**args, "render": {"name": "r"}, "target_dir": str(tmp_path / "out")},
        )
        assert err and out["code"] == "WRONG_STATE"
    assert world.import_calls == [] and world.created == []


# -- apply_look ------------------------------------------------------------------------


@pytest.fixture
def lut_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "LUT"
    monkeypatch.setattr(color, "lut_root", lambda: root)  # never touch Resolve's real folder
    return root


def _look_world() -> World:
    world = World()
    world.add_timeline("Look", clips=3, color=True)
    return world


async def test_apply_look_lut_group_and_still(
    settings: Settings, tmp_path: Path, lut_root: Path
) -> None:
    world = _look_world()
    lut = tmp_path / "film.cube"
    lut.write_text("LUT")
    tl = world.project._current
    saved_tc = tl.timecode  # type: ignore[union-attr]
    world.resolve.page = "edit"
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c, "apply_look", lut=str(lut), node=2, color_group="Show", grab_still=True
        )
    assert not err and out["receipt"]["verified"] is True
    r = out["receipt"]["result"]
    assert r["lut_reference"] == "MCP/film.cube" and len(r["items"]) == 3
    assert all(i["group"] == "Show" and i["lut"] == "MCP/film.cube" for i in r["items"])
    assert r["stills_after"] == r["stills_before"] + 1
    assert (lut_root / "MCP" / "film.cube").is_file()
    assert tl.timecode == saved_tc  # type: ignore[union-attr]
    assert world.resolve.page == "edit"
    assert all(len(g.members) == 3 for g in world.project.groups)  # type: ignore[attr-defined]


async def test_apply_look_reuses_existing_group(
    settings: Settings, tmp_path: Path, lut_root: Path
) -> None:
    world = _look_world()
    existing = world.project.AddColorGroup("Show")  # type: ignore[attr-defined]
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "apply_look", color_group="Show")
    assert not err and out["receipt"]["verified"] is True
    assert len(world.project.groups) == 1 and len(existing.members) == 3  # type: ignore[attr-defined]


async def test_apply_look_detects_lut_that_does_not_stick(
    settings: Settings, tmp_path: Path, lut_root: Path
) -> None:
    world = _look_world()
    lut = tmp_path / "film.cube"
    lut.write_text("LUT")
    tl = world.project._current
    tl.tracks[0][1].GetNodeGraph().lie = True  # type: ignore[union-attr]
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "apply_look", lut=str(lut))
    assert not err and out["receipt"]["verified"] is False
    assert "shot2" in str(out["receipt"]["observed"])


async def test_apply_look_refusals(settings: Settings, tmp_path: Path, lut_root: Path) -> None:
    world = _look_world()
    bad = tmp_path / "evil.dctl"
    bad.write_text("x")
    lut = tmp_path / "ok.cube"
    lut.write_text("x")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, _ = await _write(c, "apply_look")
        assert err  # nothing to do
        err, out = await _write(c, "apply_look", lut="/etc/hosts")
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await _write(c, "apply_look", lut=str(bad))
        assert err and out["code"] == "INVALID_PARAMS"
        err, out = await _write(c, "apply_look", lut=str(lut), node=9)
        assert err and out["code"] == "INVALID_PARAMS"
    assert not (lut_root / "MCP").exists()  # failed before installing anything


# -- title_overlay ---------------------------------------------------------------------


def _title_world(*, existing: bool = False, media_io: bool = True) -> tuple[World, WfItem]:
    world = World()
    tl = world.add_timeline("Edit", clips=1)
    item = tl.tracks[0][0]
    world.add_fusion(cast("Any", item), existing=existing, media_io=media_io)
    return world, item  # type: ignore[return-value]


async def test_title_overlay_creates_comp_and_animates_size(settings: Settings) -> None:
    world, item = _title_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c,
            "title_overlay",
            item={"id": item.GetUniqueId()},
            text="HITSCROLL",
            font="Open Sans",
            size_from=0.0,
            size_to=0.12,
            frames=30,
        )
    assert not err and out["receipt"]["verified"] is True
    r = out["receipt"]["result"]
    assert (r["text_tool"], r["merge_tool"]) == ("MCP_Title", "MCP_TitleMerge")
    assert r["keyframes"] == {"0": 0.0, "30": 0.12}
    comp = world.comps[0]
    text, merge = comp.FindTool("MCP_Title"), comp.FindTool("MCP_TitleMerge")
    assert text.values["StyledText"] == "HITSCROLL" and text.inputs["Size"].animated
    assert merge.inputs["Foreground"].source.tool is text
    assert merge.inputs["Background"].source.tool is comp.FindTool("MediaIn1")
    assert comp.FindTool("MediaOut1").inputs["Input"].source.tool is merge
    assert comp.undo_log == ["MCP title overlay"] and comp.open_undo == 0 and comp.lock_depth == 0


async def test_title_overlay_keeps_existing_chain_as_background(settings: Settings) -> None:
    world, item = _title_world(existing=True)
    comp = world.comps[0]
    blur = comp.make_tool("Background", "Look")
    comp.FindTool("MediaOut1").inputs["Input"].source = blur.FindMainOutput(1)
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "title_overlay", item={"id": item.GetUniqueId()}, text="Hi")
    assert not err and out["receipt"]["verified"] is True
    assert comp.FindTool("MCP_TitleMerge").inputs["Background"].source.tool is blur
    assert len(world.comps) == 1  # the existing comp was reused


async def test_title_overlay_without_merge_and_with_name_clash(settings: Settings) -> None:
    world, item = _title_world(existing=True)
    comp = world.comps[0]
    comp.make_tool("TextPlus", "MCP_Title")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(
            c, "title_overlay", item={"id": item.GetUniqueId()}, text="Hi", merge=False
        )
    assert not err and out["receipt"]["verified"] is True
    r = out["receipt"]["result"]
    assert r["text_tool"] == "MCP_Title2" and r["merge_tool"] is None
    assert comp.FindTool("MediaOut1").inputs["Input"].source is None


async def test_title_overlay_detects_unapplied_text(settings: Settings) -> None:
    world, item = _title_world(existing=True)
    world.comps[0].lie_inputs.add("StyledText")
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "title_overlay", item={"id": item.GetUniqueId()}, text="Hi")
    assert not err and out["receipt"]["verified"] is False
    assert out["receipt"]["observed"]["checks"]["styled_text"] is False


async def test_title_overlay_errors(settings: Settings) -> None:
    world, item = _title_world(existing=True, media_io=False)
    async with mcp_client(world.resolve, settings, ACTIONS) as c:
        err, out = await _write(c, "title_overlay", item={"id": "nope"}, text="Hi")
        assert err and out["code"] == "NOT_FOUND"
        err, out = await _write(c, "title_overlay", item={"id": item.GetUniqueId()}, text="Hi")
        assert err and out["code"] == "WRONG_STATE"  # no MediaIn/MediaOut for the merge
        err, _ = await _write(c, "title_overlay", item={"id": item.GetUniqueId()}, text="")
        assert err
    assert world.comps[0].tools == []  # nothing was added


def test_actions_are_all_writes() -> None:
    assert {a.name for a in ACTIONS} == {
        "conform_from_edl",
        "deliver",
        "build_vertical_reel",
        "apply_look",
        "title_overlay",
    }
    assert all(a.tool_name == "workflow_write" and not a.long_running for a in ACTIONS)


def test_folder_expansion_skips_symlinks(tmp_path, monkeypatch) -> None:
    inside = tmp_path / "media"
    inside.mkdir()
    (inside / "a.mov").write_text("x")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.mov"
    outside.write_text("secret")
    (inside / "link.mov").symlink_to(outside)

    monkeypatch.setattr(workflow, "runtime", lambda: SimpleNamespace(allowed_roots=[inside]))
    files = workflow._expand_media([str(inside)])
    assert [f.name for f in files] == ["a.mov"]
