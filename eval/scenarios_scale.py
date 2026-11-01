# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Scale check: a project with 160 clips and 4 timelines of 40 items.

Measures whether a server stays usable (and its replies small) when listing everything
is no longer an option. Run with ``run_eval.py --suite scale``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from scenarios import Check, Resolve, Scenario, _clip, _timeline

CLIPS = 160
PER_TIMELINE = 40


def _name(i: int) -> str:
    return f"broll_{i:03d}" if i % 5 == 0 else f"shot_{i:03d}"


def _setup(_r: Resolve, project: Any, work: Path) -> None:
    project.SetSetting("timelineFrameRate", "25")
    seed = work / "seed.mov"
    _clip(seed, "gray", 1)
    media = work / "media"
    media.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(1, CLIPS + 1):
        path = media / f"{_name(i)}.mov"
        shutil.copyfile(seed, path)
        paths.append(str(path))
    pool = project.GetMediaPool()
    items = pool.ImportMedia(paths) or []
    by_name = {Path(it.GetName()).stem: it for it in items}
    for t in range(4):
        names = [_name(i) for i in range(t * PER_TIMELINE + 1, (t + 1) * PER_TIMELINE + 1)]
        pool.CreateTimelineFromClips(f"Edit_{t + 1:02d}", [by_name[n] for n in names])


def _prompt(_work: Path) -> str:
    return (
        "The open DaVinci Resolve project has 160 clips and four timelines, Edit_01 to "
        "Edit_04. In timeline 'Edit_03' only, set the clip color of every timeline item whose "
        "name starts with 'broll_' to Orange, and leave every other item unchanged. Then, in "
        "timeline 'Edit_02', add a Red timeline marker named 'Check' at the start of the item "
        "named 'shot_052'."
    )


def _items(tl: Any) -> list[Any]:
    return list(tl.GetItemListInTrack("video", 1) or []) if tl is not None else []


def _check(_r: Resolve, project: Any, _work: Path) -> Check:
    edit3, edit2 = _timeline(project, "Edit_03"), _timeline(project, "Edit_02")
    items3 = _items(edit3)
    broll = [it for it in items3 if Path(it.GetName()).stem.startswith("broll_")]
    others = [it for it in items3 if it not in broll]
    target = next((it for it in _items(edit2) if Path(it.GetName()).stem == "shot_052"), None)
    marks: dict[Any, Any] = dict(edit2.GetMarkers() or {}) if edit2 is not None else {}
    start = edit2.GetStartFrame() if edit2 is not None else 0
    at = None if target is None else target.GetStart() - start
    marker = next((m for f, m in marks.items() if at is not None and round(float(f)) == at), None)
    untouched = [_items(_timeline(project, n)) for n in ("Edit_01", "Edit_04")]
    return {
        "broll_orange": len(broll) == PER_TIMELINE // 5
        and all(it.GetClipColor() == "Orange" for it in broll),
        "others_unchanged": bool(others) and all(not it.GetClipColor() for it in others),
        "other_timelines_unchanged": all(
            not it.GetClipColor() for items in untouched for it in items
        ),
        "marker_at_shot_052": marker is not None
        and marker.get("color") == "Red"
        and marker.get("name") == "Check",
    }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("big_project", "Gros projet : 160 clips, 4 timelines", _setup, _prompt, _check),
)
