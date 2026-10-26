# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Fakes for the project and app domains: folders, settings, presets, app presets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.fakes.fake_resolve import (
    FakeProject,
    FakeProjectManager,
    FakeResolve,
    make_resolve,
    recorded,
)

_SETTINGS = {
    "timelineFrameRate": "24",
    "timelineResolutionWidth": "1920",
    "timelinePlaybackFrameRate": "24",
}


class RichProject(FakeProject):
    live_class = "Project"

    def __init__(self, root: FakeResolve, name: str) -> None:
        super().__init__(root, name, 0, 0)
        self._settings = dict(_SETTINGS)
        self.presets: list[str] = ["Preset A"]
        self.lying_keys: set[str] = set()

    @recorded
    def GetUniqueId(self) -> str:
        return f"uid-{self._name}"

    @recorded
    def SetName(self, name: str) -> bool:
        pm: RichPM = self._root.project_manager  # type: ignore[assignment]
        pm.projects[name] = pm.projects.pop(self._name)
        self._name = name
        return True

    @recorded
    def SetSettings(self, settings: dict[str, Any]) -> bool:
        ok = True
        for key, value in settings.items():
            if key in self.lying_keys:
                continue
            if key not in self._settings or key == "timelinePlaybackFrameRate":
                ok = False
                continue
            self._settings[key] = str(value)
        return ok

    @recorded
    def GetProjectSettingsPresetList(self) -> list[dict[str, Any]]:
        return [{"Name": n, "Width": 1920, "Height": 1080} for n in self.presets]

    @recorded
    def SaveCurrentProjectSettingsAsNewPreset(self, name: str) -> bool:
        self.presets.append(name)
        return True

    @recorded
    def UpdateProjectSettingsPreset(self, name: str) -> bool:
        return name in self.presets

    @recorded
    def SetProjectSettingsPreset(self, name: str) -> bool:
        return name in self.presets

    @recorded
    def DeleteProjectSettingsPreset(self, name: str) -> bool:
        if name not in self.presets:
            return False
        self.presets.remove(name)
        return True

    @recorded
    def ExportProjectSettingsPreset(self, name: str, path: str) -> bool:
        Path(path).write_text(name)
        return name in self.presets

    @recorded
    def ImportProjectSettingsPreset(self, path: str, name: str | None = None) -> bool:
        self.presets.append(name or Path(path).stem)
        return True


class RichPM(FakeProjectManager):
    live_class = "ProjectManager"

    def __init__(self, root: FakeResolve) -> None:
        super().__init__(root)
        self.folders: list[str] = ["Clients"]
        self.folder_path = "Root"
        self.modal = False
        self.dbs = [
            {"DbType": "Disk", "DbName": "Local Database"},
            {"DbType": "Disk", "DbName": "B"},
        ]
        self.db = self.dbs[0]

    def add(self, name: str) -> RichProject:
        project = RichProject(self._root, name)
        self.projects[name] = project
        return project

    @recorded
    def GetCurrentDatabase(self) -> dict[str, str] | None:
        return None if self.stuck else self.db

    @recorded
    def GetDatabaseList(self) -> list[dict[str, str]]:
        return list(self.dbs)

    @recorded
    def SetCurrentDatabase(self, info: dict[str, str]) -> bool:
        match = [d for d in self.dbs if d["DbName"] == info.get("DbName")]
        if not match:
            return False
        self.db = match[0]
        return True

    @recorded
    def CreateProject(self, name: str, media: str | None = None) -> RichProject | None:
        if self.modal or name in self.projects:
            return None
        project = self.add(name)
        self._current = project
        return project

    @recorded
    def SaveProject(self) -> bool:
        return self._current is not None

    @recorded
    def DeleteProject(self, name: str) -> bool:
        if name not in self.projects or self.projects[name] is self._current:
            return False
        del self.projects[name]
        return True

    @recorded
    def GetFolderListInCurrentFolder(self) -> list[str]:
        return list(self.folders)

    @recorded
    def CreateFolder(self, name: str) -> bool:
        self.folders.append(name)
        return True

    @recorded
    def OpenFolder(self, name: str) -> bool:
        self.folder_path += f"/{name}"
        return name in self.folders

    @recorded
    def GotoRootFolder(self) -> bool:
        self.folder_path = "Root"
        return True

    @recorded
    def GotoParentFolder(self) -> bool:
        self.folder_path = self.folder_path.rsplit("/", 1)[0]
        return True

    @recorded
    def GetCurrentFolder(self) -> str:
        return self.folder_path

    @recorded
    def DeleteFolder(self, name: str) -> bool:
        if name not in self.folders:
            return False
        self.folders.remove(name)
        return True

    @recorded
    def GetProjectLastModifiedTime(self, name: str) -> int:
        return 1_700_000_000

    @recorded
    def GetProjectAttributesInCurrentFolder(self) -> dict[str, dict[str, str]]:
        return {n: {"lastModifiedDate": "2026-01-01T00:00:00+00:00"} for n in self.projects}

    @recorded
    def ImportProject(self, path: str, name: str | None = None) -> bool:
        self.add(name or Path(path).stem)
        return True

    @recorded
    def RestoreProject(self, path: str, name: str | None = None) -> bool:
        self.add(name or Path(path).stem)
        return True

    @recorded
    def ArchiveProject(self, name: str, path: str, *flags: bool) -> bool:
        Path(path).write_text("a")
        return True


class AppResolve(FakeResolve):
    """Fake Resolve with app-level presets, keyframe mode and session toggles."""

    live_class = "Resolve"

    def __init__(self) -> None:
        super().__init__()
        self.KEYFRAME_MODE_ALL = 0
        self.KEYFRAME_MODE_COLOR = 1
        self.KEYFRAME_MODE_SIZING = 2
        self.keyframe = 0
        self.presets: dict[str, list[str]] = {
            "layout": ["Default"],
            "keyboard": ["Resolve"],
            "user_preferences": ["Std"],
            "burn_in": ["Timecode"],
        }
        self.current_keyboard = "Resolve"
        self.high_priority: bool | None = None
        self.quit_called = False
        self.bg_disabled = False

    @recorded
    def GetKeyframeMode(self) -> int:
        return self.keyframe

    @recorded
    def SetKeyframeMode(self, mode: int) -> bool:
        self.keyframe = mode
        return True

    @recorded
    def SetHighPriority(self, flag: bool = False) -> bool:
        self.high_priority = flag
        return isinstance(flag, bool)

    @recorded
    def Quit(self) -> bool:
        self.quit_called = True
        return True

    @recorded
    def DisableBackgroundTasksForCurrentResolveSession(self) -> None:
        self.bg_disabled = True

    @recorded
    def GetCurrentKeyboardPreset(self) -> str:
        return self.current_keyboard

    def _list(self, kind: str) -> list[str]:
        return list(self.presets[kind])

    def _save(self, kind: str, name: str) -> bool:
        self.presets[kind].append(name)
        return True

    def _delete(self, kind: str, name: str) -> bool:
        if name not in self.presets[kind]:
            return False
        self.presets[kind].remove(name)
        return True

    def _import(self, kind: str, path: str, name: str | None = None) -> bool:
        self.presets[kind].append(name or Path(path).stem)
        return True

    def _export(self, kind: str, name: str, path: str) -> bool:
        Path(path).write_text(name)
        return True

    @recorded
    def GetLayoutPresetList(self) -> list[str]:
        return self._list("layout")

    @recorded
    def SaveLayoutPreset(self, name: str) -> bool:
        return self._save("layout", name)

    @recorded
    def UpdateLayoutPreset(self, name: str) -> bool:
        return name in self.presets["layout"]

    @recorded
    def LoadLayoutPreset(self, name: str) -> bool:
        return name in self.presets["layout"]

    @recorded
    def DeleteLayoutPreset(self, name: str) -> bool:
        return self._delete("layout", name)

    @recorded
    def ImportLayoutPreset(self, path: str, name: str | None = None) -> bool:
        return self._import("layout", path, name)

    @recorded
    def ExportLayoutPreset(self, name: str, path: str) -> bool:
        return self._export("layout", name, path)

    @recorded
    def GetKeyboardPresetList(self) -> list[str]:
        return self._list("keyboard")

    @recorded
    def LoadKeyboardPreset(self, name: str) -> bool:
        self.current_keyboard = name
        return True

    @recorded
    def DeleteKeyboardPreset(self, name: str) -> bool:
        return self._delete("keyboard", name)

    @recorded
    def ImportKeyboardPreset(self, path: str, name: str | None = None) -> bool:
        return self._import("keyboard", path, name)

    @recorded
    def ExportKeyboardPreset(self, name: str, path: str) -> bool:
        return self._export("keyboard", name, path)

    @recorded
    def GetUserPreferencesPresetList(self) -> list[str]:
        return self._list("user_preferences")

    @recorded
    def SaveUserPreferencesPreset(self, name: str) -> bool:
        return self._save("user_preferences", name)

    @recorded
    def GetBurnInPresetList(self) -> list[str]:
        return self._list("burn_in")

    @recorded
    def ImportBurnInPreset(self, path: str) -> bool:
        return self._import("burn_in", path)

    @recorded
    def DeleteBurnInPreset(self, name: str) -> bool:
        return self._delete("burn_in", name)

    @recorded
    def GetFairlightPresets(self) -> list[str]:
        return ["Fairlight A"]


def make_project_resolve(current: str | None = "Shot") -> AppResolve:
    """Fake Resolve whose project manager is a ``RichPM`` (optionally with a current project)."""
    resolve = AppResolve()
    pm = RichPM(resolve)
    resolve.project_manager = pm
    pm.add("Other")
    if current:
        pm._current = pm.add(current)
    return resolve


__all__ = ["AppResolve", "RichPM", "RichProject", "make_project_resolve", "make_resolve"]
