# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Stateful fakes of MediaStorage / MediaPool / Folder / MediaPoolItem for the media domain.

``noop`` (a set of method names) makes a method return True without any effect, which
reproduces Resolve's habit of accepting a call and not applying it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from tests.fakes.fake_resolve import (
    FakeFolder,
    FakeMediaPool,
    FakeMediaPoolItem,
    FakeMediaStorage,
    FakeResolve,
    make_resolve,
    recorded,
)

PERSISTED = {"Reel Name": False}


class MItem(FakeMediaPoolItem):
    live_class = "MediaPoolItem"

    def __init__(self, root: FakeResolve, name: str, path: str = "") -> None:
        super().__init__(root, name)
        self._props["File Path"] = path
        self._props["Type"] = "Video"
        self._props["Usage"] = "0"
        self._props["Proxy Media Path"] = ""
        self.noop: set[str] = set()
        self.color = ""
        self.flags: list[str] = []
        self.markers: dict[int, dict[str, Any]] = {}
        self.metadata: dict[str, str] = {}
        self.third: dict[str, str] = {}
        self.inout: dict[str, Any] = {}
        self.audio_mapping = '{"embedded_audio_channels": 2}'

    @recorded
    def SetName(self, name: str) -> bool:
        if "SetName" not in self.noop:
            self._name = name
        return True

    @recorded
    def SetClipProperty(self, key: str, value: str) -> bool:
        if key not in self._props:
            return False
        if key not in self.noop:
            self._props[key] = value
        return True

    @recorded
    def GetMetadata(self, key: str | None = None) -> Any:
        return dict(self.metadata) if key is None else self.metadata.get(key, "")

    @recorded
    def SetMetadata(self, data: dict[str, str]) -> bool:
        if "SetMetadata" not in self.noop:
            self.metadata.update(data)
        return True

    @recorded
    def GetThirdPartyMetadata(self, key: str | None = None) -> Any:
        return dict(self.third) if key is None else self.third.get(key, "")

    @recorded
    def SetThirdPartyMetadata(self, data: dict[str, str]) -> bool:
        self.third.update(data)
        return True

    @recorded
    def GetMediaId(self) -> str:
        return "media-" + self._uid

    @recorded
    def GetClipColor(self) -> str:
        return self.color

    @recorded
    def SetClipColor(self, color: str) -> bool:
        if "SetClipColor" not in self.noop:
            self.color = color
        return True

    @recorded
    def ClearClipColor(self) -> bool:
        self.color = ""
        return True

    @recorded
    def AddFlag(self, color: str) -> bool:
        self.flags.append(color)
        return True

    @recorded
    def GetFlagList(self) -> list[str]:
        return list(self.flags)

    @recorded
    def ClearFlags(self, color: str) -> bool:
        self.flags = [] if color == "All" else [f for f in self.flags if f != color]
        return True

    @recorded
    def AddMarker(  # noqa: PLR0917
        self, frame: int, color: str, name: str, note: str, duration: int, data: str = ""
    ) -> bool:
        if frame in self.markers:
            return False
        self.markers[frame] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": data,
        }
        return True

    @recorded
    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.markers)

    @recorded
    def GetMarkerCustomData(self, frame: int) -> str:
        return self.markers.get(frame, {}).get("customData", "")

    @recorded
    def UpdateMarkerCustomData(self, frame: int, data: str) -> bool:
        if frame not in self.markers:
            return False
        if "UpdateMarkerCustomData" not in self.noop:
            self.markers[frame]["customData"] = data
        return True

    @recorded
    def DeleteMarkerAtFrame(self, frame: int) -> bool:
        if "DeleteMarkerAtFrame" in self.noop:
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
        for frame, marker in sorted(self.markers.items()):
            if marker["customData"] == data:
                del self.markers[frame]
                return True
        return False

    @recorded
    def GetMarkInOut(self) -> dict[str, Any]:
        return dict(self.inout)

    @recorded
    def SetMarkInOut(self, mark_in: int, mark_out: int, kind: str = "all") -> bool:
        self.inout = {"video": {"in": mark_in, "out": mark_out}}
        return True

    @recorded
    def ClearMarkInOut(self, kind: str = "all") -> bool:
        self.inout = {}
        return True

    @recorded
    def GetAudioMapping(self) -> str:
        return self.audio_mapping

    @recorded
    def LinkProxyMedia(self, path: str) -> bool:
        if "LinkProxyMedia" not in self.noop:
            self._props["Proxy Media Path"] = path
        return True

    @recorded
    def UnlinkProxyMedia(self) -> bool:
        if "UnlinkProxyMedia" not in self.noop:
            self._props["Proxy Media Path"] = ""
        return True

    @recorded
    def LinkFullResolutionMedia(self, path: str) -> bool:
        return True

    @recorded
    def ReplaceClip(self, path: str) -> bool:
        if "ReplaceClip" not in self.noop:
            self._props["File Path"] = path
        return True

    @recorded
    def ReplaceClipPreserveSubClip(self, path: str) -> bool:
        self._props["File Path"] = path
        return True

    @recorded
    def MonitorGrowingFile(self) -> bool:
        return True

    @recorded
    def GetTimeline(self) -> None:
        return None


class MFolder(FakeFolder):
    live_class = "Folder"

    @recorded
    def Export(self, path: str) -> bool:
        Path(path).write_bytes(b"drb")
        return True


class MPool(FakeMediaPool):
    live_class = "MediaPool"

    def __init__(self, root: FakeResolve) -> None:
        super().__init__(root)
        self.root_folder = MFolder(root)
        self._current = self.root_folder
        self.selected: list[MItem] = []
        self.noop: set[str] = set()
        self.mattes: dict[str, list[str]] = {}

    def folders(self) -> list[MFolder]:
        out: list[MFolder] = []
        stack = [self.root_folder]
        while stack:
            f = stack.pop()
            out.append(f)
            stack.extend(f.subfolders)  # type: ignore[arg-type]
        return out

    def parent_of_clip(self, clip: MItem) -> MFolder | None:
        return next((f for f in self.folders() if clip in f.clips), None)

    @recorded
    def AddSubFolder(self, parent: MFolder, name: str) -> MFolder:
        folder = MFolder(self._root, name)
        parent.subfolders.append(folder)
        return folder

    @recorded
    def ImportMedia(self, infos: list[dict[str, Any] | str]) -> list[MItem] | None:
        # Mirrors Resolve 21.1.1: plain dicts without a sequence index return None.
        if any(isinstance(i, dict) and "StartIndex" not in i for i in infos):
            return None
        out: list[MItem] = []
        for info in infos:
            path = info if isinstance(info, str) else info["FilePath"]
            if Path(path).exists() or (isinstance(info, dict) and "StartIndex" in info):
                clip = MItem(self._root, Path(path).name, path)
                self._current.clips.append(clip)
                out.append(clip)
        return out

    @recorded
    def MoveClips(self, clips: list[MItem], target: MFolder) -> bool:
        if "MoveClips" in self.noop:
            return True
        for clip in clips:
            parent = self.parent_of_clip(clip)
            if parent is not None:
                parent.clips.remove(clip)
            target.clips.append(clip)
        return True

    @recorded
    def MoveFolders(self, folders: list[MFolder], target: MFolder) -> bool:
        for folder in folders:
            for f in self.folders():
                if folder in f.subfolders:
                    f.subfolders.remove(folder)  # type: ignore[arg-type]
            target.subfolders.append(folder)
        return True

    @recorded
    def DeleteClips(self, clips: list[MItem]) -> bool:
        if "DeleteClips" in self.noop:
            return True
        for clip in clips:
            parent = self.parent_of_clip(clip)
            if parent is not None:
                parent.clips.remove(clip)
        return True

    @recorded
    def DeleteFolders(self, folders: list[MFolder]) -> bool:
        for folder in folders:
            for f in self.folders():
                if folder in f.subfolders:
                    f.subfolders.remove(folder)  # type: ignore[arg-type]
        return True

    @recorded
    def RelinkClips(self, clips: list[MItem], folder: str) -> bool:
        for clip in clips:
            clip._props["File Path"] = str(Path(folder) / Path(clip._props["File Path"]).name)
        return True

    @recorded
    def UnlinkClips(self, clips: list[MItem]) -> bool:
        return True

    @recorded
    def GetSelectedClips(self) -> list[MItem]:
        return list(self.selected)

    @recorded
    def SetSelectedClip(self, clip: MItem) -> bool:
        self.selected = [clip]
        return True

    @recorded
    def CreateMulticamClip(self, clips: list[MItem], options: dict[str, Any]) -> list[MItem]:
        self.last_options = options
        mc = MItem(self._root, options.get("name", "Multicam"))
        self._current.clips.append(mc)
        return [mc]

    @recorded
    def CreateStereoClip(self, left: MItem, right: MItem) -> MItem:
        clip = MItem(self._root, "Stereo")
        self._current.clips.append(clip)
        return clip

    @recorded
    def AutoSyncAudio(self, clips: list[MItem], settings: dict[str, Any]) -> bool:
        self.last_sync = settings
        clips[0]._props["Synced Audio"] = "synced.wav"
        return True

    @recorded
    def ExportMetadata(self, path: str, clips: list[MItem] | None = None) -> bool:
        Path(path).write_text("Clip Name\n")
        return True

    @recorded
    def GetClipMatteList(self, clip: MItem) -> list[str]:
        return list(self.mattes.get(clip._uid, []))

    @recorded
    def GetTimelineMatteList(self, folder: MFolder) -> list[MItem]:
        return []

    @recorded
    def DeleteClipMattes(self, clip: MItem, paths: list[str]) -> bool:
        self.mattes[clip._uid] = [m for m in self.mattes.get(clip._uid, []) if m not in paths]
        return True

    @recorded
    def ImportFolderFromFile(self, path: str, source: str | None = None) -> bool:
        return True

    @recorded
    def RefreshFolders(self) -> bool:
        return True


class MStorage(FakeMediaStorage):
    live_class = "MediaStorage"

    def __init__(self, root: FakeResolve, pool: MPool) -> None:
        super().__init__(root)
        self.pool = pool
        self.clone_progress = 0.0
        self.clone_started: tuple[str, Any] | None = None
        self.clone_settings: dict[str, Any] = {}

    @recorded
    def GetMountedVolumeList(self) -> list[str]:
        return ["/Volumes/A", "/Volumes/B"]

    @recorded
    def GetSubFolderList(self, path: str) -> list[str]:
        return sorted(str(p) for p in Path(path).iterdir() if p.is_dir())

    @recorded
    def GetFileList(self, path: str) -> list[str]:
        return sorted(str(p) for p in Path(path).iterdir() if p.is_file())

    @recorded
    def AddItemListToMediaPool(self, infos: list[dict[str, Any]]) -> list[MItem] | None:
        return self.pool.ImportMedia([i["media"] if isinstance(i, dict) else i for i in infos])

    @recorded
    def AddClipMattesToMediaPool(
        self, clip: MItem, paths: list[str], eye: str | None = None
    ) -> bool:
        self.pool.mattes.setdefault(clip._uid, []).extend(paths)
        return True

    @recorded
    def AddTimelineMattesToMediaPool(self, paths: list[str]) -> list[MItem] | None:
        return self.pool.ImportMedia(list(paths))

    @recorded
    def RevealInStorage(self, path: str) -> bool:
        return True

    @recorded
    def SetCloneToolSettings(self, settings: dict[str, Any]) -> bool:
        self.clone_settings = settings
        return True

    @recorded
    def StartCloneMedia(self, source: str, targets: Any) -> bool:
        self.clone_started = (source, targets)
        self.clone_progress = 100.0
        return True

    @recorded
    def StopCloneMedia(self) -> bool:
        return True

    @recorded
    def GetCloneStatus(self) -> dict[str, Any]:
        return {"JobStatus": "Complete", "CompletionPercentage": self.clone_progress}


def make_media_resolve() -> FakeResolve:
    """Fake Resolve whose media pool/storage are the stateful media fakes."""
    resolve = make_resolve()
    project = resolve.project_manager._current
    assert project is not None
    pool = MPool(resolve)
    project.media_pool = pool
    project.GetUniqueId = lambda: "project-uid"  # type: ignore[method-assign]
    resolve.media_storage = MStorage(resolve, pool)
    resolve.AUDIO_SYNC_WAVEFORM = 7
    resolve.AUDIO_SYNC_CHANNEL_MIX = 9
    resolve.MULTICAM_ANGLE_SYNC_AUDIO = 11
    resolve.CLONE_CHECKSUM_TYPE_SHA256 = 5
    return resolve


def add_clip(
    resolve: FakeResolve, name: str, folder: MFolder | None = None, path: str = ""
) -> MItem:
    project = resolve.project_manager._current
    assert project is not None
    pool = project.media_pool
    assert isinstance(pool, MPool)
    clip = MItem(resolve, name, path)
    (folder or pool.root_folder).clips.append(clip)
    return clip
