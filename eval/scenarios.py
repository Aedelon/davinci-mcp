# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""The four reference scenarios of the cadrage, with setup and independent checks.

Setup and checks talk to Resolve directly (DaVinciResolveScript), never through the
MCP server under test, so a server cannot grade itself.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

type Resolve = Any
type Check = dict[str, bool]

SPEECH = (
    "Welcome to HitScroll, we turn your track into videos.",
    "Every day, new clips, ready to post.",
    "Comment DEMO to try it today.",
)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, capture_output=True)


def _clip(path: Path, color: str, seconds: float, speech: str | None = None) -> None:
    if speech is None:
        _run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=1920x1080:r=25:d={seconds}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=330:duration={seconds}",
                "-c:v",
                "prores_ks",
                "-c:a",
                "pcm_s16le",
                "-shortest",
                str(path),
            ]
        )
        return
    voice = path.with_suffix(".aiff")
    _run(["say", "-o", str(voice), speech])
    _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1920x1080:r=25:d={seconds}",
            "-i",
            str(voice),
            "-c:v",
            "prores_ks",
            "-c:a",
            "pcm_s16le",
            "-af",
            "apad",
            "-t",
            str(seconds),
            str(path),
        ]
    )
    voice.unlink()


def _probe(path: Path) -> dict[str, Any]:
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
            "stream=codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    ).stdout
    streams = json.loads(out or "{}").get("streams") or [{}]
    return streams[0]


def _timeline(project: Any, name: str) -> Any:
    for i in range(1, int(project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl.GetName() == name:
            return tl
    return None


def _items(timeline: Any, kind: str = "video") -> list[Any]:
    out: list[Any] = []
    for track in range(1, int(timeline.GetTrackCount(kind) or 0) + 1):
        out.extend(timeline.GetItemListInTrack(kind, track) or [])
    return out


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    setup: Callable[[Resolve, Any, Path], None]
    prompt: Callable[[Path], str]
    check: Callable[[Resolve, Any, Path], Check]


# -- 1. Vertical subtitled Reel ----------------------------------------------------------


def _reel_setup(_r: Resolve, _project: Any, work: Path) -> None:
    for i, (color, text) in enumerate(zip(("navy", "teal", "purple"), SPEECH, strict=True), 1):
        _clip(work / f"reel_{i}.mov", color, 4, text)


def _reel_prompt(work: Path) -> str:
    return (
        f"In the open DaVinci Resolve project, import the three clips {work}/reel_1.mov, "
        f"{work}/reel_2.mov and {work}/reel_3.mov. Create a VERTICAL timeline named 'Reel' "
        "(1080 wide x 1920 high) containing the three clips in that order on video track 1. "
        "Generate subtitles from the audio of that timeline. Then render the whole timeline "
        f"as an H.264 MP4 file named reel.mp4 in the folder {work}/out and wait until the "
        "render is finished."
    )


def _reel_check(_r: Resolve, project: Any, work: Path) -> Check:
    tl = _timeline(project, "Reel")
    if tl is None:
        return {"timeline_exists": False}
    names = [it.GetName() for it in _items(tl)]
    probe = _probe(work / "out" / "reel.mp4")
    return {
        "timeline_exists": True,
        "vertical_1080x1920": (
            str(tl.GetSetting("timelineResolutionWidth")),
            str(tl.GetSetting("timelineResolutionHeight")),
        )
        == ("1080", "1920"),
        "three_clips_in_order": [n.split(".")[0] for n in names] == ["reel_1", "reel_2", "reel_3"],
        "subtitles_created": len(_items(tl, "subtitle")) > 0,
        "render_h264": probe.get("codec_name") == "h264",
        "render_vertical": (probe.get("width"), probe.get("height")) == (1080, 1920),
    }


# -- 2. Conform & delivery ---------------------------------------------------------------

_EDL_EVENTS = (("A001", 0, 2), ("A002", 1, 3), ("A003", 0, 1.5), ("A004", 2, 4))


def _tc(seconds: float, fps: int = 25) -> str:
    frames = round(seconds * fps)
    return (
        f"{frames // (3600 * fps):02d}:{frames // (60 * fps) % 60:02d}:"
        f"{frames // fps % 60:02d}:{frames % fps:02d}"
    )


def _conform_setup(_r: Resolve, _project: Any, work: Path) -> None:
    for name, color in zip(
        ("A001", "A002", "A003", "A004"), ("red", "green", "blue", "gray"), strict=True
    ):
        _clip(work / f"{name}.mov", color, 5)
    lines = ["TITLE: CONFORM", "FCM: NON-DROP FRAME", ""]
    rec = 3600.0
    for n, (reel, src_in, src_out) in enumerate(_EDL_EVENTS, 1):
        dur = src_out - src_in
        lines += [
            f"{n:03d}  {reel}     V     C        {_tc(3600 + src_in)} {_tc(3600 + src_out)} "
            f"{_tc(rec)} {_tc(rec + dur)}",
            f"* FROM CLIP NAME: {reel}.mov",
            "",
        ]
        rec += dur
    (work / "conform.edl").write_text("\n".join(lines))


def _conform_prompt(work: Path) -> str:
    return (
        f"In the open DaVinci Resolve project, import the media files A001.mov to A004.mov "
        f"from {work}, then import the edit decision list {work}/conform.edl as a new timeline "
        "named 'Conform' linked to that media. Deliver two renders of the Conform timeline "
        f"into {work}/out: a ProRes 422 HQ master at 1920x1080 named master.mov, and an "
        "H.264 proxy at 1280x720 named proxy.mp4. Wait until both renders are finished."
    )


def _conform_check(_r: Resolve, project: Any, work: Path) -> Check:
    tl = _timeline(project, "Conform")
    master = _probe(work / "out" / "master.mov")
    proxy = _probe(work / "out" / "proxy.mp4")
    items = _items(tl) if tl is not None else []
    return {
        "timeline_exists": tl is not None,
        "four_events": len(items) == len(_EDL_EVENTS),
        "events_linked": bool(items) and all(it.GetMediaPoolItem() is not None for it in items),
        "master_prores_1080": master.get("codec_name") == "prores"
        and (master.get("width"), master.get("height")) == (1920, 1080),
        "proxy_h264_720": proxy.get("codec_name") == "h264"
        and (proxy.get("width"), proxy.get("height")) == (1280, 720),
    }


# -- 3. Series grading -------------------------------------------------------------------


def _write_cube(path: Path, size: int = 9) -> None:
    rows = ['TITLE "eval_warm"', f"LUT_3D_SIZE {size}"]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rr, gg, bb = r / (size - 1), g / (size - 1), b / (size - 1)
                rows.append(f"{min(1, rr * 1.08):.6f} {gg:.6f} {bb * 0.92:.6f}")
    path.write_text("\n".join(rows) + "\n")


def _grade_setup(_r: Resolve, project: Any, work: Path) -> None:
    clips = []
    for i, color in enumerate(("orange", "salmon", "khaki", "tan"), 1):
        p = work / f"interview_{i}.mov"
        _clip(p, color, 3)
        clips.append(str(p))
    pool = project.GetMediaPool()
    items = pool.ImportMedia(clips)
    tl = pool.CreateTimelineFromClips("Interview", items)
    project.SetCurrentTimeline(tl)
    _write_cube(work / "eval_warm.cube")


def _grade_prompt(work: Path) -> str:
    return (
        "In the open DaVinci Resolve project, the timeline 'Interview' holds four clips. "
        f"Apply the LUT file {work}/eval_warm.cube on the first color node of every clip "
        "of that timeline, create a color group named 'Interview' and put all four clips in "
        "it, then grab a still of the first clip into the gallery."
    )


def _grade_check(_r: Resolve, project: Any, _work: Path) -> Check:
    tl = _timeline(project, "Interview")
    items = _items(tl) if tl is not None else []
    luts = [str(it.GetNodeGraph().GetLUT(1) or "") for it in items]
    groups = {g.GetName(): g for g in project.GetColorGroupsList() or []}
    group = groups.get("Interview")
    members = list(group.GetClipsInTimeline(tl) or []) if group is not None and tl else []
    album = project.GetGallery().GetCurrentStillAlbum()
    stills = list(album.GetStills() or []) if album is not None else []
    return {
        "lut_on_all_clips": len(items) == 4 and all("eval_warm" in lut for lut in luts),
        "color_group_exists": group is not None,
        "four_clips_in_group": len(members) == 4,
        "still_grabbed": len(stills) > 0,
    }


# -- 4. Fusion motion design -------------------------------------------------------------


def _fusion_setup(_r: Resolve, project: Any, work: Path) -> None:
    p = work / "plate.mov"
    _clip(p, "black", 4)
    pool = project.GetMediaPool()
    items = pool.ImportMedia([str(p)])
    project.SetCurrentTimeline(pool.CreateTimelineFromClips("Motion", items))


def _fusion_prompt(_work: Path) -> str:
    return (
        "In the open DaVinci Resolve project, the timeline 'Motion' holds one clip. Add a "
        "Fusion composition to that clip containing a Text+ title that reads HITSCROLL, "
        "merged over the clip (the merge result feeds the composition output), and animate "
        "the title's text Size with keyframes from 0 at frame 0 to 0.1 at frame 24."
    )


def _fusion_check(_r: Resolve, project: Any, _work: Path) -> Check:
    tl = _timeline(project, "Motion")
    items = _items(tl) if tl is not None else []
    item = items[0] if items else None
    count = int(item.GetFusionCompCount() or 0) if item is not None else 0
    text_ok = animated = merge_ok = False
    for i in range(1, count + 1):
        comp = item.GetFusionCompByIndex(i)
        tools = list((comp.GetToolList(False) or {}).values())
        for tool in tools:
            reg = str(tool.GetAttrs().get("TOOLS_RegID", ""))
            if reg == "TextPlus":
                text_ok = text_ok or "HITSCROLL" in str(tool.GetInput("StyledText") or "")
                size = tool.Size
                animated = animated or (size is not None and size.GetConnectedOutput() is not None)
            if reg == "Merge":
                merge_ok = True
    return {
        "fusion_comp_added": count > 0,
        "textplus_hitscroll": text_ok,
        "size_animated": animated,
        "merge_present": merge_ok,
    }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("reel", "Reel vertical sous-titré", _reel_setup, _reel_prompt, _reel_check),
    Scenario(
        "conform", "Conformation / livraison", _conform_setup, _conform_prompt, _conform_check
    ),
    Scenario("grade", "Étalonnage de série", _grade_setup, _grade_prompt, _grade_check),
    Scenario("fusion", "Motion design Fusion", _fusion_setup, _fusion_prompt, _fusion_check),
)
