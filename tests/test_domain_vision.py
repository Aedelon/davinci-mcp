# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Vision domain: fake subprocess runner and fake Resolve, plus one real ffmpeg test."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.domains import vision
from davinci_mcp.domains.vision import ACTIONS
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, mcp_client


def _jpeg(width: int = 320, height: int = 180) -> bytes:
    sof = b"\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9"


def _png(width: int = 1920, height: int = 1080) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    )


class FakeRunner:
    """Records commands and fabricates the files the real tools would write."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.fail_drawtext = False
        self.fail_all = False
        self.window = "4242"
        self.probe: dict[str, Any] = {}

    def __call__(
        self, cmd: list[str], timeout_s: float = 60.0
    ) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(cmd)
        tool = cmd[0]
        if self.fail_all:
            return subprocess.CompletedProcess(cmd, 1, b"", b"boom")
        if tool == "sips":
            Path(cmd[cmd.index("--out") + 1]).write_bytes(_jpeg())
        elif tool == "ffmpeg":
            if self.fail_drawtext and "drawtext" in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, 1, b"", b"No such filter: drawtext")
            Path(cmd[-1]).write_bytes(_jpeg(960, 540))
        elif tool == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.probe).encode(), b"")
        elif tool == "osascript":
            return subprocess.CompletedProcess(cmd, 0, self.window.encode(), b"")
        elif tool == "screencapture":
            Path(cmd[-1]).write_bytes(_png())
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake = FakeRunner()
    monkeypatch.setattr(vision, "_run", fake)
    monkeypatch.setattr(vision, "_have", lambda _tool: True)
    monkeypatch.setattr(vision.sys, "platform", "darwin")
    return fake


class Playhead:
    """Fake timeline + project API surface used by the vision domain."""

    def __init__(self, resolve: Any, fps: str = "24", drop: str = "0") -> None:
        self.project = resolve.project_manager._current
        self.timeline = self.project.timelines[0]
        self.timecode = "01:00:00:00"
        self.history: list[str] = []
        self.export_ok = True
        self.set_ok = True
        self.exports: list[str] = []
        self.markers: dict[int, dict[str, Any]] = {}
        t = self.timeline
        t.GetCurrentTimecode = lambda: self.timecode
        t.SetCurrentTimecode = self._set
        t.GetSetting = {"timelineFrameRate": fps, "timelineDropFrameTimecode": drop}.get
        t.GetStartFrame = lambda: 86400
        t.GetEndFrame = lambda: 86400 + 240
        t.GetMarkers = lambda: self.markers
        self.project.ExportCurrentFrameAsStill = self._export

    def _set(self, tc: str) -> bool:
        self.history.append(tc)
        if not self.set_ok and tc != "01:00:00:00":
            return False
        self.timecode = tc
        return True

    def _export(self, path: str) -> bool:
        self.exports.append(self.timecode)
        if self.export_ok:
            Path(path).write_bytes(_png())
        return self.export_ok


def _data(result: Any) -> dict[str, Any]:
    assert result.structured_content is not None
    return result.structured_content["result"]


async def _raw(settings, resolve, tool: str, op: dict[str, Any]):
    async with mcp_client(resolve, settings, ACTIONS) as client:
        result = await client.call_tool(tool, {"op": op}, raise_on_error=False)
        return result


async def _call(settings, resolve, op: dict[str, Any]):
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "vision_read", {"op": op})
        return err, out.get("result", out)


# -- export_frame -------------------------------------------------------------------


async def test_export_frame_restores_playhead_and_returns_image(settings, runner) -> None:
    resolve = make_resolve()
    head = Playhead(resolve)
    result = await _raw(
        settings, resolve, "vision_read", {"action": "export_frame", "timecode": "01:00:03:00"}
    )
    assert not result.is_error
    data = _data(result)
    assert data["timecode"] == "01:00:03:00"
    assert head.exports == ["01:00:03:00"]
    assert head.timecode == "01:00:00:00"  # restored
    assert head.history == ["01:00:03:00", "01:00:00:00"]
    assert any(c.type == "image" for c in result.content)
    assert Path(data["path"]).is_file()
    assert data["images"][0]["width"] == 320
    assert not list((settings.state_dir / "frames").glob("*.png"))  # raw still removed
    sips = next(c for c in runner.commands if c[0] == "sips")
    assert "--resampleWidth" in sips  # 1920 > 1280


async def test_export_frame_current_playhead_no_resample_when_small(
    settings, runner, monkeypatch
) -> None:
    resolve = make_resolve()
    head = Playhead(resolve)
    monkeypatch.setattr(
        head, "_export", lambda path: Path(path).write_bytes(_png(800, 450)) or True
    )
    head.project.ExportCurrentFrameAsStill = head._export
    err, out = await _call(settings, resolve, {"action": "export_frame"})
    assert not err, out
    sips = next(c for c in runner.commands if c[0] == "sips")
    assert "--resampleWidth" not in sips
    assert head.history == ["01:00:00:00"]  # only the restore


async def test_export_frame_failures_restore_playhead(settings, runner) -> None:
    resolve = make_resolve()
    head = Playhead(resolve)
    head.set_ok = False
    err, _ = await _call(settings, resolve, {"action": "export_frame", "timecode": "09:00:00:00"})
    assert err
    assert head.timecode == "01:00:00:00"
    head.set_ok = True
    head.export_ok = False
    err, _ = await _call(settings, resolve, {"action": "export_frame", "timecode": "01:00:02:00"})
    assert err
    assert head.timecode == "01:00:00:00"
    assert not list((settings.state_dir / "frames").glob("*.png"))


async def test_old_temp_files_are_pruned(settings, runner) -> None:
    resolve = make_resolve()
    Playhead(resolve)
    frames = settings.state_dir / "frames"
    frames.mkdir(parents=True)
    old, fresh = frames / "old.jpg", frames / "fresh.jpg"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    past = time.time() - 2 * 86400
    os.utime(old, (past, past))
    err, _ = await _call(settings, resolve, {"action": "export_frame"})
    assert not err
    assert not old.exists()
    assert fresh.exists()


# -- contact_sheet ------------------------------------------------------------------


async def test_contact_sheet_even_sampling_and_labels(settings, runner, monkeypatch) -> None:
    monkeypatch.setattr(vision, "_font", lambda: "/fonts/Test.ttf")  # host-independent
    resolve = make_resolve()
    head = Playhead(resolve)
    result = await _raw(settings, resolve, "vision_read", {"action": "contact_sheet", "count": 4})
    assert not result.is_error, result.content
    data = _data(result)
    assert data["timecodes"] == ["01:00:01:06", "01:00:03:18", "01:00:06:06", "01:00:08:18"]
    assert (data["columns"], data["rows"], data["labeled"]) == (2, 2, True)
    assert head.exports == data["timecodes"]
    assert head.timecode == "01:00:00:00"
    tile = next(c for c in runner.commands if c[0] == "ffmpeg")
    vf = tile[tile.index("-vf") + 1]
    assert "tile=2x2" in vf
    assert "drawtext" in vf
    assert any(c.type == "image" for c in result.content)
    assert not list((settings.state_dir / "frames").glob("sheet_*/"))  # work dir removed


async def test_contact_sheet_markers_explicit_and_drawtext_fallback(settings, runner) -> None:
    resolve = make_resolve()
    head = Playhead(resolve, fps="29.97", drop="1")
    head.markers = {48: {"name": "a"}, 12: {"name": "b"}}
    runner.fail_drawtext = True
    err, out = await _call(settings, resolve, {"action": "contact_sheet", "from_markers": True})
    assert not err, out
    assert len(out["timecodes"]) == 2
    assert all(";" in tc for tc in out["timecodes"])
    assert out["labeled"] is False  # retried without drawtext
    err, out = await _call(
        settings,
        resolve,
        {"action": "contact_sheet", "timecodes": ["01:00:00;02", "01:01:00;02"], "columns": 1},
    )
    assert not err, out
    assert (out["columns"], out["rows"]) == (1, 2)
    err, _ = await _call(
        settings, resolve, {"action": "contact_sheet", "timecodes": ["01:01:00;00"]}
    )
    assert err  # dropped label in drop-frame
    head.markers = {}
    err, _ = await _call(settings, resolve, {"action": "contact_sheet", "from_markers": True})
    assert err
    err, _ = await _call(
        settings,
        resolve,
        {"action": "contact_sheet", "from_markers": True, "timecodes": ["01:00:00;02"]},
    )
    assert err


# -- probe_media --------------------------------------------------------------------

PROBE = {
    "format": {
        "format_name": "mov,mp4",
        "duration": "12.5",
        "size": "1000",
        "bit_rate": "640",
        "tags": {"timecode": "01:00:00:00"},
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "prores",
            "profile": "HQ",
            "width": 3840,
            "height": 2160,
            "avg_frame_rate": "30000/1001",
            "pix_fmt": "yuv422p10le",
            "bits_per_raw_sample": "10",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "color_range": "tv",
            "nb_frames": "375",
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "pcm_s24le",
            "channels": 2,
            "channel_layout": "stereo",
            "sample_rate": "48000",
            "tags": {"language": "eng"},
        },
        {"index": 2, "codec_type": "data"},
    ],
}


async def test_probe_media_path_and_clip(settings, runner, tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "a.mov"
    media.write_bytes(b"x")
    runner.probe = PROBE
    resolve = make_resolve()
    err, out = await _call(settings, resolve, {"action": "probe_media", "path": str(media)})
    assert not err, out
    video = out["video"][0]
    assert (video["codec"], video["width"], video["fps"], video["color_space"]) == (
        "prores",
        3840,
        29.97,
        "bt709",
    )
    assert video["bit_depth"] == 10
    assert out["audio"][0]["sample_rate"] == 48000
    assert out["audio"][0]["language"] == "eng"
    assert out["other_streams"] == 1
    assert out["timecode"] == "01:00:00:00"
    assert out["duration_s"] == 12.5

    class Clip:
        def GetClipProperty(self, name: str) -> str:  # noqa: N802
            return str(media)

    monkeypatch.setattr(vision, "resolve_clip", lambda _s, _ref: Clip())
    err, out = await _call(settings, resolve, {"action": "probe_media", "clip": {"name": "a"}})
    assert not err, out
    assert out["path"] == str(media)


async def test_probe_media_errors(settings, runner, tmp_path: Path) -> None:
    resolve = make_resolve()
    err, _ = await _call(
        settings, resolve, {"action": "probe_media", "path": str(tmp_path / "nope.mov")}
    )
    assert err
    err, _ = await _call(settings, resolve, {"action": "probe_media", "path": "/etc/hosts"})
    assert err  # outside allowed roots
    err, _ = await _call(settings, resolve, {"action": "probe_media"})
    assert err  # neither path nor clip
    media = tmp_path / "a.mov"
    media.write_bytes(b"x")
    runner.fail_all = True
    err, _ = await _call(settings, resolve, {"action": "probe_media", "path": str(media)})
    assert err


# -- source_frames ------------------------------------------------------------------


async def test_source_frames_fake(settings, runner, tmp_path: Path) -> None:
    media = tmp_path / "a.mov"
    media.write_bytes(b"x")
    result = await _raw(
        settings,
        make_resolve(),
        "vision_read",
        {"action": "source_frames", "path": str(media), "seconds": [0, 1.5]},
    )
    assert not result.is_error, result.content
    assert sum(c.type == "image" for c in result.content) == 2
    cmds = [c for c in runner.commands if c[0] == "ffmpeg"]
    assert [c[c.index("-ss") + 1] for c in cmds] == ["0.000", "1.500"]
    runner.fail_all = True
    err, _ = await _call(
        settings, make_resolve(), {"action": "source_frames", "path": str(media), "seconds": [99]}
    )
    assert err
    err, _ = await _call(
        settings, make_resolve(), {"action": "source_frames", "path": str(media), "seconds": [-1]}
    )
    assert err
    err, _ = await _call(
        settings, make_resolve(), {"action": "source_frames", "path": str(media), "seconds": []}
    )
    assert err


# -- screenshot_resolve -------------------------------------------------------------


async def test_screenshot_resolve(settings, runner, monkeypatch) -> None:
    resolve = make_resolve()
    result = await _raw(settings, resolve, "vision_read", {"action": "screenshot_resolve"})
    assert not result.is_error, result.content
    assert _data(result)["window_id"] == 4242
    shot = next(c for c in runner.commands if c[0] == "screencapture")
    assert shot[:5] == ["screencapture", "-x", "-o", "-l", "4242"]
    assert any(c.type == "image" for c in result.content)

    runner.window = ""
    err, _ = await _call(settings, resolve, {"action": "screenshot_resolve"})
    assert err
    runner.window = "4242"

    runner.fail_all = True
    runner.window = "4242"
    err, _ = await _call(settings, resolve, {"action": "screenshot_resolve"})
    assert err

    monkeypatch.setattr(vision.sys, "platform", "linux")
    err, _ = await _call(settings, resolve, {"action": "screenshot_resolve"})
    assert err


async def test_screenshot_without_permission_gives_hint(settings, runner, monkeypatch) -> None:
    class Denied(FakeRunner):
        def __call__(self, cmd, timeout_s=60.0):
            if cmd[0] == "screencapture":
                return subprocess.CompletedProcess(cmd, 1, b"", b"denied")
            return super().__call__(cmd, timeout_s)

    monkeypatch.setattr(vision, "_run", Denied())
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        result = await client.call_tool(
            "vision_read", {"op": {"action": "screenshot_resolve"}}, raise_on_error=False
        )
    assert result.is_error
    assert "Screen Recording" in json.dumps(result.structured_content)


# -- real ffmpeg --------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_real_ffmpeg_source_frames_and_probe(settings, tmp_path: Path) -> None:
    clip = tmp_path / "gen.mp4"
    proc = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "testsrc=duration=1:size=640x360:rate=24", "-pix_fmt", "yuv420p", str(clip),
        ],
        check=False, capture_output=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    resolve = make_resolve()
    result = await _raw(
        settings,
        resolve,
        "vision_read",
        {"action": "source_frames", "path": str(clip), "seconds": [0.0, 0.5], "max_width": 320},
    )
    assert not result.is_error, result.content
    images = _data(result)["images"]
    assert [(i["width"], i["height"]) for i in images] == [(320, 180)] * 2
    assert sum(c.type == "image" for c in result.content) == 2
    err, out = await _call(settings, resolve, {"action": "probe_media", "path": str(clip)})
    assert not err, out
    assert (out["video"][0]["codec"], out["video"][0]["width"], out["video"][0]["fps"]) == (
        "h264",
        640,
        24.0,
    )
    assert 0.9 < out["duration_s"] < 1.2
