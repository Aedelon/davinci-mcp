# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Fake render API (Project render methods, Resolve preset import/export)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.fakes.fake_resolve import DUMPS, FakeProject, FakeResolve, make_resolve, recorded


class FakeRenderProject(FakeProject):
    live_class = "Project"

    def __init__(self, root: FakeResolve, name: str) -> None:
        super().__init__(root, name, 1, 3)
        self.formats: dict[str, str] = dict(DUMPS["Project.GetRenderFormats"])
        self.codecs: dict[str, dict[str, str]] = dict(DUMPS["Project.GetRenderCodecs"])
        self.presets: list[str] = ["H.264 Master", "ProRes 422 HQ", "Audio Only"]
        self.quick: list[str] = ["H.264 Master"]
        self.fmt_codec: dict[str, str] = {"format": "mov", "codec": "ProRes422HQ"}
        self.mode = 1
        self.jobs: dict[str, dict[str, Any]] = {}
        self.pct: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.rendering = False
        self.applied: list[dict[str, Any]] = []
        self.loaded: list[str] = []
        self.ignore_mode = False
        self.reject_settings = False
        self.bad_keys: set[str] = set()  # any payload containing one is refused
        self.qe_pct = 0
        self.qe_enabled_noop = False

    @recorded
    def GetRenderFormats(self) -> dict[str, str]:
        return dict(self.formats)

    @recorded
    def GetAudioRenderFormats(self) -> dict[str, str]:
        return {"MP3": "mp3"}

    @recorded
    def GetRenderCodecs(self, ext: str) -> dict[str, str]:
        return dict(self.codecs.get(ext, {}))

    @recorded
    def GetAudioRenderCodecs(self, ext: str) -> dict[str, str]:
        return {"MP3": "mp3"} if ext == "mp3" else {}

    @recorded
    def GetRenderPresetList(self) -> list[str]:
        return list(self.presets)

    @recorded
    def GetQuickExportRenderPresets(self) -> list[str]:
        return list(self.quick)

    @recorded
    def LoadRenderPreset(self, name: str) -> bool:
        if name not in self.presets:
            return False
        self.loaded.append(name)
        return True

    @recorded
    def SaveAsNewRenderPreset(self, name: str) -> bool:
        self.presets.append(name)
        return True

    @recorded
    def UpdateRenderPreset(self, name: str) -> bool:
        return name in self.presets

    @recorded
    def DeleteRenderPreset(self, name: str) -> bool:
        if name not in self.presets:
            return False
        self.presets.remove(name)
        return True

    @recorded
    def SetQuickExportEnabledForRenderPreset(self, name: str, enabled: bool) -> bool:
        if self.qe_enabled_noop:
            return True
        if enabled and name not in self.quick:
            self.quick.append(name)
        if not enabled and name in self.quick:
            self.quick.remove(name)
        return True

    @recorded
    def GetCurrentRenderFormatAndCodec(self) -> dict[str, str]:
        return dict(self.fmt_codec)

    @recorded
    def SetCurrentRenderFormatAndCodec(self, fmt: str, codec: str) -> bool:
        self.fmt_codec = {"format": fmt, "codec": codec}
        return True

    @recorded
    def GetCurrentRenderMode(self) -> int:
        self._root.page = "deliver"
        return self.mode

    @recorded
    def SetCurrentRenderMode(self, mode: int) -> bool:
        if not self.ignore_mode:
            self.mode = mode
        return True

    @recorded
    def SetRenderSettings(self, settings: dict[str, Any]) -> bool:
        if self.reject_settings or ("CustomName" in settings and not settings["CustomName"]):
            return False
        if self.bad_keys & set(settings):
            return False
        self.applied.append(dict(settings))
        return True

    @recorded
    def GetRenderResolutions(self, fmt: str | None = None, codec: str | None = None) -> list[Any]:
        return [{"Width": 1920, "Height": 1080}]

    @recorded
    def AddRenderJob(self) -> str:
        self._root.page = "deliver"
        job_id = f"job{len(self.jobs) + 1}"
        self.jobs[job_id] = {"JobId": job_id, **(self.applied[-1] if self.applied else {})}
        self.pct[job_id] = 0
        return job_id

    @recorded
    def GetRenderJobList(self) -> list[dict[str, Any]]:
        return [dict(j) for j in self.jobs.values()]

    @recorded
    def GetRenderJobStatus(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            return {}
        if self.rendering and job_id in self.errors:
            return {"JobStatus": "Echec", "CompletionPercentage": 10, "Error": self.errors[job_id]}
        if self.rendering and self.pct[job_id] < 100:
            self.pct[job_id] = min(100, self.pct[job_id] + 50)
            if all(v >= 100 for v in self.pct.values()):
                self.rendering = False
        return {"JobStatus": "Concluso", "CompletionPercentage": self.pct[job_id]}

    @recorded
    def StartRendering(self, ids: list[str], interactive: bool = False) -> bool:
        self.rendering = True
        return True

    @recorded
    def StopRendering(self) -> None:
        self.rendering = False

    @recorded
    def IsRenderingInProgress(self) -> bool:
        return self.rendering

    @recorded
    def DeleteRenderJob(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    @recorded
    def DeleteAllRenderJobs(self) -> bool:
        self.jobs.clear()
        return True

    @recorded
    def RenderWithQuickExport(self, preset: str, info: dict[str, Any]) -> dict[str, Any]:
        self.qe_pct = 0
        self.rendering = True
        return {"JobStatus": "Rendering", "CompletionPercentage": 0}

    @recorded
    def GetRenderWithQuickExportStatus(self) -> dict[str, Any]:
        self.qe_pct = min(100, self.qe_pct + 50)
        if self.qe_pct >= 100:
            self.rendering = False
            return {
                "JobStatus": "Render Complete",
                "CompletionPercentage": 100,
                "TimeTakenToRenderInMs": 5,
            }
        return {"JobStatus": "Rendering", "CompletionPercentage": self.qe_pct}


def make_render_resolve() -> tuple[FakeResolve, FakeRenderProject]:
    resolve = make_resolve()
    manager = resolve.project_manager
    project = FakeRenderProject(resolve, "Test Project")
    manager.projects["Test Project"] = project
    manager._current = project

    @recorded
    def _import(_self: Any, path: str) -> bool:
        if not Path(path).is_file():
            return False
        project.presets.append(Path(path).stem)
        return True

    @recorded
    def _export(_self: Any, name: str, path: str) -> bool:
        if name not in project.presets:
            return False
        Path(path).write_text(name)
        return True

    resolve.ImportRenderPreset = lambda path: _import(resolve, path)  # type: ignore[attr-defined]
    resolve.ExportRenderPreset = lambda name, path: _export(resolve, name, path)  # type: ignore[attr-defined]
    return resolve, project
