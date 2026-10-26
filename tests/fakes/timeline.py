# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Richer Timeline / MediaPool behaviour on top of ``fake_resolve`` for the timeline domain.

``resolve.lie`` is a set of method names that return True without any effect, to
exercise read-back verification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.fakes.fake_resolve import (
    FakeMediaPool,
    FakeMediaPoolItem,
    FakeResolve,
    FakeTimeline,
    FakeTimelineItem,
    make_resolve,
    recorded,
)

EXPORT_CONSTANTS = {
    "EXPORT_EDL": 2,
    "EXPORT_AAF": 0,
    "EXPORT_OTIO": 14,
    "EXPORT_NONE": 100,
    "EXPORT_AAF_NEW": 101,
    "AUTO_ALIGN_CLIPS_USING_TIMECODE": 201,
    "AUTO_ALIGN_CLIPS_USING_WAVEFORM": 200,
}


def _lies(obj: Any, name: str) -> bool:
    return name in getattr(obj._root, "lie", set())


class RichTimeline(FakeTimeline):
    live_class = "Timeline"

    def _init_rich(self) -> None:
        self.audio_tracks: list[list[FakeTimelineItem]] = []
        self.track_names: dict[tuple[str, int], str] = {}
        self.track_enabled: dict[tuple[str, int], bool] = {}
        self.track_locked: dict[tuple[str, int], bool] = {}
        self.timecode = "01:00:00:00"
        self.start_tc = "01:00:00:00"
        self.marks: dict[str, dict[str, int]] = {}
        self.blanking = {"Top": 0, "Bottom": 0, "Left": 0, "Right": 0}
        self.selected: list[FakeTimelineItem] = []
        self.tl_settings: dict[str, Any] = {
            "timelineFrameRate": 24.0,
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
            "useCustomSettings": "1",
        }

    def _list(self, kind: str) -> list[list[FakeTimelineItem]] | None:
        return {"video": self.tracks, "audio": self.audio_tracks}.get(kind)

    @recorded
    def GetTrackCount(self, track_type: str) -> int:
        lst = self._list(track_type)
        return len(lst) if lst is not None else 0

    @recorded
    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        lst = self._list(track_type)
        if lst is None or not 1 <= index <= len(lst):
            return []
        return list(lst[index - 1])

    @recorded
    def DuplicateTimeline(self, name: str | None = None) -> RichTimeline:
        new = FakeTimeline.DuplicateTimeline(self, name)
        new.__class__ = RichTimeline
        new._init_rich()  # type: ignore[attr-defined]
        return new  # type: ignore[return-value]

    @recorded
    def SetName(self, name: str) -> bool:
        if _lies(self, "SetName"):
            return True
        self._name = name
        return True

    @recorded
    def GetStartTimecode(self) -> str:
        return self.start_tc

    @recorded
    def SetStartTimecode(self, tc: str) -> bool:
        if not _lies(self, "SetStartTimecode"):
            self.start_tc = tc
        return True

    @recorded
    def GetSettings(self) -> dict[str, Any]:
        return dict(self.tl_settings)

    @recorded
    def SetSettings(self, settings: dict[str, Any]) -> bool:
        if _lies(self, "SetSettings"):
            return True
        if "timelineFrameRate" in settings:
            return False
        self.tl_settings.update(settings)
        return True

    @recorded
    def GetTrackName(self, kind: str, index: int) -> str:
        return self.track_names.get((kind, index), f"{kind[0].upper()}{index}")

    @recorded
    def SetTrackName(self, kind: str, index: int, name: str) -> bool:
        if not _lies(self, "SetTrackName"):
            self.track_names[(kind, index)] = name
        return True

    @recorded
    def GetIsTrackEnabled(self, kind: str, index: int) -> bool:
        return self.track_enabled.get((kind, index), True)

    @recorded
    def SetTrackEnable(self, kind: str, index: int, enabled: bool) -> bool:
        if not _lies(self, "SetTrackEnable"):
            self.track_enabled[(kind, index)] = enabled
        return True

    @recorded
    def GetIsTrackLocked(self, kind: str, index: int) -> bool:
        return self.track_locked.get((kind, index), False)

    @recorded
    def SetTrackLock(self, kind: str, index: int, locked: bool) -> bool:
        if not _lies(self, "SetTrackLock"):
            self.track_locked[(kind, index)] = locked
        return True

    @recorded
    def GetTrackSubType(self, kind: str, index: int) -> str:
        return "stereo"

    @recorded
    def AddTrack(self, kind: str, sub: str | None = None) -> bool:
        lst = self._list(kind)
        if lst is None:
            return False
        if not _lies(self, "AddTrack"):
            lst.append([])
        return True

    @recorded
    def DeleteTrack(self, kind: str, index: int) -> bool:
        lst = self._list(kind)
        if lst is None or not 1 <= index <= len(lst):
            return False
        if not _lies(self, "DeleteTrack"):
            lst.pop(index - 1)
        return True

    @recorded
    def GetCurrentTimecode(self) -> str:
        return self.timecode

    @recorded
    def SetCurrentTimecode(self, tc: str) -> bool:
        if not _lies(self, "SetCurrentTimecode"):
            self.timecode = tc
        return True

    @recorded
    def GetCurrentVideoItem(self) -> FakeTimelineItem | None:
        return self.tracks[0][0] if self.tracks and self.tracks[0] else None

    @recorded
    def GetSelectedClips(self) -> list[FakeTimelineItem]:
        return list(self.selected)

    @recorded
    def GetMarkInOut(self) -> dict[str, dict[str, int]]:
        return dict(self.marks)

    @recorded
    def SetMarkInOut(self, a: int, b: int, kind: str | None = None) -> bool:
        if _lies(self, "SetMarkInOut"):
            return True
        for k in ("video", "audio") if kind in (None, "all") else (kind or "",):
            self.marks[k] = {"in": a, "out": b}
        return True

    @recorded
    def ClearMarkInOut(self, kind: str | None = None) -> bool:
        if _lies(self, "ClearMarkInOut"):
            return True
        for k in ("video", "audio") if kind in (None, "all") else (kind or "",):
            self.marks.pop(k, None)
        return True

    @recorded
    def UpdateMarkerCustomData(self, frame: int, data: str) -> bool:
        if frame not in self.markers:
            return False
        if not _lies(self, "UpdateMarkerCustomData"):
            self.markers[frame]["customData"] = data
        return True

    @recorded
    def DeleteMarkersByColor(self, color: str) -> bool:
        if _lies(self, "DeleteMarkersByColor"):
            return True
        self.markers = {
            f: m for f, m in self.markers.items() if color != "All" and m["color"] != color
        }
        return True

    @recorded
    def DeleteClips(self, items: list[FakeTimelineItem], ripple: bool = False) -> bool:
        if _lies(self, "DeleteClips"):
            return True
        for track in (*self.tracks, *self.audio_tracks):
            track[:] = [i for i in track if i not in items]
        return True

    def _insert(self, name: str) -> FakeTimelineItem | None:
        if self.track_locked.get(("video", 1)):
            return None
        item = FakeTimelineItem(self._root, name, 90000, 90100)
        if not _lies(self, "Insert"):
            self.tracks[0].append(item)
        return item

    @recorded
    def InsertGeneratorIntoTimeline(self, name: str) -> FakeTimelineItem | None:
        return self._insert(name)

    @recorded
    def InsertTitleIntoTimeline(self, name: str) -> FakeTimelineItem | None:
        return self._insert(name)

    @recorded
    def InsertFusionCompositionIntoTimeline(self) -> FakeTimelineItem | None:
        return self._insert("Fusion Composition")

    @recorded
    def CreateCompoundClip(
        self, items: list[FakeTimelineItem], info: dict[str, Any] | None = None
    ) -> FakeTimelineItem:
        name = (info or {}).get("name", "Compound Clip")
        for track in self.tracks:
            track[:] = [i for i in track if i not in items]
        compound = FakeTimelineItem(self._root, name, items[0]._start, items[-1]._end)
        self.tracks[0].append(compound)
        return compound

    @recorded
    def SetClipsLinked(self, items: list[FakeTimelineItem], linked: bool) -> bool:
        return True

    @recorded
    def AutoAlignClips(self, items: list[FakeTimelineItem], options: dict[str, Any]) -> bool:
        self.align_options = options
        return True

    @recorded
    def ConvertTimelineToStereo(self) -> bool:
        return True

    @recorded
    def SetOutputBlanking(self, blanking: dict[str, int]) -> bool:
        if not _lies(self, "SetOutputBlanking"):
            self.blanking = dict(blanking)
        return True

    @recorded
    def GetOutputBlanking(self) -> dict[str, int]:
        return dict(self.blanking)

    @recorded
    def Export(self, name: str, etype: Any, subtype: Any) -> bool:
        if etype is None or subtype is None:
            return False
        self.export_args = (etype, subtype)
        if not _lies(self, "Export"):
            Path(name).write_text("EDL")
        return True

    @recorded
    def ImportIntoTimeline(self, path: str, options: dict[str, Any] | None = None) -> bool:
        self.tracks[0].append(FakeTimelineItem(self._root, "imported", 95000, 95100))
        return True


class RichPool(FakeMediaPool):
    live_class = "MediaPool"

    def _project(self) -> Any:
        return self._root.project_manager._current

    def _new(self, name: str) -> RichTimeline:
        project = self._project()
        tl = FakeTimeline(self._root, name, 1, 0)
        tl.__class__ = RichTimeline
        tl._init_rich()  # type: ignore[attr-defined]
        project.timelines.append(tl)
        project._current = tl
        return tl  # type: ignore[return-value]

    @recorded
    def CreateEmptyTimeline(self, name: str) -> RichTimeline | None:
        if name in [t._name for t in self._project().timelines]:
            return None
        return self._new(name)

    @recorded
    def CreateTimelineFromClips(
        self, name: str, infos: list[dict[str, Any]]
    ) -> RichTimeline | None:
        if name in [t._name for t in self._project().timelines]:
            return None
        tl = self._new(name)
        for info in infos:
            tl.tracks[0].append(FakeTimelineItem(self._root, info["mediaPoolItem"]._name, 0, 100))
        return tl

    @recorded
    def AppendToTimeline(self, infos: list[dict[str, Any]]) -> list[FakeTimelineItem]:
        tl = self._project()._current
        out: list[FakeTimelineItem] = []
        self.append_infos = infos
        for info in infos:
            start = int(info.get("recordFrame", tl._start_frame))
            length = int(info.get("endFrame", 100)) - int(info.get("startFrame", 0))
            item = FakeTimelineItem(self._root, info["mediaPoolItem"]._name, start, start + length)
            if not _lies(self, "AppendToTimeline"):
                tl.tracks[info.get("trackIndex", 1) - 1].append(item)
            out.append(item)
        return out

    @recorded
    def ImportTimelineFromFile(self, path: str, options: dict[str, Any] | None = None) -> Any:
        if not Path(path).exists():
            return None
        name = (options or {}).get("timelineName", "Imported")
        existing = [t for t in self._project().timelines if t._name == name]
        if existing and name == "Imported":
            return existing[0]  # FCP7-like: returns the existing timeline
        return self._new(name)

    @recorded
    def DeleteTimelines(self, timelines: list[FakeTimeline]) -> bool:
        if _lies(self, "DeleteTimelines"):
            return True
        project = self._project()
        project.timelines[:] = [t for t in project.timelines if t not in timelines]
        return True


def make_timeline_resolve(timelines: int = 2, items_per_track: int = 3) -> FakeResolve:
    """Fake Resolve whose timelines and media pool support the timeline domain."""
    resolve = make_resolve(timelines=timelines, items_per_track=items_per_track)
    resolve.lie = set()  # type: ignore[attr-defined]
    for name, value in EXPORT_CONSTANTS.items():
        setattr(resolve, name, value)
    project = resolve.project_manager._current
    assert project is not None
    for tl in project.timelines:
        tl.__class__ = RichTimeline
        tl._init_rich()  # type: ignore[attr-defined]
    project.media_pool.__class__ = RichPool
    project.GetUniqueId = lambda: "project-1"  # type: ignore[method-assign]
    return resolve


def add_pool_clips(resolve: FakeResolve, *names: str) -> list[FakeMediaPoolItem]:
    project = resolve.project_manager._current
    assert project is not None
    clips = [FakeMediaPoolItem(resolve, n) for n in names]
    project.media_pool.root_folder.clips.extend(clips)
    return clips
