# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: PLR2004, PLR0912
"""Interchange domain: offline EDL / FCPXML / OTIO / marker CSV / .cube files.

Nothing here talks to Resolve (the handlers ignore the session), so it works with
the free edition and for edits the scripting API cannot do: write a file, then
import it with the timeline domain. Every written file is parsed back to verify it.

Media paths embedded in documents (asset URLs) are plain strings that are never
opened, so they are not sandboxed; the files this module reads or writes are.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import quote, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "interchange"
MAX_READ_BYTES = 64 * 1024 * 1024


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bad(message: str, hint: str | None = None) -> DavinciError:
    return DavinciError(ErrorCode.INVALID_PARAMS, message, hint=hint, retryable=False)


# -- timecode math -------------------------------------------------------------------


def nominal_fps(fps: float) -> int:
    """Timecode counting base: 23.976 -> 24, 29.97 -> 30, 59.94 -> 60."""
    return round(fps)


def frame_duration(fps: float) -> tuple[int, int]:
    """Exact frame duration in seconds as (numerator, denominator): 29.97 -> (1001, 30000)."""
    nom = nominal_fps(fps)
    if nom > 0 and abs(fps - nom * 1000 / 1001) < 0.01:
        return (1001, nom * 1000)
    if abs(fps - nom) < 0.001:
        return (1, nom)
    frac = Fraction(fps).limit_denominator(100000)
    return (frac.denominator, frac.numerator)


def _drop_count(fps: float, drop: bool) -> int:
    if not drop:
        return 0
    nom = nominal_fps(fps)
    if nom not in (30, 60) or abs(fps - nom * 1000 / 1001) > 0.01:
        raise _bad(f"Drop-frame timecode needs 29.97 or 59.94 fps, got {fps}.")
    return nom // 15


def frames_to_timecode(frames: int, fps: float, drop: bool = False) -> str:
    """Frame count -> ``HH:MM:SS:FF`` (``;`` before the frames when drop-frame)."""
    if frames < 0:
        raise _bad(f"Negative frame count: {frames}.")
    nom = nominal_fps(fps)
    dropped = _drop_count(fps, drop)
    n = frames
    if dropped:
        per_min = nom * 60 - dropped
        per_10min = nom * 600 - 9 * dropped
        tens, rest = divmod(n, per_10min)
        n += 9 * dropped * tens
        if rest >= dropped:
            n += dropped * ((rest - dropped) // per_min)
    ff = n % nom
    ss = (n // nom) % 60
    mm = (n // (nom * 60)) % 60
    hh = n // (nom * 3600)
    sep = ";" if dropped else ":"
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


_TC_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([:;.])(\d{2,3})$")


def timecode_to_frames(timecode: str, fps: float, drop: bool = False) -> int:
    """``HH:MM:SS:FF`` or ``HH:MM:SS;FF`` -> frame count (inverse of ``frames_to_timecode``)."""
    match = _TC_RE.match(timecode.strip())
    if match is None:
        raise _bad(f"Invalid timecode {timecode!r}.", "Use HH:MM:SS:FF (or HH:MM:SS;FF).")
    hh, mm, ss, _, ff = match.groups()
    nom = nominal_fps(fps)
    if int(ff) >= nom or int(mm) > 59 or int(ss) > 59:
        raise _bad(f"Timecode {timecode!r} is out of range for {nom} fps counting.")
    dropped = _drop_count(fps, drop)
    total_min = 60 * int(hh) + int(mm)
    frames = nom * 3600 * int(hh) + nom * 60 * int(mm) + nom * int(ss) + int(ff)
    frames -= dropped * (total_min - total_min // 10)
    if dropped and int(ss) == 0 and int(ff) < dropped and int(mm) % 10 != 0:
        raise _bad(f"Timecode {timecode!r} does not exist in drop-frame counting.")
    return frames


def _rational(frames: int, fps: float) -> str:
    num, den = frame_duration(fps)
    frac = Fraction(frames * num, den)
    if frac.denominator == 1:
        return f"{frac.numerator}s"
    return f"{frac.numerator}/{frac.denominator}s"


def _parse_rational(text: str | None) -> Fraction:
    if not text:
        return Fraction(0)
    raw = text.strip().removesuffix("s")
    try:
        return Fraction(raw)
    except (ValueError, ZeroDivisionError) as exc:
        raise _bad(f"Invalid FCPXML time value {text!r}.") from exc


# -- file helpers --------------------------------------------------------------------


def _out_path(path: str, overwrite: bool) -> Path:
    target = check_path(path, runtime().allowed_roots)
    if target.is_dir():
        raise _bad(f"{target} is a directory.")
    if target.exists() and not overwrite:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"{target} already exists.",
            hint="Pass overwrite=true to replace it.",
            retryable=False,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _read_text(path: str) -> str:
    source = check_path(path, runtime().allowed_roots, must_exist=True)
    if not source.is_file():
        raise _bad(f"{source} is not a file.")
    if source.stat().st_size > MAX_READ_BYTES:
        raise _bad(f"{source} is larger than {MAX_READ_BYTES} bytes.")
    return source.read_text(encoding="utf-8", errors="replace")


def _safe_xml(text: str) -> ET.Element:
    """Parse XML while refusing entity declarations (billion-laughs / XXE)."""
    if "<!ENTITY" in text:
        raise _bad("XML entity declarations are not allowed.")
    try:
        return ET.fromstring(text)  # noqa: S314 - entities rejected above
    except ET.ParseError as exc:
        raise _bad(f"Invalid XML: {exc}") from exc


def _file_url(path: str) -> str:
    return path if "://" in path else Path(path).expanduser().absolute().as_uri()


def _url_to_path(url: str) -> str:
    parsed = urlparse(url)
    return unquote(parsed.path) if parsed.scheme == "file" else url


# =====================================================================================
# EDL (CMX3600)
# =====================================================================================


class EdlEvent(BaseModel):
    reel: str = Field(default="AX", max_length=8, description="Reel name (max 8 chars).")
    track: str = Field(default="V", description="V, A, A2, AA, AA/V, B, NONE…")
    transition: Literal["C", "D", "W"] = Field(default="C", description="Cut, dissolve, wipe.")
    transition_frames: int = Field(default=0, ge=0, description="Dissolve / wipe length.")
    wipe_code: str = Field(default="W001", description="Wipe pattern when transition is W.")
    src_in: str = Field(description="Source in timecode.")
    src_out: str = Field(description="Source out timecode (exclusive).")
    rec_in: str = Field(description="Record in timecode.")
    rec_out: str = Field(description="Record out timecode (exclusive).")
    clip_name: str | None = Field(default=None, description="Written as * FROM CLIP NAME.")
    source_file: str | None = Field(default=None, description="Written as * SOURCE FILE.")
    comment: str | None = None
    speed_fps: float | None = Field(default=None, gt=0, description="M2 retime speed in fps.")


class WriteEdlParams(_Params):
    path: str
    title: str = "Untitled"
    fps: float = Field(default=24.0, gt=0)
    drop_frame: bool = False
    events: list[EdlEvent] = Field(min_length=1)
    overwrite: bool = False


class EdlWritten(BaseModel):
    path: str
    events: int
    fps: float
    drop_frame: bool
    bytes: int


class ReadEdlParams(_Params):
    path: str


class EdlRead(BaseModel):
    title: str
    drop_frame: bool
    events: list[EdlEvent]


def _norm_tc(timecode: str, fps: float, drop: bool) -> tuple[str, int]:
    frames = timecode_to_frames(timecode, fps, drop)
    return frames_to_timecode(frames, fps, drop), frames


def render_edl(p: WriteEdlParams) -> str:
    _drop_count(p.fps, p.drop_frame)
    lines = [
        f"TITLE: {p.title}",
        f"FCM: {'DROP FRAME' if p.drop_frame else 'NON-DROP FRAME'}",
        "",
    ]
    for number, ev in enumerate(p.events, start=1):
        tcs: list[str] = []
        frames: list[int] = []
        for value in (ev.src_in, ev.src_out, ev.rec_in, ev.rec_out):
            tc, count = _norm_tc(value, p.fps, p.drop_frame)
            tcs.append(tc)
            frames.append(count)
        if frames[1] <= frames[0] or frames[3] <= frames[2]:
            raise _bad(f"Event {number}: out must be after in.")
        if ev.transition == "C":
            kind, dur = "C", ""
        elif ev.transition == "D":
            kind, dur = "D", f"{ev.transition_frames:03d}"
        else:
            kind, dur = ev.wipe_code, f"{ev.transition_frames:03d}"
        lines.append(
            f"{number:03d}  {ev.reel:<8} {ev.track:<5} {kind:<4} {dur:<3}  " + " ".join(tcs)
        )
        if ev.speed_fps is not None:
            lines.append(f"M2   {ev.reel:<8} {ev.speed_fps:>6.1f}   {tcs[0]}")
        if ev.clip_name:
            lines.append(f"* FROM CLIP NAME: {ev.clip_name}")
        if ev.source_file:
            lines.append(f"* SOURCE FILE: {ev.source_file}")
        if ev.comment:
            lines.append(f"* COMMENT: {ev.comment}")
        lines.append("")
    return "\n".join(lines)


_EVENT_RE = re.compile(r"^\d{3,6}\s+\S+\s+\S+\s+\S+")


def parse_edl(text: str) -> EdlRead:
    title = ""
    drop = False
    events: list[EdlEvent] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("TITLE:"):
            title = line.removeprefix("TITLE:").strip()
        elif line.startswith("FCM:"):
            drop = line.removeprefix("FCM:").strip().upper().startswith("DROP")
        elif line.startswith("*") and events:
            body = line.lstrip("* ").strip()
            key, _, value = body.partition(":")
            key = key.strip().upper()
            if key == "FROM CLIP NAME":
                events[-1].clip_name = value.strip()
            elif key == "SOURCE FILE":
                events[-1].source_file = value.strip()
            elif key == "COMMENT":
                events[-1].comment = value.strip()
        elif line.startswith("M2") and events:
            tokens = line.split()
            if len(tokens) >= 3:
                events[-1].speed_fps = float(tokens[2])
        elif _EVENT_RE.match(line):
            tokens = line.split()
            if len(tokens) < 8:
                raise _bad(f"Malformed EDL event line: {line!r}")
            number, reel, track, kind = tokens[:4]
            tcs = tokens[-4:]
            middle = tokens[4:-4]
            frames = int(middle[0]) if middle and middle[0].isdigit() else 0
            if not number.isdigit():
                raise _bad(f"Malformed EDL event line: {line!r}")
            events.append(
                EdlEvent(
                    reel=reel,
                    track=track,
                    transition="W" if kind.startswith("W") else ("D" if kind == "D" else "C"),
                    transition_frames=frames,
                    wipe_code=kind if kind.startswith("W") else "W001",
                    src_in=tcs[0],
                    src_out=tcs[1],
                    rec_in=tcs[2],
                    rec_out=tcs[3],
                )
            )
        elif line.strip() and not line.startswith(("TITLE", "FCM")) and not events:
            continue
    return EdlRead(
        title=title, drop_frame=drop or any(";" in e.rec_in for e in events), events=events
    )


def _verify_edl(_: ResolveSession, p: WriteEdlParams, result: EdlWritten) -> Verification:
    parsed = parse_edl(Path(result.path).read_text(encoding="utf-8"))
    ok = len(parsed.events) == len(p.events) and parsed.events[-1].rec_out.replace(
        ";", ":"
    ) == _norm_tc(p.events[-1].rec_out, p.fps, p.drop_frame)[0].replace(";", ":")
    return Verification(ok=ok, observed={"events": len(parsed.events)})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=WriteEdlParams,
    result=EdlWritten,
    verify=_verify_edl,
)
def write_edl(s: ResolveSession, p: WriteEdlParams) -> EdlWritten:
    """Write a CMX3600 EDL from a structured event list (fps / drop-frame aware)."""
    target = _out_path(p.path, p.overwrite)
    content = render_edl(p)
    target.write_text(content, encoding="utf-8")
    return EdlWritten(
        path=str(target),
        events=len(p.events),
        fps=p.fps,
        drop_frame=p.drop_frame,
        bytes=len(content.encode()),
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ReadEdlParams, result=EdlRead)
def read_edl(s: ResolveSession, p: ReadEdlParams) -> EdlRead:
    """Parse a CMX3600 EDL into events."""
    return parse_edl(_read_text(p.path))


# =====================================================================================
# Shared sequence model (FCPXML / OTIO)
# =====================================================================================


class SeqItem(BaseModel):
    kind: Literal["clip", "gap", "title"] = "clip"
    name: str = ""
    file: str | None = Field(default=None, description="Media path or file:// URL (clips).")
    src_in_frames: int = Field(default=0, ge=0, description="First source frame used.")
    duration_frames: int = Field(ge=1)
    text: str | None = Field(default=None, description="Title text (titles).")


class SeqMarker(BaseModel):
    frame: int = Field(ge=0, description="Frame offset from the sequence start.")
    name: str = ""
    note: str = ""
    color: str = Field(default="Blue", description="Resolve color name.")
    duration_frames: int = Field(default=1, ge=1)


def _locate(items: list[SeqItem], frame: int) -> tuple[SeqItem, int, int]:
    """(item, item_rec_start, offset_in_item) for a sequence frame."""
    cursor = 0
    for item in items:
        if cursor <= frame < cursor + item.duration_frames:
            return item, cursor, frame - cursor
        cursor += item.duration_frames
    raise _bad(f"Marker at frame {frame} is outside the sequence ({cursor} frames).")


def _check_items(items: list[SeqItem]) -> None:
    for item in items:
        if item.kind == "clip" and not item.file:
            raise _bad(f"Clip {item.name!r} needs a file.")


# =====================================================================================
# FCPXML 1.10
# =====================================================================================


class WriteFcpxmlParams(_Params):
    path: str
    name: str = "Untitled"
    fps: float = Field(default=24.0, gt=0)
    drop_frame: bool = False
    width: int = Field(default=1920, ge=16)
    height: int = Field(default=1080, ge=16)
    start_timecode: str = Field(default="00:00:00:00", description="Sequence start timecode.")
    items: list[SeqItem] = Field(min_length=1, description="Spine items in order.")
    markers: list[SeqMarker] = Field(default_factory=list[SeqMarker])
    overwrite: bool = False


class FcpxmlWritten(BaseModel):
    path: str
    assets: int
    items: int
    markers: int
    duration_frames: int


class ReadFcpxmlParams(_Params):
    path: str


class FcpAsset(BaseModel):
    id: str
    name: str
    path: str | None
    start_s: float
    duration_s: float


class FcpItem(BaseModel):
    kind: str
    name: str
    asset_id: str | None
    offset_s: float
    start_s: float
    duration_s: float
    duration_frames: int | None
    lane: int = 0


class FcpMarker(BaseModel):
    name: str
    note: str
    start_s: float
    duration_s: float
    parent: str


class FcpxmlRead(BaseModel):
    version: str
    fps: float | None
    width: int | None
    height: int | None
    assets: list[FcpAsset]
    items: list[FcpItem]
    markers: list[FcpMarker]


_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"


def _fps_label(fps: float) -> str:
    return {23.976: "2398", 29.97: "2997", 59.94: "5994"}.get(
        round(fps, 3), f"{fps:g}".replace(".", "")
    )


def render_fcpxml(p: WriteFcpxmlParams) -> tuple[str, int, int]:
    _check_items(p.items)
    _drop_count(p.fps, p.drop_frame)
    num, den = frame_duration(p.fps)
    total = sum(i.duration_frames for i in p.items)
    start_frames = timecode_to_frames(p.start_timecode, p.fps, p.drop_frame)
    root = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        id="r1",
        name=f"FFVideoFormat{p.height}p{_fps_label(p.fps)}",
        frameDuration=f"{num}/{den}s",
        width=str(p.width),
        height=str(p.height),
        colorSpace="1-1-1 (Rec. 709)",
    )
    # One asset per distinct file; its duration covers the furthest frame used.
    needed: dict[str, int] = {}
    for item in p.items:
        if item.kind == "clip" and item.file:
            needed[item.file] = max(
                needed.get(item.file, 0), item.src_in_frames + item.duration_frames
            )
    ids = {file: f"r{n}" for n, file in enumerate(needed, start=2)}
    for file, frames in needed.items():
        asset = ET.SubElement(
            resources,
            "asset",
            id=ids[file],
            name=Path(unquote(urlparse(file).path)).stem or file,
            start="0s",
            duration=_rational(frames, p.fps),
            hasVideo="1",
            hasAudio="1",
            format="r1",
            audioSources="1",
            audioChannels="2",
            audioRate="48000",
        )
        ET.SubElement(
            asset,
            "media-rep",
            kind="original-media",
            src=quote(_file_url(file), safe=":/%"),
        )
    title_effect: str | None = None
    if any(i.kind == "title" for i in p.items):
        title_effect = f"r{len(ids) + 2}"
        ET.SubElement(
            resources,
            "effect",
            id=title_effect,
            name="Basic Title",
            uid=_TITLE_UID,
        )
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name=p.name)
    project = ET.SubElement(event, "project", name=p.name)
    sequence = ET.SubElement(
        project,
        "sequence",
        format="r1",
        duration=_rational(total, p.fps),
        tcStart=_rational(start_frames, p.fps),
        tcFormat="DF" if p.drop_frame else "NDF",
        audioLayout="stereo",
        audioRate="48k",
    )
    spine = ET.SubElement(sequence, "spine")
    elements: list[ET.Element] = []
    cursor = 0
    for item in p.items:
        offset = _rational(cursor, p.fps)
        duration = _rational(item.duration_frames, p.fps)
        if item.kind == "clip" and item.file:
            el = ET.SubElement(
                spine,
                "asset-clip",
                ref=ids[item.file],
                offset=offset,
                name=item.name or Path(item.file).stem,
                start=_rational(item.src_in_frames, p.fps),
                duration=duration,
                format="r1",
                tcFormat="DF" if p.drop_frame else "NDF",
            )
        elif item.kind == "gap":
            el = ET.SubElement(
                spine, "gap", name=item.name or "Gap", offset=offset, start="0s", duration=duration
            )
        else:
            el = ET.SubElement(
                spine,
                "title",
                ref=title_effect or "",
                offset=offset,
                name=item.name or "Title",
                start="0s",
                duration=duration,
            )
            text = ET.SubElement(el, "text")
            style = ET.SubElement(text, "text-style", ref=f"ts{len(elements) + 1}")
            style.text = item.text or item.name
            defn = ET.SubElement(el, "text-style-def", id=f"ts{len(elements) + 1}")
            ET.SubElement(defn, "text-style", font="Helvetica", fontSize="63")
        elements.append(el)
        cursor += item.duration_frames
    for marker in p.markers:
        item, _, offset_in = _locate(p.items, marker.frame)
        parent = elements[p.items.index(item)]
        base = item.src_in_frames if item.kind == "clip" else 0
        attrs = {
            "start": _rational(base + offset_in, p.fps),
            "duration": _rational(marker.duration_frames, p.fps),
            "value": marker.name,
        }
        if marker.note:
            attrs["note"] = marker.note
        ET.SubElement(parent, "marker", attrs)
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n{body}\n'
    return xml, len(ids), total


def parse_fcpxml(text: str) -> FcpxmlRead:
    root = _safe_xml(text)
    if root.tag != "fcpxml":
        raise _bad("Not an FCPXML document (root element is not <fcpxml>).")
    fmt = root.find("./resources/format")
    fps: float | None = None
    width = height = None
    if fmt is not None:
        fd = _parse_rational(fmt.get("frameDuration"))
        fps = float(1 / fd) if fd else None
        w, h = fmt.get("width"), fmt.get("height")
        width = int(w) if w else None
        height = int(h) if h else None
    assets: list[FcpAsset] = []
    for el in root.iterfind("./resources/asset"):
        rep = el.find("media-rep")
        src = rep.get("src") if rep is not None else el.get("src")
        assets.append(
            FcpAsset(
                id=el.get("id", ""),
                name=el.get("name", ""),
                path=_url_to_path(src) if src else None,
                start_s=float(_parse_rational(el.get("start"))),
                duration_s=float(_parse_rational(el.get("duration"))),
            )
        )
    items: list[FcpItem] = []
    markers: list[FcpMarker] = []
    clip_tags = {"asset-clip", "clip", "video", "title", "gap", "audio", "ref-clip", "sync-clip"}

    def frames_of(duration: Fraction) -> int | None:
        return round(duration * fps) if fps else None

    def walk(el: ET.Element, lane_of_parent: int) -> None:
        for child in el:
            if child.tag in clip_tags:
                lane = int(child.get("lane", lane_of_parent))
                duration = _parse_rational(child.get("duration"))
                items.append(
                    FcpItem(
                        kind=child.tag,
                        name=child.get("name", ""),
                        asset_id=child.get("ref"),
                        offset_s=float(_parse_rational(child.get("offset"))),
                        start_s=float(_parse_rational(child.get("start"))),
                        duration_s=float(duration),
                        duration_frames=frames_of(duration),
                        lane=lane,
                    )
                )
                walk(child, lane)
            elif child.tag == "marker":
                markers.append(
                    FcpMarker(
                        name=child.get("value", ""),
                        note=child.get("note", ""),
                        start_s=float(_parse_rational(child.get("start"))),
                        duration_s=float(_parse_rational(child.get("duration")) or 0),
                        parent=el.get("name", el.tag),
                    )
                )
            else:
                walk(child, lane_of_parent)

    for sequence in root.iter("sequence"):
        walk(sequence, 0)
    return FcpxmlRead(
        version=root.get("version", ""),
        fps=fps,
        width=width,
        height=height,
        assets=assets,
        items=items,
        markers=markers,
    )


def _verify_fcpxml(_: ResolveSession, p: WriteFcpxmlParams, result: FcpxmlWritten) -> Verification:
    parsed = parse_fcpxml(Path(result.path).read_text(encoding="utf-8"))
    top = [i for i in parsed.items if i.lane == 0]
    ok = len(top) == len(p.items) and len(parsed.markers) == len(p.markers)
    return Verification(ok=ok, observed={"items": len(top), "markers": len(parsed.markers)})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=WriteFcpxmlParams,
    result=FcpxmlWritten,
    verify=_verify_fcpxml,
)
def write_fcpxml(s: ResolveSession, p: WriteFcpxmlParams) -> FcpxmlWritten:
    """Write a minimal FCPXML 1.10 (assets, spine of clips / gaps / titles, markers)."""
    target = _out_path(p.path, p.overwrite)
    xml, assets, total = render_fcpxml(p)
    target.write_text(xml, encoding="utf-8")
    return FcpxmlWritten(
        path=str(target),
        assets=assets,
        items=len(p.items),
        markers=len(p.markers),
        duration_frames=total,
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ReadFcpxmlParams, result=FcpxmlRead)
def read_fcpxml(s: ResolveSession, p: ReadFcpxmlParams) -> FcpxmlRead:
    """Parse an FCPXML file: assets, clips and markers."""
    return parse_fcpxml(_read_text(p.path))


# =====================================================================================
# OpenTimelineIO (hand-written JSON, no dependency)
# =====================================================================================


class OtioTrack(BaseModel):
    name: str = "V1"
    kind: Literal["Video", "Audio"] = "Video"
    items: list[SeqItem] = Field(min_length=1)


class WriteOtioParams(_Params):
    path: str
    name: str = "Untitled"
    fps: float = Field(default=24.0, gt=0)
    start_timecode: str | None = Field(default=None, description="Sets global_start_time.")
    drop_frame: bool = False
    tracks: list[OtioTrack] = Field(min_length=1)
    markers: list[SeqMarker] = Field(
        default_factory=list[SeqMarker], description="Attached to the first track."
    )
    overwrite: bool = False


class OtioWritten(BaseModel):
    path: str
    tracks: int
    items: int
    markers: int


class ReadOtioParams(_Params):
    path: str


class OtioItem(BaseModel):
    kind: str
    name: str
    target_url: str | None
    start: float | None
    duration: float | None
    rate: float | None


class OtioTrackRead(BaseModel):
    name: str
    kind: str
    items: list[OtioItem]


class OtioMarker(BaseModel):
    name: str
    color: str
    start: float
    duration: float
    rate: float
    parent: str


class OtioRead(BaseModel):
    name: str
    rate: float | None
    tracks: list[OtioTrackRead]
    markers: list[OtioMarker]


_OTIO_COLORS = {
    "red",
    "pink",
    "orange",
    "yellow",
    "green",
    "cyan",
    "blue",
    "purple",
    "magenta",
    "black",
    "white",
}


def _rt(value: float, rate: float) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": float(value)}


def _tr(start: float, duration: float, rate: float) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "duration": _rt(duration, rate),
        "start_time": _rt(start, rate),
    }


def render_otio(p: WriteOtioParams) -> tuple[str, int]:
    rate = p.fps
    count = 0
    for track in p.tracks:
        _check_items(track.items)
    if p.markers and not p.tracks:
        raise _bad("Markers need a track.")
    media_frames: dict[str, int] = {}
    for track in p.tracks:
        for it in track.items:
            if it.kind == "clip" and it.file:
                media_frames[it.file] = max(
                    media_frames.get(it.file, 0), it.src_in_frames + it.duration_frames
                )
    children: list[dict[str, Any]] = []
    for t_index, track in enumerate(p.tracks):
        kids: list[dict[str, Any]] = []
        for it in track.items:
            count += 1
            if it.kind == "gap":
                kids.append(
                    {
                        "OTIO_SCHEMA": "Gap.1",
                        "name": it.name,
                        "source_range": _tr(0, it.duration_frames, rate),
                        "effects": [],
                        "markers": [],
                        "metadata": {},
                    }
                )
                continue
            if it.kind == "clip" and it.file:
                reference: dict[str, Any] = {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "name": "",
                    "target_url": _file_url(it.file),
                    "available_range": _tr(0, media_frames[it.file], rate),
                    "metadata": {},
                }
            else:
                reference = {"OTIO_SCHEMA": "MissingReference.1", "name": "", "metadata": {}}
            clip_markers: list[dict[str, Any]] = []
            if t_index == 0:
                cursor = 0
                for marker in p.markers:
                    owner, start, offset = _locate(track.items, marker.frame)
                    if owner is it and cursor == start:
                        base = it.src_in_frames if it.kind == "clip" else 0
                        color = marker.color.upper()
                        clip_markers.append(
                            {
                                "OTIO_SCHEMA": "Marker.2",
                                "name": marker.name,
                                "color": color if color.lower() in _OTIO_COLORS else "RED",
                                "marked_range": _tr(base + offset, marker.duration_frames, rate),
                                "comment": marker.note,
                                "metadata": {},
                            }
                        )
            kids.append(
                {
                    "OTIO_SCHEMA": "Clip.1",
                    "name": it.name or (Path(it.file).stem if it.file else ""),
                    "source_range": _tr(it.src_in_frames, it.duration_frames, rate),
                    "media_reference": reference,
                    "effects": [],
                    "markers": clip_markers,
                    "metadata": {"text": it.text} if it.kind == "title" and it.text else {},
                }
            )
        children.append(
            {
                "OTIO_SCHEMA": "Track.1",
                "name": track.name,
                "kind": track.kind,
                "source_range": None,
                "children": kids,
                "effects": [],
                "markers": [],
                "metadata": {},
            }
        )
    start_value: dict[str, Any] | None = None
    if p.start_timecode is not None:
        frames = timecode_to_frames(p.start_timecode, p.fps, p.drop_frame)
        start_value = _rt(frames, rate)
    doc: dict[str, Any] = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": p.name,
        "global_start_time": start_value,
        "metadata": {},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "source_range": None,
            "children": children,
            "effects": [],
            "markers": [],
            "metadata": {},
        },
    }
    return json.dumps(doc, indent=2) + "\n", count


def _rt_value(node: object) -> tuple[float | None, float | None]:
    if isinstance(node, dict):
        d = cast("dict[str, Any]", node)
        if "value" in d and "rate" in d:
            return float(d["value"]), float(d["rate"])
    return None, None


def parse_otio(text: str) -> OtioRead:
    try:
        doc: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _bad(f"Invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise _bad("Not an OTIO Timeline (top level is not an object).")
    root = cast("dict[str, Any]", doc)
    if not str(root.get("OTIO_SCHEMA", "")).startswith("Timeline."):
        raise _bad("Not an OTIO Timeline (missing OTIO_SCHEMA Timeline.*).")
    tracks: list[OtioTrackRead] = []
    markers: list[OtioMarker] = []
    first_rate: float | None = None
    stack: dict[str, Any] = root.get("tracks") or {}
    for track in stack.get("children", []):
        items: list[OtioItem] = []
        for child in track.get("children", []):
            schema = str(child.get("OTIO_SCHEMA", "")).split(".")[0]
            if schema not in ("Clip", "Gap"):
                continue
            rng: dict[str, Any] = child.get("source_range") or {}
            start, rate = _rt_value(rng.get("start_time"))
            duration, rate2 = _rt_value(rng.get("duration"))
            rate = rate or rate2
            first_rate = first_rate or rate
            ref: dict[str, Any] = child.get("media_reference") or {}
            if not ref and child.get("media_references"):
                active = child.get("active_media_reference_key")
                ref = child["media_references"].get(active) or {}
            items.append(
                OtioItem(
                    kind=schema.lower(),
                    name=str(child.get("name", "")),
                    target_url=ref.get("target_url"),
                    start=start,
                    duration=duration,
                    rate=rate,
                )
            )
            for mk in cast("list[dict[str, Any]]", child.get("markers", [])):
                mrange: dict[str, Any] = mk.get("marked_range") or {}
                mstart, mrate = _rt_value(mrange.get("start_time"))
                mdur, _ = _rt_value(mrange.get("duration"))
                markers.append(
                    OtioMarker(
                        name=str(mk.get("name", "")),
                        color=str(mk.get("color", "")),
                        start=mstart or 0.0,
                        duration=mdur or 0.0,
                        rate=mrate or rate or 0.0,
                        parent=str(child.get("name", "")),
                    )
                )
        tracks.append(
            OtioTrackRead(
                name=str(track.get("name", "")), kind=str(track.get("kind", "")), items=items
            )
        )
    return OtioRead(name=str(root.get("name", "")), rate=first_rate, tracks=tracks, markers=markers)


def _verify_otio(_: ResolveSession, p: WriteOtioParams, result: OtioWritten) -> Verification:
    parsed = parse_otio(Path(result.path).read_text(encoding="utf-8"))
    n_items = sum(len(t.items) for t in parsed.tracks)
    ok = len(parsed.tracks) == len(p.tracks) and n_items == result.items
    ok = ok and len(parsed.markers) == len(p.markers)
    return Verification(ok=ok, observed={"tracks": len(parsed.tracks), "items": n_items})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=WriteOtioParams,
    result=OtioWritten,
    verify=_verify_otio,
)
def write_otio(s: ResolveSession, p: WriteOtioParams) -> OtioWritten:
    """Write an OpenTimelineIO JSON timeline (tracks of clips / gaps, external refs)."""
    target = _out_path(p.path, p.overwrite)
    content, count = render_otio(p)
    target.write_text(content, encoding="utf-8")
    return OtioWritten(path=str(target), tracks=len(p.tracks), items=count, markers=len(p.markers))


@action(domain=DOMAIN, kind=Kind.READ, params=ReadOtioParams, result=OtioRead)
def read_otio(s: ResolveSession, p: ReadOtioParams) -> OtioRead:
    """Parse an OpenTimelineIO JSON file: tracks, clips, gaps and markers."""
    return parse_otio(_read_text(p.path))


# =====================================================================================
# Marker CSV
# =====================================================================================

CSV_COLUMNS = ["#", "Timecode", "Name", "Color", "Duration", "Notes"]
_CSV_ALIASES = {
    "timecode": ("timecode", "timecode in", "record in", "in", "tc"),
    "name": ("name", "marker name"),
    "color": ("color", "colour", "marker type"),
    "duration": ("duration",),
    "notes": ("notes", "note", "description", "comment"),
}


class CsvMarker(BaseModel):
    timecode: str
    name: str = ""
    color: str = "Blue"
    duration: int = Field(default=1, ge=1, description="Frames.")
    notes: str = ""


class WriteMarkerCsvParams(_Params):
    path: str
    fps: float = Field(default=24.0, gt=0)
    drop_frame: bool = False
    markers: list[CsvMarker] = Field(min_length=1)
    overwrite: bool = False


class MarkerCsvWritten(BaseModel):
    path: str
    markers: int
    columns: list[str]


class ReadMarkerCsvParams(_Params):
    path: str


class MarkerCsvRead(BaseModel):
    markers: list[CsvMarker]


def _verify_csv(
    _: ResolveSession, p: WriteMarkerCsvParams, result: MarkerCsvWritten
) -> Verification:
    parsed = parse_marker_csv(Path(result.path).read_text(encoding="utf-8"))
    return Verification(
        ok=len(parsed.markers) == len(p.markers), observed={"markers": len(parsed.markers)}
    )


def parse_marker_csv(text: str) -> MarkerCsvRead:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise _bad("Empty CSV.")
    header = [c.strip().lower() for c in rows[0]]
    index: dict[str, int] = {}
    for key, names in _CSV_ALIASES.items():
        for name in names:
            if name in header:
                index[key] = header.index(name)
                break
    if "timecode" not in index:
        raise _bad("No timecode column found.", f"Expected one of {_CSV_ALIASES['timecode']}.")

    def cell(row: list[str], key: str, default: str = "") -> str:
        i = index.get(key)
        return row[i].strip() if i is not None and i < len(row) else default

    markers = [
        CsvMarker(
            timecode=cell(row, "timecode"),
            name=cell(row, "name"),
            color=cell(row, "color", "Blue") or "Blue",
            duration=max(1, int(cell(row, "duration", "1") or 1)),
            notes=cell(row, "notes"),
        )
        for row in rows[1:]
    ]
    return MarkerCsvRead(markers=markers)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=WriteMarkerCsvParams,
    result=MarkerCsvWritten,
    verify=_verify_csv,
)
def write_marker_csv(s: ResolveSession, p: WriteMarkerCsvParams) -> MarkerCsvWritten:
    """Write markers to a CSV (#, Timecode, Name, Color, Duration, Notes)."""
    target = _out_path(p.path, p.overwrite)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for n, m in enumerate(p.markers, start=1):
        tc, _ = _norm_tc(m.timecode, p.fps, p.drop_frame)
        writer.writerow([n, tc, m.name, m.color, m.duration, m.notes])
    target.write_text(buffer.getvalue(), encoding="utf-8")
    return MarkerCsvWritten(path=str(target), markers=len(p.markers), columns=CSV_COLUMNS)


@action(domain=DOMAIN, kind=Kind.READ, params=ReadMarkerCsvParams, result=MarkerCsvRead)
def read_marker_csv(s: ResolveSession, p: ReadMarkerCsvParams) -> MarkerCsvRead:
    """Read a marker CSV (header names are matched leniently)."""
    return parse_marker_csv(_read_text(p.path))


# =====================================================================================
# .cube LUTs
# =====================================================================================

type Vec3 = tuple[float, float, float]


class CubeTransform(BaseModel):
    """Declarative grade; applied in order: matrix, gain, lift, gamma, saturation."""

    model_config = ConfigDict(extra="forbid")

    matrix: list[list[float]] | None = Field(default=None, description="3x3, rows of output RGB.")
    gain: list[float] | float = 1.0
    lift: list[float] | float = 0.0
    gamma: list[float] | float = 1.0
    saturation: float = Field(default=1.0, ge=0)


class WriteCubeParams(_Params):
    path: str
    title: str = "davinci-mcp LUT"
    dim: Literal[1, 3] = 3
    size: int = Field(default=33, ge=2, le=4096)
    domain_min: float = 0.0
    domain_max: float = 1.0
    values: list[list[float]] | None = Field(
        default=None, description="Rows of RGB: size rows (1D) or size^3 rows (3D, red fastest)."
    )
    transform: CubeTransform | None = None
    clamp: bool = Field(default=True, description="Clamp transform output to the domain.")
    overwrite: bool = False


class CubeWritten(BaseModel):
    path: str
    dim: int
    size: int
    entries: int


class ReadCubeParams(_Params):
    path: str


class CubeRead(BaseModel):
    title: str | None
    dim: int
    size: int
    domain_min: list[float]
    domain_max: list[float]
    entries: int
    complete: bool
    first: list[float] | None
    last: list[float] | None


def _triple(value: list[float] | float, what: str) -> Vec3:
    if isinstance(value, list):
        if len(value) != 3:
            raise _bad(f"{what} must be a number or [r, g, b].")
        return (value[0], value[1], value[2])
    return (value, value, value)


def _apply(rgb: Vec3, t: CubeTransform, gain: Vec3, lift: Vec3, gamma: Vec3) -> Vec3:
    r, g, b = rgb
    if t.matrix is not None:
        m = t.matrix
        r, g, b = (
            m[0][0] * r + m[0][1] * g + m[0][2] * b,
            m[1][0] * r + m[1][1] * g + m[1][2] * b,
            m[2][0] * r + m[2][1] * g + m[2][2] * b,
        )
    out: list[float] = []
    for c, gn, lf, gm in zip((r, g, b), gain, lift, gamma, strict=True):
        v = c * gn
        v += lf * (1.0 - v)
        out.append(math.copysign(abs(v) ** (1.0 / gm), v) if v else 0.0)
    if t.saturation != 1.0:
        luma = 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
        out = [luma + (c - luma) * t.saturation for c in out]
    return (out[0], out[1], out[2])


def render_cube(p: WriteCubeParams) -> tuple[str, int]:
    if p.dim == 3 and p.size > 65:
        raise _bad("3D LUT size is limited to 65.")
    if p.domain_max <= p.domain_min:
        raise _bad("domain_max must be greater than domain_min.")
    expected = p.size if p.dim == 1 else p.size**3
    rows: list[Vec3]
    if (p.values is None) == (p.transform is None):
        raise _bad("Pass exactly one of values or transform.")
    if p.values is not None:
        if len(p.values) != expected or any(len(r) != 3 for r in p.values):
            raise _bad(f"values must hold {expected} rows of 3 numbers.")
        rows = [(r[0], r[1], r[2]) for r in p.values]
    else:
        t = p.transform
        assert t is not None
        if t.matrix is not None and (len(t.matrix) != 3 or any(len(r) != 3 for r in t.matrix)):
            raise _bad("matrix must be 3x3.")
        gain, lift, gamma = (
            _triple(t.gain, "gain"),
            _triple(t.lift, "lift"),
            _triple(t.gamma, "gamma"),
        )
        if min(gamma) <= 0:
            raise _bad("gamma must be positive.")
        span = p.domain_max - p.domain_min
        step = [p.domain_min + span * i / (p.size - 1) for i in range(p.size)]
        inputs: list[Vec3] = (
            [(v, v, v) for v in step]
            if p.dim == 1
            else [(r, g, b) for b in step for g in step for r in step]
        )
        rows = [_apply(rgb, t, gain, lift, gamma) for rgb in inputs]
        if p.clamp:
            rows = [
                (
                    min(max(r, p.domain_min), p.domain_max),
                    min(max(g, p.domain_min), p.domain_max),
                    min(max(b, p.domain_min), p.domain_max),
                )
                for r, g, b in rows
            ]
    header = [f'TITLE "{p.title.replace(chr(34), chr(39))}"']
    if (p.domain_min, p.domain_max) != (0.0, 1.0):
        header.append(f"DOMAIN_MIN {p.domain_min:.6f} {p.domain_min:.6f} {p.domain_min:.6f}")
        header.append(f"DOMAIN_MAX {p.domain_max:.6f} {p.domain_max:.6f} {p.domain_max:.6f}")
    header.append(f"LUT_{p.dim}D_SIZE {p.size}")
    body = [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in rows]
    return "\n".join([*header, "", *body]) + "\n", len(rows)


def parse_cube(text: str) -> CubeRead:
    title: str | None = None
    dim = size = 0
    dmin = [0.0, 0.0, 0.0]
    dmax = [1.0, 1.0, 1.0]
    first: list[float] | None = None
    last: list[float] | None = None
    entries = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, rest = line.partition(" ")
        key = key.upper()
        try:
            if key == "TITLE":
                title = rest.strip().strip('"')
            elif key in ("LUT_1D_SIZE", "LUT_3D_SIZE"):
                dim, size = int(key[4]), int(rest)
            elif key == "DOMAIN_MIN":
                dmin = [float(x) for x in rest.split()]
            elif key == "DOMAIN_MAX":
                dmax = [float(x) for x in rest.split()]
            elif key[0].isalpha():
                continue  # unknown keyword (e.g. LUT_3D_INPUT_RANGE)
            else:
                values = [float(x) for x in line.split()]
                if len(values) != 3:
                    raise ValueError(line)  # noqa: TRY301
                first = first or values
                last = values
                entries += 1
        except ValueError as exc:
            raise _bad(f"Malformed .cube line: {raw!r}") from exc
    if not dim or not size:
        raise _bad("Missing LUT_1D_SIZE / LUT_3D_SIZE.")
    expected = size if dim == 1 else size**3
    return CubeRead(
        title=title,
        dim=dim,
        size=size,
        domain_min=dmin,
        domain_max=dmax,
        entries=entries,
        complete=entries == expected,
        first=first,
        last=last,
    )


def _verify_cube(_: ResolveSession, p: WriteCubeParams, result: CubeWritten) -> Verification:
    parsed = parse_cube(Path(result.path).read_text(encoding="utf-8"))
    ok = parsed.complete and parsed.dim == p.dim and parsed.size == p.size
    return Verification(ok=ok, observed={"entries": parsed.entries})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=WriteCubeParams,
    result=CubeWritten,
    verify=_verify_cube,
)
def write_cube_lut(s: ResolveSession, p: WriteCubeParams) -> CubeWritten:
    """Write a 1D/3D .cube LUT from explicit values or a declarative grade transform."""
    target = _out_path(p.path, p.overwrite)
    content, entries = render_cube(p)
    target.write_text(content, encoding="utf-8")
    return CubeWritten(path=str(target), dim=p.dim, size=p.size, entries=entries)


@action(domain=DOMAIN, kind=Kind.READ, params=ReadCubeParams, result=CubeRead)
def read_cube_lut(s: ResolveSession, p: ReadCubeParams) -> CubeRead:
    """Read a .cube header: title, dimension, size, domain and entry count."""
    return parse_cube(_read_text(p.path))


ACTIONS = [
    write_edl,
    read_edl,
    write_fcpxml,
    read_fcpxml,
    write_otio,
    read_otio,
    write_marker_csv,
    read_marker_csv,
    write_cube_lut,
    read_cube_lut,
]
