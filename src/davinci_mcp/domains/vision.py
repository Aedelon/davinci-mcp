# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: PLR2004
"""Vision domain: let the agent see frames, contact sheets, source media and the Resolve UI.

Every action is a READ: none of them changes the project. ``export_frame`` and
``contact_sheet`` move the playhead to grab pixels and put it back before returning, and
write only temporary images under ``<state_dir>/frames`` (pruned after one day).
Frames come from ``Project.ExportCurrentFrameAsStill`` (real pixels at the playhead);
``Timeline.GetCurrentClipThumbnailImage`` is deliberately not used: it only works on the
Color page with Resolve in the foreground and returns a clip-level thumbnail, not the frame.

External tools are called through ``_run`` (a single module-level seam that tests replace).
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import Kind, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.domains.interchange import frames_to_timecode, timecode_to_frames
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.images import ImageFile
from davinci_mcp.refs import ClipRef, resolve_clip
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "vision"
PRUNE_AFTER_S = 24 * 3600
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
RESOLVE_APP = "DaVinci Resolve"
_WINDOW_ID_JXA = """
ObjC.import("CoreGraphics");
const info = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(
  $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, 0)));
let best = null;
for (const w of info) {
  if (w.kCGWindowOwnerName !== "%s" || w.kCGWindowLayer !== 0) continue;
  const area = w.kCGWindowBounds.Width * w.kCGWindowBounds.Height;
  if (best === null || area > best.area) best = {id: w.kCGWindowNumber, area: area};
}
best === null ? "" : String(best.id);
"""


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- subprocess seam ---------------------------------------------------------------


def _run(cmd: list[str], timeout_s: float = 60.0) -> subprocess.CompletedProcess[bytes]:
    """Run an external tool without a shell. Tests replace this function."""
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise DavinciError(
            ErrorCode.EXTRAS_MISSING, f"{cmd[0]} is not installed.", retryable=False
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DavinciError(
            ErrorCode.TIMEOUT, f"{cmd[0]} timed out after {timeout_s:.0f}s.", retryable=True
        ) from exc


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _need(tool: str) -> None:
    if not _have(tool):
        raise DavinciError(
            ErrorCode.EXTRAS_MISSING,
            f"{tool} was not found on PATH.",
            hint="Install it with `brew install ffmpeg`.",
            retryable=False,
        )


# -- temp files --------------------------------------------------------------------


def _frames_dir() -> Path:
    directory = runtime().settings.state_dir / "frames"
    directory.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - PRUNE_AFTER_S
    for entry in directory.rglob("*"):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
    for sub in sorted(directory.glob("*/"), reverse=True):
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()
    return directory


def _stamp() -> str:
    return f"{time.time_ns() // 1_000_000}"


def _png_width(path: Path) -> int | None:
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">I", head[16:20])[0]


def _jpeg_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return width, height
        pos += 2 + length
    return None


def _to_jpeg(src: Path, dst: Path, max_width: int) -> None:
    """Convert ``src`` to a JPEG no wider than ``max_width`` (sips on macOS, else ffmpeg)."""
    if sys.platform == "darwin" and _have("sips"):
        width = _png_width(src)
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80"]
        if width is None or width > max_width:
            cmd += ["--resampleWidth", str(max_width)]
        proc = _run([*cmd, str(src), "--out", str(dst)])
    else:
        _need("ffmpeg")
        proc = _run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(src),
                "-vf",
                f"scale='min({max_width},iw)':-2",
                "-q:v",
                "3",
                str(dst),
            ]
        )
    if proc.returncode != 0 or not dst.is_file():
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Could not convert the captured frame to JPEG.",
            details={"stderr": proc.stderr.decode("utf-8", errors="replace")[-300:]},
            retryable=False,
        )


def _image(path: Path, caption: str | None = None) -> ImageFile:
    size = _jpeg_size(path) if path.is_file() else None
    return ImageFile(
        path=str(path),
        mime="image/jpeg",
        width=size[0] if size else None,
        height=size[1] if size else None,
        caption=caption,
    )


# -- Resolve playhead --------------------------------------------------------------


@contextmanager
def _playhead(s: ResolveSession) -> Generator[ResolveObject]:
    """Yield the current timeline and always put the playhead back."""
    timeline = s.timeline
    saved = timeline.GetCurrentTimecode()
    try:
        yield timeline
    finally:
        if saved:
            timeline.SetCurrentTimecode(saved)


def _grab(s: ResolveSession, timeline: ResolveObject, timecode: str | None, raw: Path) -> str:
    """Move to ``timecode`` (if any) and export the current frame; return the actual timecode."""
    if timecode is not None and not timeline.SetCurrentTimecode(timecode):
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            f"Timeline.SetCurrentTimecode({timecode!r}) returned False.",
            hint="The timecode may be outside the timeline; check its start timecode.",
            retryable=False,
        )
    actual = str(timeline.GetCurrentTimecode() or timecode or "")
    if not s.project.ExportCurrentFrameAsStill(str(raw)):
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Project.ExportCurrentFrameAsStill returned False.",
            hint="Resolve must be able to render the frame (media online, project open).",
            retryable=True,
        )
    deadline = time.monotonic() + 3.0
    while not raw.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not raw.is_file():
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Resolve reported success but wrote no still file.",
            details={"expected": str(raw)},
            retryable=True,
        )
    return actual


# =====================================================================================
# export_frame
# =====================================================================================


class ExportFrameParams(_Params):
    timecode: str | None = Field(
        default=None, description="Timeline timecode to capture; omit for the current playhead."
    )
    max_width: int = Field(default=1280, ge=64, le=3840)


class FrameResult(BaseModel):
    path: str
    timecode: str
    images: list[ImageFile]


@action(domain=DOMAIN, kind=Kind.READ, params=ExportFrameParams, result=FrameResult)
def export_frame(s: ResolveSession, p: ExportFrameParams) -> FrameResult:
    """Capture the timeline frame at a timecode as a JPEG (playhead is restored)."""
    # READ because Resolve state is unchanged: the playhead moves only transiently and is
    # restored, and the only artifact is a temp image in state_dir.
    out_dir = _frames_dir()
    stamp = _stamp()
    raw = out_dir / f"frame_{stamp}.png"
    dst = out_dir / f"frame_{stamp}.jpg"
    try:
        with _playhead(s) as timeline:
            actual = _grab(s, timeline, p.timecode, raw)
        _to_jpeg(raw, dst, p.max_width)
    finally:
        raw.unlink(missing_ok=True)
    return FrameResult(
        path=str(dst), timecode=actual, images=[_image(dst, f"Timeline frame at {actual}")]
    )


# =====================================================================================
# contact_sheet
# =====================================================================================


class ContactSheetParams(_Params):
    count: int = Field(default=9, ge=2, le=36, description="Frames sampled evenly.")
    timecodes: list[str] | None = Field(
        default=None, max_length=36, description="Explicit timecodes (overrides count)."
    )
    from_markers: bool = Field(default=False, description="Use timeline markers as sample points.")
    columns: int | None = Field(default=None, ge=1, le=12)
    thumb_width: int = Field(default=320, ge=64, le=960)
    labels: bool = Field(default=True, description="Burn the timecode on each tile.")

    @model_validator(mode="after")
    def _one_source(self) -> ContactSheetParams:
        if self.timecodes and self.from_markers:
            raise ValueError("use only one of timecodes or from_markers")
        return self


class SheetResult(BaseModel):
    path: str
    timecodes: list[str]
    columns: int
    rows: int
    labeled: bool
    images: list[ImageFile]


def _timeline_fps(s: ResolveSession, timeline: ResolveObject) -> tuple[float, bool]:
    raw = timeline.GetSetting("timelineFrameRate") or s.project.GetSetting("timelineFrameRate")
    try:
        fps = float(str(raw).split()[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"Cannot read the timeline frame rate ({raw!r}).", retryable=True
        ) from exc
    drop = str(timeline.GetSetting("timelineDropFrameTimecode")) in ("1", "True", "true")
    return fps, drop and abs(fps - round(fps)) > 0.001


def _sample_timecodes(
    s: ResolveSession, timeline: ResolveObject, p: ContactSheetParams
) -> tuple[list[str], float, bool]:
    fps, drop = _timeline_fps(s, timeline)
    if p.timecodes:
        for tc in p.timecodes:
            timecode_to_frames(tc, fps, drop)  # validates the format early
        return list(p.timecodes), fps, drop
    start = int(timeline.GetStartFrame())
    end = int(timeline.GetEndFrame())  # exclusive
    if p.from_markers:
        markers: dict[Any, Any] = timeline.GetMarkers() or {}
        frames = sorted(start + int(f) for f in markers)[:36]
        if not frames:
            raise DavinciError(
                ErrorCode.NOT_FOUND, "The timeline has no markers to sample.", retryable=False
            )
    else:
        span = max(end - start, 1)
        frames = [start + min(int(span * (i + 0.5) / p.count), span - 1) for i in range(p.count)]
    return [frames_to_timecode(f, fps, drop) for f in frames], fps, drop


def _font() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).is_file()), None)


def _tile_command(
    work: Path, dst: Path, grid: tuple[int, int], width: int, labels: list[str] | None
) -> list[str]:
    cols, rows = grid
    filters = [f"scale={width}:-2"]
    font = _font()
    if labels is not None and font is not None:
        for i, label in enumerate(labels):
            filters.append(
                f"drawtext=fontfile='{font}':text='{label}':x=6:y=h-th-6:fontsize=18:"
                f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4:enable='eq(n,{i})'"
            )
    filters.append(f"tile={cols}x{rows}:padding=4:margin=4:color=black")
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-framerate",
        "1",
        "-i",
        str(work / "f%03d.jpg"),
        "-vf",
        ",".join(filters),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(dst),
    ]


@action(domain=DOMAIN, kind=Kind.READ, params=ContactSheetParams, result=SheetResult)
def contact_sheet(s: ResolveSession, p: ContactSheetParams) -> SheetResult:
    """Tile frames sampled across the timeline (or at timecodes / markers) into one JPEG."""
    # READ for the same reason as export_frame: the playhead is restored, output is a temp file.
    _need("ffmpeg")
    out_dir = _frames_dir()
    stamp = _stamp()
    work = out_dir / f"sheet_{stamp}"
    work.mkdir()
    dst = out_dir / f"sheet_{stamp}.jpg"
    try:
        with _playhead(s) as timeline:
            timecodes, _, _ = _sample_timecodes(s, timeline, p)
            for i, tc in enumerate(timecodes, start=1):
                raw = work / f"raw{i:03d}.png"
                _grab(s, timeline, tc, raw)
                _to_jpeg(raw, work / f"f{i:03d}.jpg", p.thumb_width)
                raw.unlink(missing_ok=True)
        n = len(timecodes)
        cols = min(p.columns or math.ceil(math.sqrt(n)), n)
        rows = math.ceil(n / cols)
        labels = [tc.replace(":", "\\:").replace(";", "\\;") for tc in timecodes]
        labeled = p.labels and _font() is not None
        proc = _run(
            _tile_command(work, dst, (cols, rows), p.thumb_width, labels if labeled else None),
            timeout_s=120.0,
        )
        if (proc.returncode != 0 or not dst.is_file()) and labeled:
            labeled = False  # drawtext can be missing from some ffmpeg builds: retry plain
            proc = _run(_tile_command(work, dst, (cols, rows), p.thumb_width, None), 120.0)
        if proc.returncode != 0 or not dst.is_file():
            raise DavinciError(
                ErrorCode.API_CALL_FAILED,
                "ffmpeg could not build the contact sheet.",
                details={"stderr": proc.stderr.decode("utf-8", errors="replace")[-300:]},
                retryable=False,
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return SheetResult(
        path=str(dst),
        timecodes=timecodes,
        columns=cols,
        rows=rows,
        labeled=labeled,
        images=[
            _image(dst, f"{n} frames, left to right then top to bottom: {', '.join(timecodes)}")
        ],
    )


# =====================================================================================
# probe_media
# =====================================================================================


class ProbeMediaParams(_Params):
    path: str | None = None
    clip: ClipRef | None = Field(default=None, description="Media Pool clip (uses its File Path).")

    @model_validator(mode="after")
    def _one(self) -> ProbeMediaParams:
        if (self.path is None) == (self.clip is None):
            raise ValueError("use exactly one of path or clip")
        return self


class VideoStream(BaseModel):
    index: int
    codec: str | None
    profile: str | None
    width: int | None
    height: int | None
    fps: float | None
    pix_fmt: str | None
    bit_depth: int | None
    field_order: str | None
    color_space: str | None
    color_transfer: str | None
    color_primaries: str | None
    color_range: str | None
    frames: int | None
    timecode: str | None


class AudioStream(BaseModel):
    index: int
    codec: str | None
    channels: int | None
    layout: str | None
    sample_rate: int | None
    bit_rate: int | None
    language: str | None


class MediaProbe(BaseModel):
    path: str
    container: str | None
    duration_s: float | None
    size_bytes: int | None
    bit_rate: int | None
    video: list[VideoStream]
    audio: list[AudioStream]
    other_streams: int
    timecode: str | None


def _num(value: object) -> float | None:
    try:
        return float(cast("Any", value))
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _rate(value: object) -> float | None:
    text = str(value or "")
    if "/" in text:
        num, _, den = text.partition("/")
        n, d = _num(num), _num(den)
        return round(n / d, 3) if n is not None and d else None
    return _num(text)


def _tags(obj: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in cast("dict[str, Any]", obj.get("tags") or {}).items()}


def summarize_probe(path: str, data: dict[str, Any]) -> MediaProbe:
    fmt = cast("dict[str, Any]", data.get("format") or {})
    video: list[VideoStream] = []
    audio: list[AudioStream] = []
    other = 0
    for st in cast("list[dict[str, Any]]", data.get("streams") or []):
        kind = st.get("codec_type")
        tags = _tags(st)
        if kind == "video" and st.get("disposition", {}).get("attached_pic", 0) != 1:
            video.append(
                VideoStream(
                    index=int(st.get("index", 0)),
                    codec=st.get("codec_name"),
                    profile=st.get("profile"),
                    width=_int(st.get("width")),
                    height=_int(st.get("height")),
                    fps=_rate(st.get("avg_frame_rate")) or _rate(st.get("r_frame_rate")),
                    pix_fmt=st.get("pix_fmt"),
                    bit_depth=_int(st.get("bits_per_raw_sample")),
                    field_order=st.get("field_order"),
                    color_space=st.get("color_space"),
                    color_transfer=st.get("color_transfer"),
                    color_primaries=st.get("color_primaries"),
                    color_range=st.get("color_range"),
                    frames=_int(st.get("nb_frames")),
                    timecode=tags.get("timecode"),
                )
            )
        elif kind == "audio":
            audio.append(
                AudioStream(
                    index=int(st.get("index", 0)),
                    codec=st.get("codec_name"),
                    channels=_int(st.get("channels")),
                    layout=st.get("channel_layout"),
                    sample_rate=_int(st.get("sample_rate")),
                    bit_rate=_int(st.get("bit_rate")),
                    language=tags.get("language"),
                )
            )
        else:
            other += 1
    fmt_tags = _tags(fmt)
    return MediaProbe(
        path=path,
        container=fmt.get("format_name"),
        duration_s=_num(fmt.get("duration")),
        size_bytes=_int(fmt.get("size")),
        bit_rate=_int(fmt.get("bit_rate")),
        video=video,
        audio=audio,
        other_streams=other,
        timecode=fmt_tags.get("timecode") or next((v.timecode for v in video if v.timecode), None),
    )


def _ffprobe(path: Path) -> dict[str, Any]:
    _need("ffprobe")
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if proc.returncode != 0:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"ffprobe could not read {path.name}.",
            details={"stderr": proc.stderr.decode("utf-8", errors="replace")[-300:]},
            retryable=False,
        )
    import json  # noqa: PLC0415 - only needed here

    try:
        return cast("dict[str, Any]", json.loads(proc.stdout or b"{}"))
    except ValueError as exc:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED, "ffprobe returned invalid JSON.", retryable=False
        ) from exc


@action(domain=DOMAIN, kind=Kind.READ, params=ProbeMediaParams, result=MediaProbe)
def probe_media(s: ResolveSession, p: ProbeMediaParams) -> MediaProbe:
    """ffprobe summary of a file or Media Pool clip: codecs, fps, audio, color metadata."""
    if p.clip is not None:
        # The path comes from Resolve itself (media can live on any volume), read-only ffprobe.
        raw = resolve_clip(s, p.clip).GetClipProperty("File Path")
        if not raw:
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                "The clip has no File Path (generator, compound or timeline clip).",
                retryable=False,
            )
        target = Path(str(raw))
        if not target.is_file():
            raise DavinciError(
                ErrorCode.NOT_FOUND, f"Media file is offline: {target}", retryable=False
            )
    else:
        assert p.path is not None
        target = check_path(p.path, runtime().allowed_roots, must_exist=True)
        if not target.is_file():
            raise DavinciError(ErrorCode.NOT_FOUND, f"{target} is not a file.", retryable=False)
    return summarize_probe(str(target), _ffprobe(target))


# =====================================================================================
# source_frames
# =====================================================================================


class SourceFramesParams(_Params):
    path: str
    seconds: list[float] = Field(min_length=1, max_length=16, description="Positions in the file.")
    max_width: int = Field(default=960, ge=64, le=3840)


class SourceFrames(BaseModel):
    path: str
    seconds: list[float]
    images: list[ImageFile]


@action(domain=DOMAIN, kind=Kind.READ, params=SourceFramesParams, result=SourceFrames)
def source_frames(s: ResolveSession, p: SourceFramesParams) -> SourceFrames:
    """Extract frames straight from a media file with ffmpeg (Resolve not needed)."""
    _need("ffmpeg")
    source = check_path(p.path, runtime().allowed_roots, must_exist=True)
    if not source.is_file():
        raise DavinciError(ErrorCode.NOT_FOUND, f"{source} is not a file.", retryable=False)
    if any(sec < 0 for sec in p.seconds):
        raise DavinciError(ErrorCode.INVALID_PARAMS, "seconds must be >= 0.", retryable=False)
    out_dir = _frames_dir()
    stamp = _stamp()
    images: list[ImageFile] = []
    for i, sec in enumerate(p.seconds, start=1):
        dst = out_dir / f"src_{stamp}_{i:02d}.jpg"
        proc = _run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                f"{sec:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({p.max_width},iw)':-2",
                "-q:v",
                "3",
                str(dst),
            ]
        )
        if proc.returncode != 0 or not dst.is_file():
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                f"No frame could be extracted at {sec}s of {source.name}.",
                hint="The position may be beyond the file duration (see probe_media).",
                details={"stderr": proc.stderr.decode("utf-8", errors="replace")[-300:]},
                retryable=False,
            )
        images.append(_image(dst, f"{source.name} @ {sec:g}s"))
    return SourceFrames(path=str(source), seconds=p.seconds, images=images)


# =====================================================================================
# screenshot_resolve
# =====================================================================================


class ScreenshotParams(_Params):
    max_width: int = Field(default=1600, ge=320, le=3840)


class Screenshot(BaseModel):
    path: str
    window_id: int
    images: list[ImageFile]


@action(domain=DOMAIN, kind=Kind.READ, params=ScreenshotParams, result=Screenshot)
def screenshot_resolve(s: ResolveSession, p: ScreenshotParams) -> Screenshot:
    """Screenshot of the DaVinci Resolve window (macOS, needs Screen Recording permission)."""
    if sys.platform != "darwin":
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "screenshot_resolve is only implemented on macOS.",
            retryable=False,
        )
    # Window id through the CoreGraphics window list (JXA), not System Events: windows exposed
    # by System Events have no CGWindowID. Ids are readable without Screen Recording permission.
    listing = _run(["osascript", "-l", "JavaScript", "-e", _WINDOW_ID_JXA % RESOLVE_APP])
    window = _text(listing)
    if listing.returncode != 0 or not window.isdigit():
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "The DaVinci Resolve window could not be found.",
            hint="Resolve must be running with a visible window (not minimized).",
            details={"stderr": listing.stderr.decode("utf-8", errors="replace")[-200:]},
            retryable=True,
        )
    out_dir = _frames_dir()
    stamp = _stamp()
    raw = out_dir / f"screen_{stamp}.png"
    dst = out_dir / f"screen_{stamp}.jpg"
    try:
        proc = _run(["screencapture", "-x", "-o", "-l", window, str(raw)])
        if proc.returncode != 0 or not raw.is_file() or raw.stat().st_size == 0:
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                "screencapture produced no image.",
                hint=(
                    "Grant Screen Recording to the app running this server in System Settings > "
                    "Privacy & Security, then restart it."
                ),
                details={"stderr": proc.stderr.decode("utf-8", errors="replace")[-200:]},
                retryable=False,
            )
        _to_jpeg(raw, dst, p.max_width)
    finally:
        raw.unlink(missing_ok=True)
    return Screenshot(
        path=str(dst),
        window_id=int(window),
        images=[_image(dst, "DaVinci Resolve window")],
    )


ACTIONS = [export_frame, contact_sheet, probe_media, source_frames, screenshot_resolve]
