# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Fake TimelineItem with the 21.1 behaviour needed by the item domain tests."""

from __future__ import annotations

import copy
from typing import Any, get_args

from davinci_mcp.api._generated import resolve_21_1 as gen
from tests.fakes.fake_resolve import (
    FakeMediaPoolItem,
    FakeResolve,
    FakeTimeline,
    FakeTimelineItem,
    make_resolve,
    recorded,
)

_CONST_ENUMS = (
    gen.DynamicZoomEase,
    gen.CompositeMode,
    gen.RetimeProcess,
    gen.MotionEstimation,
    gen.Scaling,
    gen.ResizeFilter,
    gen.FlattenMulticamGrade,
    gen.SmartSwitchQuality,
    gen.SmartSwitchWideAngleFrequency,
    gen.SmartSwitchAnalysisMode,
)


class FakeItem(FakeTimelineItem):
    """Stateful TimelineItem. Names in ``lies`` return True without applying."""

    live_class = "TimelineItem"

    def __init__(self, root: FakeResolve, name: str, start: int, end: int) -> None:
        super().__init__(root, name, start, end)
        self.lies: set[str] = set()
        self.track: list[FakeItem] = []
        self.enabled = True
        self.color = ""
        self.flags: list[str] = []
        self.speed: dict[str, Any] = {"Percentage": 100.0, "PitchCorrection": False}
        self.fades: dict[str, Any] = {"FadeIn": 0, "FadeOut": 0}
        self.markers: dict[int, dict[str, Any]] = {}
        self.takes: list[dict[str, Any]] = []
        self.selected_take = 0
        self.blanking: dict[str, Any] = {}
        self.use_timeline_blanking = True
        self.no_handles = False
        self.multicam_calls: list[tuple[str, Any]] = []

    def _lie(self, name: str) -> bool:
        return name in self.lies

    @recorded
    def GetType(self) -> str:
        return "video"

    @recorded
    def SetName(self, name: str) -> bool:
        if not self._lie("SetName"):
            self._name = name
        return True

    @recorded
    def GetLeftOffset(self) -> int:
        return 10

    @recorded
    def GetRightOffset(self) -> int:
        return 20

    @recorded
    def GetSourceStartFrame(self) -> int:
        return 0

    @recorded
    def GetSourceEndFrame(self) -> int:
        return self._end - self._start

    @recorded
    def GetSourceStartTime(self) -> float:
        return 0.0

    @recorded
    def GetSourceEndTime(self) -> float:
        return 4.0

    @recorded
    def GetTrackTypeAndIndex(self) -> list[Any]:
        return ["video", 1]

    @recorded
    def GetClipEnabled(self) -> bool:
        return self.enabled

    @recorded
    def SetClipEnabled(self, enabled: bool) -> bool:
        if not self._lie("SetClipEnabled"):
            self.enabled = enabled
        return True

    @recorded
    def GetClipColor(self) -> str:
        return self.color

    @recorded
    def SetClipColor(self, color: str) -> bool:
        if not self._lie("SetClipColor"):
            self.color = color
        return True

    @recorded
    def ClearClipColor(self) -> bool:
        if not self._lie("ClearClipColor"):
            self.color = ""
        return True

    @recorded
    def GetFlagList(self) -> list[str]:
        return list(self.flags)

    @recorded
    def AddFlag(self, color: str) -> bool:
        if not self._lie("AddFlag"):
            self.flags.append(color)
        return True

    @recorded
    def ClearFlags(self, color: str) -> bool:
        if not self._lie("ClearFlags"):
            self.flags = [] if color == "All" else [f for f in self.flags if f != color]
        return True

    @recorded
    def GetLinkedItems(self) -> list[FakeItem]:
        return [i for i in self.track if i is not self][:1]

    @recorded
    def GetMediaPoolItem(self) -> FakeMediaPoolItem | None:
        return None

    @recorded
    def GetProperties(self) -> dict[str, Any]:
        return dict(self._props)

    @recorded
    def SetProperties(self, props: dict[str, Any]) -> bool:
        if self._lie("SetProperties"):
            return True
        for k, v in props.items():
            if k in self._props:
                self._props[k] = v
        return True

    @recorded
    def GetSpeed(self) -> dict[str, Any]:
        return dict(self.speed)

    @recorded
    def SetSpeed(self, opts: dict[str, Any]) -> bool:
        if not self._lie("SetSpeed"):
            self.speed.update(opts)
        return True

    @recorded
    def GetFades(self) -> dict[str, Any]:
        return dict(self.fades)

    @recorded
    def SetFades(self, fades: dict[str, Any]) -> bool:
        if not self._lie("SetFades"):
            self.fades.update(fades)
        return True

    @recorded
    def AddTransition(self, opts: dict[str, Any]) -> FakeItem | None:
        if self.no_handles:
            return None
        tr = FakeItem(self._root, opts.get("type", "Cross Dissolve"), self._end, self._end + 10)
        tr.track = self.track
        if not self._lie("AddTransition"):
            self.track.append(tr)
        return tr

    @recorded
    def Stabilize(self) -> bool:
        return not self._lie("Stabilize")

    @recorded
    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.markers)

    @recorded
    def AddMarker(  # noqa: PLR0917
        self, frame: int, color: str, name: str, note: str, duration: int, custom: str | None = None
    ) -> bool:
        if self._lie("AddMarker"):
            return True
        self.markers[frame] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": custom or "",
        }
        return True

    @recorded
    def UpdateMarkerCustomData(self, frame: int, data: str) -> bool:
        if frame not in self.markers:
            return False
        if not self._lie("UpdateMarkerCustomData"):
            self.markers[frame]["customData"] = data
        return True

    @recorded
    def GetMarkerCustomData(self, frame: int) -> str:
        return self.markers.get(frame, {}).get("customData", "")

    @recorded
    def DeleteMarkerAtFrame(self, frame: int) -> bool:
        if self._lie("DeleteMarkerAtFrame"):
            return True
        return self.markers.pop(frame, None) is not None

    @recorded
    def DeleteMarkersByColor(self, color: str) -> bool:
        self.markers = {
            f: m for f, m in self.markers.items() if color != "All" and m["color"] != color
        }
        return True

    @recorded
    def DeleteMarkerByCustomData(self, data: str) -> bool:
        for frame, marker in list(self.markers.items()):
            if marker["customData"] == data:
                del self.markers[frame]
                return True
        return False

    @recorded
    def AddTake(
        self, clip: FakeMediaPoolItem, start: int | None = None, end: int | None = None
    ) -> bool:
        if self._lie("AddTake"):
            return True
        self.takes.append({"startFrame": start or 0, "endFrame": end or 99, "mediaPoolItem": clip})
        self.selected_take = self.selected_take or 1
        return True

    @recorded
    def GetTakesCount(self) -> int:
        return len(self.takes)

    @recorded
    def GetSelectedTakeIndex(self) -> int:
        return self.selected_take

    @recorded
    def GetTakeByIndex(self, idx: int) -> dict[str, Any] | None:
        return self.takes[idx - 1] if 1 <= idx <= len(self.takes) else None

    @recorded
    def SelectTakeByIndex(self, idx: int) -> bool:
        if not self._lie("SelectTakeByIndex"):
            self.selected_take = idx
        return True

    @recorded
    def FinalizeTake(self) -> bool:
        self.takes = []
        self.selected_take = 0
        return True

    @recorded
    def DeleteTakeByIndex(self, idx: int) -> bool:
        if not self._lie("DeleteTakeByIndex"):
            del self.takes[idx - 1]
        return True

    @recorded
    def GetOutputBlanking(self) -> dict[str, Any]:
        return {} if self.use_timeline_blanking else dict(self.blanking)

    @recorded
    def SetOutputBlanking(self, blanking: dict[str, Any]) -> bool:
        self.blanking = dict(blanking)
        return True

    @recorded
    def GetUseTimelineForOutputBlanking(self) -> bool:
        return self.use_timeline_blanking

    @recorded
    def SetUseTimelineForOutputBlanking(self, use: bool) -> bool:
        if not self._lie("SetUseTimelineForOutputBlanking"):
            self.use_timeline_blanking = use
        return True

    @recorded
    def FlattenMulticam(self, grade: Any) -> bool:
        self.multicam_calls.append(("flatten", grade))
        return True

    @recorded
    def PerformMulticamSmartSwitch(self, settings: dict[str, Any]) -> bool:
        self.multicam_calls.append(("smart_switch", settings))
        return True


def make_item_resolve(items: int = 3) -> FakeResolve:
    """Fake Resolve whose first timeline holds FakeItem instances and defines the constants."""
    resolve = make_resolve(items_per_track=items)
    for enum in _CONST_ENUMS:
        for value, name in enumerate(get_args(enum), start=1):
            setattr(resolve, name, value)
    project = resolve.project_manager._current
    assert project is not None
    # The base fake project has no GetUniqueId (needed by the Media Pool clip index).
    project.GetUniqueId = lambda: "fake-project-id"  # type: ignore[method-assign]
    timeline: FakeTimeline = project.timelines[0]
    track: list[FakeItem] = []
    for old in timeline.tracks[0]:
        item = FakeItem(resolve, old._name, old._start, old._end)
        item.track = track
        track.append(item)
    timeline.tracks[0] = track  # type: ignore[assignment]
    project.media_pool.root_folder.clips.append(FakeMediaPoolItem(resolve, "take.mov"))
    return resolve


def first_items(resolve: FakeResolve) -> list[FakeItem]:
    project = resolve.project_manager._current
    assert project is not None
    return list(project.timelines[0].tracks[0])  # type: ignore[arg-type]
