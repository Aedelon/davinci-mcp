# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""In-memory fake of the Resolve scripting API, shaped by live introspection data.

``dir()`` of every fake returns exactly the method list of the real class on Resolve
Studio 21.1.1. Names that are not implemented resolve to ``None`` like real proxies.
"""

from __future__ import annotations

import copy
import functools
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

_DATA = Path(__file__).resolve().parents[2] / "research" / "api-introspection"
LIVE_DIR: dict[str, list[str]] = json.loads((_DATA / "live-dir-21.1.1.10.json").read_text())
DUMPS: dict[str, Any] = json.loads((_DATA / "dumps-21.1.1.10.json").read_text())


def _dump(key: str) -> Any:
    return copy.deepcopy(DUMPS.get(key))


def recorded[F: Callable[..., Any]](fn: F) -> F:
    """Record the call and honour the ``ResolveDead`` toggle."""

    @functools.wraps(fn)
    def wrapper(self: FakeObject, *args: Any, **kwargs: Any) -> Any:
        if self._root.dead and self._root.dead_mode == "none":
            return None
        self._root.check_alive()
        self.calls.append((fn.__name__, args))
        self._root.all_calls.append((f"{type(self).__name__}.{fn.__name__}", args))
        return fn(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class FakeObject:
    """Base proxy: ``dir`` = live method list, unknown attributes are ``None``."""

    live_class: str = ""

    def __init__(self, root: FakeResolve | None = None) -> None:
        self._root: FakeResolve = root if root is not None else self  # type: ignore[assignment]
        self._id = uuid.uuid4().hex[:12]
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __dir__(self) -> list[str]:
        return list(LIVE_DIR.get(self.live_class or type(self).__name__.removeprefix("Fake"), []))

    def __str__(self) -> str:
        name = self.live_class or type(self).__name__.removeprefix("Fake")
        return f"{name} (0x{self._id}) [App: 'Resolve' on 127.0.0.1, UUID: fake]"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name == "calls":
            raise AttributeError(name)
        return None


class FakeMediaPoolItem(FakeObject):
    def __init__(self, root: FakeResolve, name: str = "clip.mov") -> None:
        super().__init__(root)
        self._uid = str(uuid.uuid4())
        self._name = name
        self._props: dict[str, Any] = _dump("MediaPoolItem.GetClipProperty") or {}
        self._props["Clip Name"] = name

    @recorded
    def GetUniqueId(self) -> str:
        return self._uid

    @recorded
    def GetName(self) -> str:
        return self._name

    @recorded
    def GetClipProperty(self, key: str | None = None) -> Any:
        return dict(self._props) if key is None else self._props.get(key)


class FakeFolder(FakeObject):
    def __init__(self, root: FakeResolve, name: str = "Master") -> None:
        super().__init__(root)
        self._uid = str(uuid.uuid4())
        self._name = name
        self.clips: list[FakeMediaPoolItem] = []
        self.subfolders: list[FakeFolder] = []

    @recorded
    def GetUniqueId(self) -> str:
        return self._uid

    @recorded
    def GetName(self) -> str:
        return self._name

    @recorded
    def GetClipList(self) -> list[FakeMediaPoolItem]:
        return list(self.clips)

    @recorded
    def GetSubFolderList(self) -> list[FakeFolder]:
        return list(self.subfolders)


class FakeMediaPool(FakeObject):
    def __init__(self, root: FakeResolve) -> None:
        super().__init__(root)
        self.root_folder = FakeFolder(root)
        self._current = self.root_folder

    @recorded
    def GetRootFolder(self) -> FakeFolder:
        return self.root_folder

    @recorded
    def GetCurrentFolder(self) -> FakeFolder:
        return self._current

    @recorded
    def SetCurrentFolder(self, folder: FakeFolder) -> bool:
        self._current = folder
        return True


class FakeTimelineItem(FakeObject):
    def __init__(self, root: FakeResolve, name: str, start: int, end: int) -> None:
        super().__init__(root)
        self._uid = str(uuid.uuid4())
        self._name = name
        self._start = start
        self._end = end
        self._props: dict[str, Any] = _dump("TimelineItem.GetProperty") or {}

    @recorded
    def GetUniqueId(self) -> str:
        return self._uid

    @recorded
    def GetName(self) -> str:
        return self._name

    @recorded
    def GetStart(self) -> int:
        return self._start

    @recorded
    def GetEnd(self) -> int:
        return self._end

    @recorded
    def GetDuration(self) -> int:
        return self._end - self._start

    @recorded
    def GetProperty(self, key: str | None = None) -> Any:
        return dict(self._props) if key is None else self._props.get(key)

    @recorded
    def SetProperty(self, key: str, value: Any) -> bool:
        if key not in self._props:
            return False
        self._props[key] = value
        return True


class FakeTimeline(FakeObject):
    def __init__(
        self, root: FakeResolve, name: str, tracks: int = 1, items_per_track: int = 3
    ) -> None:
        super().__init__(root)
        self._uid = str(uuid.uuid4())
        self._name = name
        self._start_frame = 86400
        self.markers: dict[int, dict[str, Any]] = {}
        self.tracks: list[list[FakeTimelineItem]] = [
            [
                FakeTimelineItem(root, f"item_{t}_{i}", 86400 + i * 100, 86400 + (i + 1) * 100)
                for i in range(items_per_track)
            ]
            for t in range(tracks)
        ]
        self._settings: dict[str, Any] = _dump("Timeline.GetSetting") or {}

    @recorded
    def GetName(self) -> str:
        return self._name

    @recorded
    def GetUniqueId(self) -> str:
        return self._uid

    @recorded
    def GetTrackCount(self, track_type: str) -> int:
        return len(self.tracks) if track_type == "video" else 0

    @recorded
    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        if track_type != "video" or not 1 <= index <= len(self.tracks):
            return []
        return list(self.tracks[index - 1])

    @recorded
    def GetStartFrame(self) -> int:
        return self._start_frame

    @recorded
    def GetEndFrame(self) -> int:
        last = max((i._end for t in self.tracks for i in t), default=self._start_frame)
        return last

    @recorded
    def AddMarker(  # noqa: PLR0917
        self,
        frame: int,
        color: str,
        name: str,
        note: str,
        duration: int,
        custom_data: str = "",
    ) -> bool:
        if frame in self.markers:
            return False
        self.markers[frame] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": custom_data,
        }
        return True

    @recorded
    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.markers)

    @recorded
    def DeleteMarkerAtFrame(self, frame: int) -> bool:
        return self.markers.pop(frame, None) is not None

    @recorded
    def DuplicateTimeline(self, name: str | None = None) -> FakeTimeline:
        copy_name = name or f"{self._name} copy"
        new = FakeTimeline(self._root, copy_name, len(self.tracks), 0)
        new.tracks = [list(t) for t in self.tracks]
        self._root.project_of_timeline(self).timelines.append(new)
        return new

    @recorded
    def GetSetting(self, key: str | None = None) -> Any:
        return dict(self._settings) if key is None else self._settings.get(key)


class FakeGallery(FakeObject):
    pass


class FakeProject(FakeObject):
    def __init__(self, root: FakeResolve, name: str, timelines: int, items_per_track: int) -> None:
        super().__init__(root)
        self._name = name
        self._settings: dict[str, Any] = _dump("Project.GetSetting") or {}
        self.media_pool = FakeMediaPool(root)
        self.gallery = FakeGallery(root)
        self.timelines: list[FakeTimeline] = [
            FakeTimeline(root, f"Timeline {i + 1}", 1, items_per_track) for i in range(timelines)
        ]
        self._current: FakeTimeline | None = self.timelines[0] if self.timelines else None

    @recorded
    def GetName(self) -> str:
        return self._name

    @recorded
    def GetMediaPool(self) -> FakeMediaPool:
        return self.media_pool

    @recorded
    def GetTimelineCount(self) -> int:
        return len(self.timelines)

    @recorded
    def GetTimelineByIndex(self, index: int) -> FakeTimeline | None:
        return self.timelines[index - 1] if 1 <= index <= len(self.timelines) else None

    @recorded
    def GetCurrentTimeline(self) -> FakeTimeline | None:
        return self._current

    @recorded
    def SetCurrentTimeline(self, timeline: FakeTimeline) -> bool:
        if timeline not in self.timelines:
            return False
        self._current = timeline
        return True

    @recorded
    def GetSetting(self, key: str | None = None) -> Any:
        return dict(self._settings) if key is None else self._settings.get(key)

    @recorded
    def GetSettings(self) -> dict[str, Any]:
        return dict(self._settings)

    @recorded
    def SetSetting(self, key: str, value: Any) -> bool:
        if key not in self._settings:
            return False
        self._settings[key] = str(value)
        return True

    @recorded
    def GetGallery(self) -> FakeGallery:
        return self.gallery


class FakeProjectManager(FakeObject):
    def __init__(self, root: FakeResolve) -> None:
        super().__init__(root)
        self.stuck = False
        self.projects: dict[str, FakeProject] = {}
        self._current: FakeProject | None = None

    @recorded
    def GetCurrentDatabase(self) -> dict[str, str] | None:
        if self.stuck:
            return None
        return {"DbType": "Disk", "DbName": "Local Database"}

    @recorded
    def GetCurrentProject(self) -> FakeProject | None:
        return self._current

    @recorded
    def CreateProject(self, name: str) -> FakeProject | None:
        if name in self.projects:
            return None
        project = FakeProject(self._root, name, 0, 0)
        self.projects[name] = project
        self._current = project
        return project

    @recorded
    def LoadProject(self, name: str) -> FakeProject | None:
        project = self.projects.get(name)
        if project is not None:
            self._current = project
        return project

    @recorded
    def CloseProject(self, project: FakeProject) -> bool:
        if self._current is project:
            self._current = None
        return True

    @recorded
    def DeleteProject(self, name: str) -> bool:
        project = self.projects.pop(name, None)
        if project is None:
            return False
        if self._current is project:
            self._current = None
        return True

    @recorded
    def SaveProject(self) -> bool:
        return self._current is not None

    @recorded
    def ExportProject(self, name: str, path: str, with_stills_and_luts: bool = True) -> bool:
        if name not in self.projects:
            return False
        Path(path).write_bytes(b"")
        return True

    @recorded
    def GetProjectListInCurrentFolder(self) -> list[str]:
        return list(self.projects)


class FakeMediaStorage(FakeObject):
    @recorded
    def GetMountedVolumeList(self) -> list[str]:
        return list(DUMPS.get("MediaStorage.GetMountedVolumeList") or [])


class FakeFusion(FakeObject):
    pass


class FakeResolve(FakeObject):
    """Root object. ``dead = True`` simulates Resolve quitting."""

    def __init__(self) -> None:
        super().__init__(None)
        self._root = self
        self.dead = False
        # "raise": every method raises; "none": methods return None (what proxies often do).
        self.dead_mode: Literal["raise", "none"] = "raise"
        self.all_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.project_manager = FakeProjectManager(self)
        self.media_storage = FakeMediaStorage(self)
        self.fusion = FakeFusion(self)
        self.page = "edit"

    def check_alive(self) -> None:
        if self.dead:
            raise RuntimeError("connection lost")

    def project_of_timeline(self, timeline: FakeTimeline) -> FakeProject:
        for project in self.project_manager.projects.values():
            if timeline in project.timelines:
                return project
        raise LookupError("timeline not attached to a project")

    @recorded
    def GetProductName(self) -> str:
        return "DaVinci Resolve Studio"

    @recorded
    def GetVersion(self) -> list[Any]:
        return [21, 1, 1, 10, ""]

    @recorded
    def GetVersionString(self) -> str:
        return "21.1.1.10"

    @recorded
    def IsStudio(self) -> bool:
        return True

    @recorded
    def GetProjectManager(self) -> FakeProjectManager:
        return self.project_manager

    @recorded
    def GetMediaStorage(self) -> FakeMediaStorage:
        return self.media_storage

    @recorded
    def GetCurrentPage(self) -> str:
        return self.page

    @recorded
    def OpenPage(self, page: str) -> bool:
        if page not in {"media", "cut", "edit", "fusion", "color", "fairlight", "deliver"}:
            return False
        self.page = page
        return True

    @recorded
    def Fusion(self) -> FakeFusion:
        return self.fusion


def make_resolve(
    with_project: bool = True, timelines: int = 1, items_per_track: int = 3
) -> FakeResolve:
    """Build a coherent fake Resolve, optionally with an open project and timelines."""
    resolve = FakeResolve()
    if with_project:
        project = FakeProject(resolve, "Test Project", timelines, items_per_track)
        resolve.project_manager.projects[project._name] = project
        resolve.project_manager._current = project
    return resolve
