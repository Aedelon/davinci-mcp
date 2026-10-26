# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""A small Resolve world for the workflow domain: media pool, EDL import, render, subtitles.

Layers on the render, color and fusion fakes. The EDL import links an event to a pool clip only
when the reel/clip name matches AND the source-in lies in the clip's timecode range, like Resolve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from davinci_mcp.domains.interchange import timecode_to_frames
from tests.fakes.color import install_color
from tests.fakes.fake_resolve import (
    FakeFolder,
    FakeMediaPool,
    FakeMediaPoolItem,
    FakeResolve,
    FakeTimeline,
    FakeTimelineItem,
    make_resolve,
    recorded,
)
from tests.fakes.fusion import FakeComp, FakeTool, attach_fusion
from tests.fakes.render import FakeRenderProject

_EVENT = re.compile(r"^\d{3,6}\s+(\S+)\s+\S+\s+\S+")
_TC = re.compile(r"\d{2}:\d{2}:\d{2}[:;]\d{2}")


class WfClip(FakeMediaPoolItem):
    live_class = "MediaPoolItem"

    def __init__(
        self,
        root: FakeResolve,
        path: str,
        start_tc: str = "00:00:00:00",
        frames: int = 2400,
    ) -> None:
        super().__init__(root, Path(path).name)
        self._props.update({"File Path": path, "Start TC": start_tc, "Frames": str(frames)})
        self._props["FPS"] = "24"
        self.lie_start_tc = False  # SetClipProperty returns True without applying

    @recorded
    def SetClipProperty(self, key: str, value: str) -> bool:
        if not (key == "Start TC" and self.lie_start_tc):
            self._props[key] = value
        return True


class WfItem(FakeTimelineItem):
    live_class = "TimelineItem"

    def __init__(
        self, root: FakeResolve, name: str, start: int, end: int, clip: WfClip | None = None
    ) -> None:
        super().__init__(root, name, start, end)
        self.clip = clip

    @recorded
    def GetMediaPoolItem(self) -> WfClip | None:
        return self.clip


class WfTimeline(FakeTimeline):
    live_class = "Timeline"

    def __init__(self, root: FakeResolve, name: str, world: World) -> None:
        super().__init__(root, name, 1, 0)
        self.world = world
        self.subs: list[WfItem] = []
        self.timecode = "01:00:00:00"
        self.stick_resolution = True
        self._settings.update(
            {"timelineFrameRate": "24", "timelineDropFrameTimecode": "0", "useCustomSettings": "0"}
        )

    @recorded
    def GetTrackCount(self, track_type: str) -> int:
        if track_type == "video":
            return 1
        return 1 if track_type == "subtitle" and self.subs else 0

    @recorded
    def GetItemListInTrack(self, track_type: str, index: int) -> list[Any]:
        if index != 1:
            return []
        if track_type == "video":
            return list(self.tracks[0])
        return list(self.subs) if track_type == "subtitle" else []

    @recorded
    def SetSetting(self, key: str, value: Any) -> bool:
        if self.world.timeline_settings_stick:
            self._settings[key] = str(value)
        return True

    @recorded
    def SetCurrentTimecode(self, timecode: str) -> bool:
        self.timecode = timecode
        return True

    @recorded
    def GetCurrentTimecode(self) -> str:
        return self.timecode

    @recorded
    def CreateSubtitlesFromAudio(self, settings: dict[Any, Any] | None = None) -> Any:
        mode = self.world.subtitle_mode
        if mode == "extras":
            return "Required package is not installed"
        if mode == "refused":
            return False
        self.world.subtitle_target = self
        self.world.subtitle_polls = 0
        return True


class WfPool(FakeMediaPool):
    live_class = "MediaPool"

    def __init__(self, root: FakeResolve, world: World) -> None:
        super().__init__(root)
        self.world = world

    @recorded
    def ImportMedia(self, infos: list[Any]) -> list[WfClip] | None:
        self.world.import_calls.append(list(infos))
        made: list[WfClip] = []
        for path in infos:
            if isinstance(path, str) and Path(path).is_file():
                spec = self.world.clip_specs.get(Path(path).name, {})
                clip = WfClip(self._root, path, **spec)
                self._current.clips.append(clip)
                made.append(clip)
        return made or None

    def _project(self) -> WfProject:
        return self.world.project

    @recorded
    def CreateEmptyTimeline(self, name: str) -> WfTimeline | None:
        project = self._project()
        if any(t._name == name for t in project.timelines):
            return None
        tl = WfTimeline(self._root, name, self.world)
        for key in ("timelineResolutionWidth", "timelineResolutionHeight"):
            tl._settings[key] = project._settings[key]
        project.timelines.append(tl)
        project._current = tl
        self.world.created.append(name)
        return tl

    @recorded
    def DeleteTimelines(self, timelines: list[WfTimeline]) -> bool:
        project = self._project()
        for tl in timelines:
            project.timelines.remove(tl)
            if project._current is tl:
                project._current = project.timelines[0] if project.timelines else None
        self.world.deleted += len(timelines)
        return True

    @recorded
    def AppendToTimeline(self, clips: list[WfClip]) -> list[WfItem]:
        tl = self._project()._current
        assert isinstance(tl, WfTimeline)
        ordered = list(reversed(clips)) if self.world.reverse_append else list(clips)
        placed: list[WfItem] = []
        cursor = 86400 + sum(i._end - i._start for i in tl.tracks[0])
        for clip in ordered:
            frames = int(clip._props["Frames"])
            item = WfItem(self._root, clip._name, cursor, cursor + frames, clip)
            tl.tracks[0].append(item)
            placed.append(item)
            cursor += frames
        return placed

    @recorded
    def ImportTimelineFromFile(self, path: str, options: dict[str, Any] | None = None) -> Any:
        project = self._project()
        name = (options or {}).get("timelineName") or Path(path).stem
        if any(t._name == name for t in project.timelines):
            return None
        self.world.edl_imports += 1
        tl = WfTimeline(self._root, name, self.world)
        clips = [c for f in [self.root_folder, *self.root_folder.subfolders] for c in f.clips]
        events: list[tuple[str, str | None, str]] = []
        for line in Path(path).read_text().splitlines():
            if m := _EVENT.match(line):
                events.append((m.group(1), None, _TC.findall(line)[0]))
            elif line.startswith("* FROM CLIP NAME:") and events:
                reel, _, tc = events[-1]
                events[-1] = (reel, line.split(":", 1)[1].strip(), tc)
        cursor = 86400
        for reel, clip_name, src_in in events:
            linked = next(
                (c for c in clips if _links(cast("WfClip", c), reel, clip_name, src_in)), None
            )
            item = WfItem(
                self._root, clip_name or reel, cursor, cursor + 120, cast("WfClip | None", linked)
            )
            tl.tracks[0].append(item)
            cursor += 120
        project.timelines.append(tl)
        project._current = tl
        return tl


def _links(clip: WfClip, reel: str, clip_name: str | None, src_in: str) -> bool:
    name = clip._name.lower()
    stem = Path(name).stem
    wanted = {reel.lower(), (clip_name or "").lower()} - {""}
    if not wanted & {name, stem}:
        return False
    start = timecode_to_frames(str(clip._props["Start TC"]), 24.0)
    return start <= timecode_to_frames(src_in, 24.0) < start + int(clip._props["Frames"])


class WfProject(FakeRenderProject):
    live_class = "Project"

    def __init__(self, root: FakeResolve, world: World) -> None:
        super().__init__(root, "Test Project")
        self.world = world
        self.media_pool = WfPool(root, world)
        self.timelines = []
        self._settings.update(
            {"timelineResolutionWidth": "1920", "timelineResolutionHeight": "1080"}
        )
        self.skip_files = False  # finish jobs without writing the output file
        self.stall = False  # jobs never complete

    @recorded
    def SetSetting(self, key: str, value: Any) -> bool:
        self._settings[key] = str(value)
        return True

    @recorded
    def AddRenderJob(self) -> str:
        job_id = super().AddRenderJob.__wrapped__(self)  # type: ignore[attr-defined]
        job = self.jobs[job_id]
        job["OutputFilename"] = f"{job.get('CustomName', 'out')}.{self.fmt_codec['format']}"
        return job_id

    @recorded
    def GetRenderJobStatus(self, job_id: str) -> dict[str, Any]:
        if self.stall and job_id in self.jobs:
            return {"JobStatus": "Rendering", "CompletionPercentage": 10}
        status = super().GetRenderJobStatus.__wrapped__(self, job_id)  # type: ignore[attr-defined]
        job = self.jobs.get(job_id)
        if job and status.get("CompletionPercentage", 0) >= 100 and not self.skip_files:
            Path(job["TargetDir"], job["OutputFilename"]).write_bytes(b"render")
        return status

    @recorded
    def GetCreateSubtitlesFromAudioStatus(self) -> dict[str, Any]:
        world = self.world
        world.subtitle_polls += 1
        if world.subtitle_mode == "failed":
            return {"JobStatus": "Failed"}
        if world.subtitle_polls < 2:
            return {"JobStatus": "Analyzing", "CompletionPercentage": 50}
        tl = world.subtitle_target
        if tl is not None and not tl.subs:
            tl.subs = [
                WfItem(self._root, f"Sub {i}", 86400 + i * 24, 86400 + i * 24 + 24)
                for i in range(3)
            ]
        return {"JobStatus": "Complete", "CompletionPercentage": 100}


class WfTool(FakeTool):
    @recorded
    def SetInput(self, name: str, value: Any, time: float | None = None) -> bool:
        if name in getattr(self.comp, "lie_inputs", set()):
            return True
        return super().SetInput.__wrapped__(self, name, value, time)  # type: ignore[attr-defined]


class WfComp(FakeComp):
    def __init__(self, root: FakeResolve, name: str) -> None:
        super().__init__(root, name)
        self.lie_inputs: set[str] = set()

    def make_tool(self, reg_id: str, name: str | None = None) -> FakeTool:
        tool = super().make_tool(reg_id, name)
        wrapped = WfTool(tool._root, self, tool.ID, tool.Name, dict(tool.values))
        self.tools[self.tools.index(tool)] = wrapped
        return wrapped

    def with_media_io(self) -> WfComp:
        self.make_tool("MediaIn", "MediaIn1")
        self.make_tool("MediaOut", "MediaOut1")
        return self


@dataclass
class World:
    resolve: FakeResolve = field(default_factory=make_resolve)
    project: WfProject = field(init=False)
    clip_specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    import_calls: list[list[Any]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: int = 0
    edl_imports: int = 0
    timeline_settings_stick: bool = True
    subtitle_mode: str = "ok"  # ok | extras | refused | failed
    subtitle_polls: int = 0
    subtitle_target: WfTimeline | None = None
    reverse_append: bool = False
    comps: list[WfComp] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project = WfProject(self.resolve, self)
        manager = self.resolve.project_manager
        manager.projects["Test Project"] = self.project
        manager._current = self.project
        self.add_timeline("Timeline 1")

    def add_timeline(self, name: str, clips: int = 0, *, color: bool = False) -> WfTimeline:
        tl = WfTimeline(self.resolve, name, self)
        for i in range(clips):
            tl.tracks[0].append(
                WfItem(self.resolve, f"shot{i + 1}", 86400 + i * 100, 86500 + i * 100)
            )
        self.project.timelines.append(tl)
        self.project._current = tl
        if color:
            install_color(self.resolve)
        return tl

    def add_clip(self, path: Path, **spec: Any) -> WfClip:
        clip = WfClip(self.resolve, str(path), **spec)
        self.project.media_pool.root_folder.clips.append(clip)
        return clip

    def add_fusion(self, item: WfItem, *, existing: bool = False, media_io: bool = True) -> WfComp:
        """Fusion methods on ``item``; ``existing`` starts with one comp."""
        comps = attach_fusion(item, [])
        root = self.resolve
        count = {"n": 0}

        def fresh() -> WfComp:
            count["n"] += 1
            comp = WfComp(root, f"Composition {count['n']}")
            return comp.with_media_io() if media_io else comp

        def add_comp() -> WfComp:
            comp = fresh()
            comps.append(comp)  # type: ignore[arg-type]
            return comp

        item.AddFusionComp = add_comp  # type: ignore[attr-defined]
        if existing:
            add_comp()
        self.comps = comps  # type: ignore[assignment]
        return comps[0] if comps else None  # type: ignore[return-value]


def folder_of(world: World) -> FakeFolder:
    return world.project.media_pool.root_folder
