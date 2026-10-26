# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Benchmark v2: four post-production jobs written independently of any MCP server.

Each scenario is a small but complete job (media management, multi-track assembly with
audio, look development with versions and stills, QC + interchange + range delivery).
Setup and checks talk to Resolve directly (DaVinciResolveScript) or inspect files on
disk, never through the server under test, and every check is wrapped so that a missing
object yields False instead of raising.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scenarios import Check, Resolve, Scenario, _clip, _items, _run, _timeline

FPS = 25


# -- Shared helpers ------------------------------------------------------------------------


def _safe[T](fn: Callable[[], T], default: T) -> T:
    """Run a Resolve read; any exception or None becomes `default`."""
    try:
        value = fn()
    except Exception:
        return default
    return default if value is None else value


def _stem(name: str) -> str:
    """Clip name without a media extension ('A.mov' -> 'A', 'A' -> 'A')."""
    return re.sub(r"\.(mov|mp4|wav|aiff?|mxf)$", "", str(name or "").strip(), flags=re.I)


def _subfolder(folder: Any, name: str) -> Any:
    if folder is None:
        return None
    for sub in _safe(folder.GetSubFolderList, []):
        if _safe(sub.GetName, "") == name:
            return sub
    return None


def _all_clips(folder: Any) -> list[Any]:
    """Every media pool item under `folder`, recursively."""
    if folder is None:
        return []
    out = list(_safe(folder.GetClipList, []))
    for sub in _safe(folder.GetSubFolderList, []):
        out.extend(_all_clips(sub))
    return out


def _markers(obj: Any) -> dict[int, dict[str, Any]]:
    """Markers keyed by int frame (Resolve may return float keys)."""
    raw = _safe(obj.GetMarkers, {}) if obj is not None else {}
    out: dict[int, dict[str, Any]] = {}
    for frame, info in dict(raw).items():
        try:
            out[round(float(frame))] = dict(info or {})
        except (TypeError, ValueError):
            continue
    return out


def _has_marker(obj: Any, frames: tuple[int, ...], color: str, name: str) -> bool:
    marks = _markers(obj)
    for frame in frames:
        info = marks.get(frame)
        if (
            info
            and str(info.get("color", "")).lower() == color.lower()
            and str(info.get("name", "")).strip() == name
        ):
            return True
    return False


def _probe_full(path: Path) -> dict[str, Any]:
    """First video stream: codec, profile, size, frame count (nb_frames or duration)."""
    if not path.is_file():
        return {}
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,profile,width,height,nb_frames,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    ).stdout
    stream = (json.loads(out or "{}").get("streams") or [{}])[0]
    frames = 0
    try:
        frames = int(stream.get("nb_frames") or 0)
    except ValueError:
        frames = 0
    if not frames:
        try:
            frames = round(float(stream.get("duration") or 0) * FPS)
        except ValueError:
            frames = 0
    stream["frames"] = frames
    return stream


def _wav(path: Path, seconds: float, freq: int = 220) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}:sample_rate=48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


def _import_timeline(project: Any, name: str, paths: list[Path]) -> Any:
    # The media is 25 fps; a fresh project defaults to 24, which would shift every timecode.
    project.SetSetting("timelineFrameRate", str(FPS))
    pool = project.GetMediaPool()
    for _ in range(5):  # right after a project switch, Resolve sometimes returns None
        items = pool.ImportMedia([str(p) for p in paths])
        tl = pool.CreateTimelineFromClips(name, items) if items else None
        if tl is not None:
            project.SetCurrentTimeline(tl)
            return tl
        time.sleep(1)
    raise RuntimeError(f"setup could not create timeline {name!r}")


# -- 1. Dailies ingest & logging ---------------------------------------------------------

_LOG = (
    # file, scene, take, description, circled
    ("INT_A_01", "12", "1", "Director interview wide", "no"),
    ("INT_A_02", "12", "2", "Director interview close", "yes"),
    ("BR_city_01", "14", "1", "City skyline at dusk", "yes"),
    ("BR_city_02", "14", "3", "Street crossing handheld", "no"),
)


def _dailies_setup(_r: Resolve, _project: Any, work: Path) -> None:
    src = work / "dailies"
    src.mkdir(parents=True, exist_ok=True)
    for (name, *_), color in zip(_LOG, ("sienna", "peru", "steelblue", "slategray"), strict=True):
        _clip(src / f"{name}.mov", color, 3)
    rows = ["file,scene,take,description,circled"]
    rows += [
        f"{name}.mov,{scene},{take},{desc},{circled}" for name, scene, take, desc, circled in _LOG
    ]
    (work / "camera_log.csv").write_text("\n".join(rows) + "\n")


def _dailies_prompt(work: Path) -> str:
    return (
        "In the open DaVinci Resolve project, ingest today's dailies. Create a bin named "
        "'Day 03' at the root of the media pool, with two sub-bins inside it: 'Interviews' and "
        f"'B-Roll'. Import the four clips of the folder {work}/dailies once each: the INT_* "
        "clips go into 'Interviews' and the BR_* clips into 'B-Roll'. Using the camera log "
        f"{work}/camera_log.csv, set on every clip the metadata fields Scene, Take and "
        "Description to the values of its row. Set the clip color to Green for circled takes "
        "(circled = yes) and to Orange for the others. Finally, on clip BR_city_02, add a Red "
        "clip marker at frame 30, 1 frame long, named 'Boom in shot' with the note "
        "'Mic visible at top of frame'."
    )


def _dailies_check(_r: Resolve, project: Any, _work: Path) -> Check:
    pool = _safe(project.GetMediaPool, None)
    root = _safe(pool.GetRootFolder, None) if pool is not None else None
    day = _subfolder(root, "Day 03")
    interviews = _subfolder(day, "Interviews")
    broll = _subfolder(day, "B-Roll")
    int_names = sorted(_stem(_safe(c.GetName, "")) for c in _all_clips(interviews))
    br_names = sorted(_stem(_safe(c.GetName, "")) for c in _all_clips(broll))
    everywhere = [_stem(_safe(c.GetName, "")) for c in _all_clips(root)]
    by_name = {_stem(_safe(c.GetName, "")): c for c in _all_clips(day)}

    def meta_ok(name: str, scene: str, take: str, desc: str) -> bool:
        clip = by_name.get(name)
        meta = dict(_safe(clip.GetMetadata, {})) if clip is not None else {}
        return (
            str(meta.get("Scene", "")).strip() == scene
            and str(meta.get("Take", "")).strip() == take
            and str(meta.get("Description", "")).strip() == desc
        )

    def color_ok(name: str, circled: str) -> bool:
        clip = by_name.get(name)
        color = str(_safe(clip.GetClipColor, "")) if clip is not None else ""
        return color == ("Green" if circled == "yes" else "Orange")

    return {
        "bins_created": interviews is not None and broll is not None,
        "clips_sorted": int_names == ["INT_A_01", "INT_A_02"]
        and br_names == ["BR_city_01", "BR_city_02"],
        "imported_once": all(everywhere.count(row[0]) == 1 for row in _LOG),
        "metadata_from_log": all(meta_ok(n, s, t, d) for n, s, t, d, _c in _LOG),
        "clip_colors": all(color_ok(n, c) for n, _s, _t, _d, c in _LOG),
        "boom_marker": _has_marker(by_name.get("BR_city_02"), (30,), "Red", "Boom in shot"),
    }


# -- 2. Promo assembly with music bed ----------------------------------------------------

_SHOT_SECONDS = 4
_PROMO_START = 10 * 3600 * FPS  # 10:00:00:00 at 25 fps


def _promo_setup(_r: Resolve, _project: Any, work: Path) -> None:
    for i, color in enumerate(("darkred", "darkgreen", "darkblue"), 1):
        _clip(work / f"shot_{i}.mov", color, _SHOT_SECONDS)
    _wav(work / "music.wav", 3 * _SHOT_SECONDS)


def _promo_prompt(work: Path) -> str:
    return (
        f"In the open DaVinci Resolve project, import {work}/shot_1.mov, {work}/shot_2.mov, "
        f"{work}/shot_3.mov and the music file {work}/music.wav. Build a 25 fps timeline named "
        "'Promo_v1' with start timecode 10:00:00:00 holding shot_1, shot_2 and shot_3 back to "
        "back in that order on video track 1 from the first frame of the timeline (their own "
        "audio stays on audio track 1). Place music.wav alone on audio track 2, starting at "
        "the first frame of the timeline, and rename audio track 2 to 'MUSIC'. Set the volume "
        "of the music clip to -8 dB and give it a 50-frame fade out. Finally add a Blue "
        "timeline marker named 'Shot 3' at timecode 10:00:08:00, where shot_3 begins."
    )


def _promo_check(_r: Resolve, project: Any, _work: Path) -> Check:
    tl = _timeline(project, "Promo_v1")
    if tl is None:
        return {"timeline_exists": False}
    start = int(_safe(tl.GetStartFrame, -1))
    v1 = list(_safe(lambda: tl.GetItemListInTrack("video", 1), []))
    a2 = list(_safe(lambda: tl.GetItemListInTrack("audio", 2), []))
    music = [it for it in a2 if _stem(_safe(it.GetName, "")) == "music"]
    item = music[0] if music else None
    props = dict(_safe(item.GetProperties, {})) if item is not None else {}
    volume = props.get("AudioVolume")
    if volume is None and item is not None:
        volume = _safe(lambda: item.GetProperty("AudioVolume"), None)
    fades = dict(_safe(item.GetFades, {})) if item is not None else {}
    shot3 = _PROMO_START + 2 * _SHOT_SECONDS * FPS
    try:
        volume_ok = abs(float(volume) + 8.0) <= 0.5
    except (TypeError, ValueError):
        volume_ok = False
    try:
        fade_ok = abs(int(fades.get("FadeOut", -999)) - 50) <= 1
    except (TypeError, ValueError):
        fade_ok = False
    return {
        "timeline_exists": True,
        "start_tc_10h": str(_safe(tl.GetStartTimecode, "")) == "10:00:00:00",
        "shots_in_order": [_stem(_safe(it.GetName, "")) for it in v1]
        == ["shot_1", "shot_2", "shot_3"]
        and int(_safe(v1[0].GetStart, -1)) == start,
        "music_on_a2_named": len(a2) == 1
        and item is not None
        and int(_safe(item.GetStart, -1)) == start
        and str(_safe(lambda: tl.GetTrackName("audio", 2), "")).strip() == "MUSIC",
        "music_minus_8db": volume_ok,
        "music_fade_out_50": fade_ok,
        "marker_shot3": _has_marker(tl, (shot3 - start, shot3), "Blue", "Shot 3"),
    }


# -- 3. Look development: versions, CDL, client still ------------------------------------

_SLOPE = (1.1, 1.0, 0.9)
_SAT = 1.2


def _look_setup(_r: Resolve, project: Any, work: Path) -> None:
    paths = []
    for i, color in enumerate(("lightslategray", "rosybrown", "darkkhaki"), 1):
        p = work / f"scene_{i}.mov"
        _clip(p, color, 3)
        paths.append(p)
    _import_timeline(project, "Grade_Review", paths)
    (work / "stills").mkdir(parents=True, exist_ok=True)


def _look_prompt(work: Path) -> str:
    return (
        "In the open DaVinci Resolve project, the timeline 'Grade_Review' holds three clips. "
        "On every clip of that timeline, add a new local color version named 'warm' and make "
        "it the active version; in that version set the CDL of the first node to slope "
        "1.1 1.0 0.9, offset 0 0 0, power 1 1 1 and saturation 1.2. Then create a new still "
        "album named 'Client Review', grab a still of the second clip into that album, and "
        f"export that still as a JPEG file into the folder {work}/stills."
    )


def _sop_matches(text: str) -> int:
    """Count ASC_SOP/ASC_SAT pairs in a CDL EDL that match the requested values."""
    nums = r"\(\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\)"
    sops = re.findall(r"ASC_SOP\s*" + nums + r"\s*" + nums + r"\s*" + nums, text)
    sats = re.findall(r"ASC_SAT\s+([-\d.]+)", text)
    want = (*_SLOPE, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
    count = 0
    for i, sop in enumerate(sops):
        values = [float(v) for v in sop]
        sat_ok = i < len(sats) and abs(float(sats[i]) - _SAT) <= 0.01
        if sat_ok and all(abs(a - b) <= 0.01 for a, b in zip(values, want, strict=True)):
            count += 1
    return count


def _look_check(r: Resolve, project: Any, work: Path) -> Check:
    tl = _timeline(project, "Grade_Review")
    items = _items(tl) if tl is not None else []

    def versions(it: Any) -> list[str]:
        return [str(v) for v in _safe(lambda: it.GetVersionNameList(0), [])]

    def current(it: Any) -> str:
        info = _safe(it.GetCurrentVersion, {})
        return str(dict(info).get("versionName", "")) if isinstance(info, dict) else ""

    cdl_ok = False
    if tl is not None:
        edl = work / ".check_cdl.edl"
        exported = _safe(lambda: tl.Export(str(edl), r.EXPORT_EDL, r.EXPORT_CDL), False)
        if exported and edl.is_file():
            cdl_ok = _sop_matches(edl.read_text(errors="replace")) >= 3

    gallery = _safe(project.GetGallery, None)
    album = None
    if gallery is not None:
        for alb in _safe(gallery.GetGalleryStillAlbums, []):
            if str(_safe(lambda a=alb: gallery.GetAlbumName(a), "")).strip() == "Client Review":
                album = alb
    stills = list(_safe(album.GetStills, [])) if album is not None else []
    jpegs = [
        p
        for p in (work / "stills").glob("*")
        if p.suffix.lower() in {".jpg", ".jpeg"} and p.read_bytes()[:2] == b"\xff\xd8"
    ]
    return {
        "warm_version_on_all": len(items) == 3 and all("warm" in versions(it) for it in items),
        "warm_is_active": len(items) == 3 and all(current(it) == "warm" for it in items),
        "cdl_values": cdl_ok,
        "album_with_still": album is not None and len(stills) >= 1,
        "jpeg_exported": bool(jpegs),
    }


# -- 4. QC copy, interchange and range delivery ------------------------------------------

_SPOT_START = 3600 * FPS  # 01:00:00:00


def _spot_setup(_r: Resolve, project: Any, work: Path) -> None:
    paths = []
    for i, color in enumerate(("gold", "orchid", "turquoise"), 1):
        p = work / f"spot_{i}.mov"
        _clip(p, color, 4)
        paths.append(p)
    tl = _import_timeline(project, "Spot", paths)
    tl.SetStartTimecode("01:00:00:00")
    for sub in ("handoff", "out"):
        (work / sub).mkdir(parents=True, exist_ok=True)


def _spot_prompt(work: Path) -> str:
    return (
        "In the open DaVinci Resolve project, the timeline 'Spot' (start timecode "
        "01:00:00:00) is approved and must stay unchanged. Duplicate it as a new timeline named "
        "'Spot_QC' and, on 'Spot_QC' only, add two timeline markers: a Red marker named "
        "'Logo too small' at 01:00:02:00 and a Yellow marker named 'Audio dip' at 01:00:07:00. "
        f"Export 'Spot_QC' as FCPXML 1.10 to {work}/handoff/Spot_QC.fcpxml and as "
        f"OpenTimelineIO to {work}/handoff/Spot_QC.otio. Then render only the range "
        "01:00:02:00 to 01:00:07:24 inclusive (150 frames) of 'Spot_QC' as a QuickTime "
        f"Apple ProRes 422 LT file at 1920x1080 named spot_qc.mov in the folder {work}/out, "
        "and wait until the render is finished."
    )


def _fcpxml_ok(path: Path) -> bool:
    if path.is_dir():  # tolerate an .fcpxmld bundle
        inner = sorted(path.rglob("*.fcpxml"))
        path = inner[0] if inner else path
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    clips = len(re.findall(r"<(?:asset-clip|clip)\b", text))
    return re.search(r"<fcpxml\b", text) is not None and clips >= 3


def _otio_ok(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(errors="replace"))
    except ValueError:
        return False
    text = json.dumps(data)
    return str(data.get("OTIO_SCHEMA", "")).startswith("Timeline") and text.count('"Clip.') >= 3


def _spot_check(_r: Resolve, project: Any, work: Path) -> Check:
    src = _timeline(project, "Spot")
    qc = _timeline(project, "Spot_QC")
    qc_start = int(_safe(qc.GetStartFrame, _SPOT_START)) if qc is not None else _SPOT_START
    red, yellow = 2 * FPS, 7 * FPS
    render = _probe_full(work / "out" / "spot_qc.mov")
    return {
        "source_untouched": src is not None
        and len(_items(src)) == 3
        and not _markers(src)
        and str(_safe(src.GetStartTimecode, "")) == "01:00:00:00",
        "qc_duplicate_with_markers": qc is not None
        and len(_items(qc)) == 3
        and qc_start == _SPOT_START
        and _has_marker(qc, (red, qc_start + red), "Red", "Logo too small")
        and _has_marker(qc, (yellow, qc_start + yellow), "Yellow", "Audio dip"),
        "fcpxml_exported": _fcpxml_ok(work / "handoff" / "Spot_QC.fcpxml")
        or _fcpxml_ok(work / "handoff" / "Spot_QC.fcpxmld"),
        "otio_exported": _otio_ok(work / "handoff" / "Spot_QC.otio"),
        # 1080p, not 720p: Resolve 21.1.1 fails ProRes LT renders downscaled to 720p on GPU.
        "render_prores_lt_1080": render.get("codec_name") == "prores"
        and "lt" in str(render.get("profile", "")).lower()
        and (render.get("width"), render.get("height")) == (1920, 1080),
        "render_150_frames": abs(int(render.get("frames", 0)) - 150) <= 1,
    }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "dailies", "Ingest et logging des rushes", _dailies_setup, _dailies_prompt, _dailies_check
    ),
    Scenario("promo", "Montage promo avec musique", _promo_setup, _promo_prompt, _promo_check),
    Scenario("look", "Look : versions, CDL, still client", _look_setup, _look_prompt, _look_check),
    Scenario(
        "spot_qc", "QC, interchange et livraison partielle", _spot_setup, _spot_prompt, _spot_check
    ),
)
