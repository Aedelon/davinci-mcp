# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Render domain: formats/codecs, presets, render queue, rendering and Quick Export.

API traps encoded here (see research/audit-existant.md section 6.2 H):

- ``GetRenderCodecs`` returns ``{description: id}``; the accepted value is the id.
- ``SetRenderSettings`` applies on top of the loaded preset (optional ``base_preset``).
- An empty/None ``CustomName`` makes Resolve reject the whole payload (dropped).
- ``MarkIn``/``MarkOut`` are absolute frames: clients give timeline-relative offsets.
- ``JobStatus`` is localized: completion is derived from ``CompletionPercentage``/``Error``.
- ``GetCurrentRenderMode`` and ``AddRenderJob`` switch to the Deliver page: page restored.
- Starting a render while another is running is refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import JobRequest, Kind, PlanDraft, action
from davinci_mcp.api._generated.resolve_21_1 import RenderSettings
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import proxies
from davinci_mcp.safety.pages import restore_page_after
from davinci_mcp.safety.paths import SAFE_NAME_PATTERN, check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "render"
_SINCE_21_1 = (21, 1, 0)
_COMPLETE = 100
_MAX_IDLE_POLLS = 3
_MODES: dict[str, int] = {"individual": 0, "single": 1}


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- models ---------------------------------------------------------------------


class RenderCodec(BaseModel):
    id: str = Field(description="Codec id accepted by Resolve.")
    description: str


class RenderFormat(BaseModel):
    format: str
    extension: str
    codecs: list[RenderCodec]


class RenderFormatList(BaseModel):
    audio: bool
    formats: list[RenderFormat]


class PresetList(BaseModel):
    render_presets: list[str]
    quick_export_presets: list[str]


class RenderConfig(BaseModel):
    format: str | None
    codec: str | None
    render_mode: Literal["single", "individual"] | None
    rendering_in_progress: bool
    settings_readable: bool = Field(
        default=False,
        description="False: no settings getter; queue a job and read it with list_render_jobs.",
    )


class RenderJob(BaseModel):
    job_id: str
    state: Literal["queued", "in_progress", "complete", "failed"]
    completion: float | None
    error: str | None = None
    job_status: str | None = Field(
        default=None, description="Localized Resolve text, display only."
    )
    info: dict[str, Any]


class RenderJobList(BaseModel):
    jobs: list[RenderJob]
    rendering_in_progress: bool


class ResolutionList(BaseModel):
    resolutions: list[dict[str, Any]]


class PresetName(BaseModel):
    name: str


class PresetImported(BaseModel):
    path: str
    added: list[str]


class PresetExported(BaseModel):
    name: str
    path: str


class QuickExportToggled(BaseModel):
    name: str
    enabled: bool


class FormatSet(BaseModel):
    format: str
    extension: str
    codec: str


class SettingsApplied(BaseModel):
    applied: dict[str, Any]
    base_preset: str | None = None


class ModeSet(BaseModel):
    render_mode: Literal["single", "individual"]


class JobAdded(BaseModel):
    job_id: str
    info: dict[str, Any]


class Stopped(BaseModel):
    stopped: bool


class JobsDeleted(BaseModel):
    deleted: list[str]


class AllJobsDeleted(BaseModel):
    deleted: int


# -- helpers --------------------------------------------------------------------


def _fail(message: str, hint: str | None = None, **details: object) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, message, hint=hint, retryable=False, details=details or None
    )


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _formats(s: ResolveSession, *, audio: bool) -> list[RenderFormat]:
    project = s.project
    list_formats, list_codecs = (
        ("GetAudioRenderFormats", "GetAudioRenderCodecs")
        if audio
        else ("GetRenderFormats", "GetRenderCodecs")
    )
    s.caps.require(project, list_formats, since=_SINCE_21_1 if audio else None)
    raw: dict[Any, Any] = getattr(project, list_formats)() or {}
    out: list[RenderFormat] = []
    for name, ext in raw.items():
        codecs: dict[Any, Any] = getattr(project, list_codecs)(str(ext)) or {}
        out.append(
            RenderFormat(
                format=str(name),
                extension=str(ext),
                codecs=[
                    RenderCodec(id=str(cid), description=str(desc)) for desc, cid in codecs.items()
                ],
            )
        )
    return out


def _find_format(formats: list[RenderFormat], value: str) -> RenderFormat:
    wanted = value.strip().lower()
    for fmt in formats:
        if wanted in (fmt.extension.lower(), fmt.format.lower()):
            return fmt
    raise DavinciError(
        ErrorCode.NOT_FOUND,
        f"Unknown render format {value!r}.",
        hint="Use render_read list_render_formats; accepts a format name or its extension.",
        retryable=False,
        details={"available": [f.format for f in formats]},
    )


def _norm(text: str) -> str:
    """'Apple ProRes 422 HQ' == 'ProRes422HQ' == 'prores 422 hq'; 'H.264' == 'H264'."""
    lowered = text.lower().removeprefix("apple ")
    return re.sub(r"[^a-z0-9]", "", lowered)


def _find_codec(fmt: RenderFormat, value: str) -> RenderCodec:
    wanted = value.strip().lower()
    for codec in fmt.codecs:
        if wanted in (codec.id.lower(), codec.description.lower()):
            return codec
    # Agents write codec names the way humans do; match ignoring case, spaces and dots,
    # and accept a unique partial match (exact normalized match wins first).
    key = _norm(value)
    exact = [c for c in fmt.codecs if key in (_norm(c.id), _norm(c.description))]
    if len(exact) == 1:
        return exact[0]
    partial = [c for c in fmt.codecs if key and (key in _norm(c.id) or key in _norm(c.description))]
    if len(partial) == 1:
        return partial[0]
    raise DavinciError(
        ErrorCode.NOT_FOUND,
        f"Format {fmt.format!r} has no codec {value!r}.",
        hint="Accepts a codec id or its description (case, spaces and dots are ignored).",
        retryable=False,
        details={"available": [c.id for c in fmt.codecs]},
    )


def _preset_names(project: ResolveObject) -> list[str]:
    raw: list[Any] = project.GetRenderPresetList() or []
    return [str(n) for n in raw]


def _require_preset(project: ResolveObject, name: str) -> None:
    names = _preset_names(project)
    if name not in names:
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"Render preset {name!r} does not exist.",
            retryable=False,
            details={"available": names},
        )


def _quick_presets(s: ResolveSession) -> list[str]:
    project = s.project
    if not s.caps.has(project, "GetQuickExportRenderPresets"):
        return []
    raw: list[Any] = project.GetQuickExportRenderPresets() or []
    return [str(n) for n in raw]


def _job_list(project: ResolveObject) -> list[dict[str, Any]]:
    raw: list[Any] = project.GetRenderJobList() or []
    return [dict(j) for j in raw]


def _job_ids(project: ResolveObject) -> list[str]:
    return [str(j.get("JobId")) for j in _job_list(project) if j.get("JobId")]


def _pct(status: Mapping[str, Any]) -> float | None:
    raw = status.get("CompletionPercentage")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _error(status: Mapping[str, Any]) -> str | None:
    err = status.get("Error")
    return err.strip() if isinstance(err, str) and err.strip() else None


def _state(status: Mapping[str, Any]) -> Literal["queued", "in_progress", "complete", "failed"]:
    pct = _pct(status) or 0.0
    if _error(status):
        return "failed"
    if pct >= _COMPLETE:
        return "complete"
    return "in_progress" if pct > 0 else "queued"


def _row(project: ResolveObject, info: dict[str, Any]) -> RenderJob:
    job_id = str(info.get("JobId", ""))
    status: dict[str, Any] = dict(project.GetRenderJobStatus(job_id) or {})
    raw_status = status.get("JobStatus")
    return RenderJob(
        job_id=job_id,
        state=_state(status),
        completion=_pct(status),
        error=_error(status),
        job_status=str(raw_status) if raw_status is not None else None,
        info=info,
    )


def _in_progress(project: ResolveObject) -> bool:
    return bool(project.IsRenderingInProgress())


def _refuse_if_rendering(project: ResolveObject) -> None:
    if _in_progress(project):
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "A render is already in progress.",
            hint="Wait for it to finish or call render_write stop_rendering.",
            retryable=False,
        )


def _check_output_name(name: str) -> str:
    """CustomName becomes a file name inside TargetDir: no separators, no leading dot."""
    if not re.fullmatch(SAFE_NAME_PATTERN, name):
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            f"Invalid output name {name!r}: use a plain file name without path separators.",
            retryable=False,
        )
    return name


def _payload(s: ResolveSession, settings: RenderSettings) -> dict[str, Any]:
    """Build the SetRenderSettings payload (empty CustomName dropped, absolute marks)."""
    data: dict[str, Any] = settings.model_dump(exclude_none=True)
    if "CustomName" in data and not str(data["CustomName"]).strip():
        del data["CustomName"]
    if "CustomName" in data:
        _check_output_name(str(data["CustomName"]))
    if "TargetDir" in data:
        data["TargetDir"] = str(
            check_path(str(data["TargetDir"]), runtime().allowed_roots, must_exist=True)
        )
    if "MarkIn" in data or "MarkOut" in data:
        rel_in, rel_out = data.get("MarkIn"), data.get("MarkOut")
        if rel_in is not None and rel_out is not None and rel_out < rel_in:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS, "MarkOut must be >= MarkIn.", retryable=False
            )
        start = int(s.timeline.GetStartFrame())
        for key in ("MarkIn", "MarkOut"):
            if key in data:
                data[key] = start + int(data[key])
        data.setdefault("SelectAllFrames", False)
    if not data:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "No render setting to apply.", retryable=False)
    return data


def _apply_settings(s: ResolveSession, payload: dict[str, Any], base_preset: str | None) -> None:
    project = s.project
    if base_preset is not None:
        _require_preset(project, base_preset)
        if not project.LoadRenderPreset(base_preset):
            raise _fail(f"LoadRenderPreset({base_preset!r}) returned False.")
    if not project.SetRenderSettings(payload):
        rejected = _rejected_keys(project, payload)
        raise _fail(
            "Project.SetRenderSettings returned False"
            + (f": Resolve rejects {', '.join(rejected)}." if rejected else "."),
            hint="Resolve rejects the whole payload if one value is invalid; fix or drop "
            "the rejected keys (render_read list_formats / list_codecs give valid values).",
            payload=payload,
            rejected=rejected,
        )


# Keys Resolve only accepts together: probing them one at a time would blame them wrongly.
_RANGE_KEYS = ("SelectAllFrames", "MarkIn", "MarkOut")


def _rejected_keys(project: ResolveObject, payload: dict[str, Any]) -> list[str]:
    """Probe the payload piece by piece to name the values Resolve refuses."""
    groups: list[dict[str, Any]] = []
    marks = {k: payload[k] for k in _RANGE_KEYS if k in payload}
    if marks:
        groups.append(marks)
    groups += [{k: v} for k, v in payload.items() if k not in _RANGE_KEYS]
    rejected: list[str] = []
    for group in groups:
        if not project.SetRenderSettings(group):
            rejected.extend(f"{k}={v!r}" for k, v in group.items())
    return rejected


def _add_job(s: ResolveSession) -> str:
    project = s.project
    with restore_page_after(s.resolve):
        job_id = project.AddRenderJob()
        if not job_id:
            raise _fail(
                "Project.AddRenderJob returned no job id.",
                hint="Check that a timeline is open and the render settings are valid.",
            )
        known = _job_ids(project)
    if str(job_id) not in known:
        raise _fail(f"Render job {job_id!r} is not in the queue after AddRenderJob.")
    return str(job_id)


def _job_outputs(project: ResolveObject, job_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(job_ids)
    outputs: list[dict[str, Any]] = []
    for job in proxies(project.GetRenderJobList()):
        info: dict[str, Any] = dict(job)
        if str(info.get("JobId")) not in wanted:
            continue
        path = Path(str(info.get("TargetDir", ""))) / str(info.get("OutputFilename", ""))
        # Individual-clip renders write several files; then only the folder is checked.
        exists = path.is_file() or (
            info.get("RenderMode") != "Single clip" and path.parent.is_dir()
        )
        outputs.append({"job_id": info.get("JobId"), "path": str(path), "exists": exists})
    return outputs


def _start_poll(job_ids: list[str]) -> Callable[[ResolveSession], dict[str, Any]]:
    """Aggregate the status of several jobs; never compares the localized JobStatus."""
    idle = [0]

    def poll(s: ResolveSession) -> dict[str, Any]:
        project = s.project
        statuses: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for job_id in job_ids:
            status: dict[str, Any] = dict(project.GetRenderJobStatus(job_id) or {})
            statuses[job_id] = status
            if not status:
                errors.append(f"Render job {job_id} no longer exists.")
            elif (err := _error(status)) is not None:
                errors.append(f"{job_id}: {err}")
        pcts = [_pct(st) or 0.0 for st in statuses.values()]
        if not pcts:
            return {"CompletionPercentage": 0.0}
        all_done = all(p >= _COMPLETE for p in pcts)
        out: dict[str, Any] = {
            "CompletionPercentage": sum(pcts) / len(pcts) if pcts else 0.0,
            "jobs": statuses,
        }
        if errors:
            out["Error"] = "; ".join(errors)
        elif all_done:
            # Report the files really produced: the preset decides the container
            # (e.g. "H.264 Master" writes .mov), so the requested name alone can mislead.
            outputs = _job_outputs(project, job_ids)
            out["outputs"] = outputs
            missing = [o["path"] for o in outputs if not o["exists"]]
            if missing:
                out["Error"] = f"Render finished but no file at: {', '.join(missing)}"
            else:
                out["done"] = True
        else:
            idle[0] = 0 if _in_progress(project) else idle[0] + 1
            if idle[0] >= _MAX_IDLE_POLLS:
                out["Error"] = "Rendering stopped before every job completed."
        return out

    return poll


def _stop(s: ResolveSession) -> object:
    return s.project.StopRendering()


def _launch_start(job_ids: list[str], interactive: bool) -> Callable[[ResolveSession], bool]:
    def launch(s: ResolveSession) -> bool:
        project = s.project
        with restore_page_after(s.resolve):
            started = bool(project.StartRendering(job_ids, interactive))
            return started or _in_progress(project)  # return value depends on state in 21.1

    return launch


# -- read actions ---------------------------------------------------------------


class ListFormatsParams(_Params):
    audio: bool = Field(default=False, description="List audio-only formats (Resolve 21.1+).")
    format: str | None = Field(default=None, description="Only this format (name or extension).")


@action(domain=DOMAIN, kind=Kind.READ, params=ListFormatsParams, result=RenderFormatList)
def list_render_formats(s: ResolveSession, p: ListFormatsParams) -> RenderFormatList:
    """Render formats; their codecs only with `format` (natural names work everywhere)."""
    formats = _formats(s, audio=p.audio)
    if p.format is not None:
        return RenderFormatList(audio=p.audio, formats=[_find_format(formats, p.format)])
    # Every codec of every format is ~15k chars: list names, codecs on request.
    return RenderFormatList(
        audio=p.audio, formats=[f.model_copy(update={"codecs": []}) for f in formats]
    )


@action(domain=DOMAIN, kind=Kind.READ, result=PresetList)
def list_render_presets(s: ResolveSession, _: BaseModel) -> PresetList:
    """List render presets and Quick Export presets."""
    return PresetList(
        render_presets=_preset_names(s.project), quick_export_presets=_quick_presets(s)
    )


@action(domain=DOMAIN, kind=Kind.READ, result=RenderConfig)
def get_render_config(s: ResolveSession, _: BaseModel) -> RenderConfig:
    """Current render format, codec, mode and whether a render is running."""
    project = s.project
    current: dict[str, Any] = dict(project.GetCurrentRenderFormatAndCodec() or {})
    with restore_page_after(s.resolve):
        mode = project.GetCurrentRenderMode()
    names = {v: k for k, v in _MODES.items()}
    fmt, codec = current.get("format"), current.get("codec")
    return RenderConfig(
        format=str(fmt) if fmt is not None else None,
        codec=str(codec) if codec is not None else None,
        render_mode=names.get(mode) if isinstance(mode, int) else None,  # pyright: ignore[reportArgumentType]
        rendering_in_progress=_in_progress(project),
    )


@action(domain=DOMAIN, kind=Kind.READ, result=RenderJobList)
def list_render_jobs(s: ResolveSession, _: BaseModel) -> RenderJobList:
    """List queued render jobs with progress (derived from percentage and error)."""
    project = s.project
    return RenderJobList(
        jobs=[_row(project, info) for info in _job_list(project)],
        rendering_in_progress=_in_progress(project),
    )


class ResolutionsParams(_Params):
    format: str | None = Field(default=None, description="Format name or extension.")
    codec: str | None = Field(default=None, description="Codec id or description (needs format).")


@action(domain=DOMAIN, kind=Kind.READ, params=ResolutionsParams, result=ResolutionList)
def get_render_resolutions(s: ResolveSession, p: ResolutionsParams) -> ResolutionList:
    """List the resolutions available for a render format and codec."""
    fmt_ext: str | None = None
    codec_id: str | None = None
    if p.format is not None:
        fmt = _find_format(_formats(s, audio=False), p.format)
        fmt_ext = fmt.extension
        if p.codec is not None:
            codec_id = _find_codec(fmt, p.codec).id
    elif p.codec is not None:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "codec requires format.", retryable=False)
    raw: list[Any] = s.project.GetRenderResolutions(fmt_ext, codec_id) or []
    return ResolutionList(resolutions=[dict(r) for r in raw])


# -- preset writes --------------------------------------------------------------


class PresetNameParams(_Params):
    name: str = Field(min_length=1, description="Render preset name.")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=PresetName,
    verify_reason="Resolve has no getter for the loaded preset (name checked beforehand).",
)
def load_render_preset(s: ResolveSession, p: PresetNameParams) -> PresetName:
    """Load a render preset as the current render settings."""
    project = s.project
    _require_preset(project, p.name)
    if not project.LoadRenderPreset(p.name):
        raise _fail(f"LoadRenderPreset({p.name!r}) returned False.")
    return PresetName(name=p.name)


def _verify_preset_present(s: ResolveSession, p: PresetNameParams, _: PresetName) -> Verification:
    return Verification(ok=p.name in _preset_names(s.project))


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=PresetName,
    verify=_verify_preset_present,
)
def save_render_preset(s: ResolveSession, p: PresetNameParams) -> PresetName:
    """Save the current render settings as a new preset."""
    project = s.project
    if p.name in _preset_names(project):
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"Render preset {p.name!r} already exists.",
            hint="Use update_render_preset to overwrite it.",
            retryable=False,
        )
    if not project.SaveAsNewRenderPreset(p.name):
        raise _fail(f"SaveAsNewRenderPreset({p.name!r}) returned False.")
    return PresetName(name=p.name)


def _plan_update(s: ResolveSession, p: PresetNameParams) -> PlanDraft:
    project = s.project
    _require_preset(project, p.name)
    return PlanDraft(
        summary=f"Overwrite render preset {p.name!r} with the current render settings.",
        effects=["The previous content of the preset is lost."],
        targets=[f"render_preset:{p.name}"],
        fingerprint=_fingerprint(p.name),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=PresetNameParams,
    result=PresetName,
    since=_SINCE_21_1,
    plan=_plan_update,
    verify_reason="Resolve cannot read a preset's content back.",
)
def update_render_preset(s: ResolveSession, p: PresetNameParams) -> PresetName:
    """Overwrite a render preset with the current render settings (21.1+)."""
    project = s.project
    s.caps.require(project, "UpdateRenderPreset", since=_SINCE_21_1)
    _require_preset(project, p.name)
    if not project.UpdateRenderPreset(p.name):
        raise _fail(f"UpdateRenderPreset({p.name!r}) returned False.")
    return PresetName(name=p.name)


def _plan_delete_preset(s: ResolveSession, p: PresetNameParams) -> PlanDraft:
    _require_preset(s.project, p.name)
    return PlanDraft(
        summary=f"Delete render preset {p.name!r}.",
        effects=["The preset is removed from Resolve."],
        targets=[f"render_preset:{p.name}"],
        fingerprint=_fingerprint(p.name),
    )


def _verify_preset_deleted(s: ResolveSession, p: PresetNameParams, _: PresetName) -> Verification:
    return Verification(ok=p.name not in _preset_names(s.project))


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=PresetNameParams,
    result=PresetName,
    plan=_plan_delete_preset,
    verify=_verify_preset_deleted,
)
def delete_render_preset(s: ResolveSession, p: PresetNameParams) -> PresetName:
    """Delete a render preset."""
    project = s.project
    _require_preset(project, p.name)
    if not project.DeleteRenderPreset(p.name):
        raise _fail(f"DeleteRenderPreset({p.name!r}) returned False.")
    return PresetName(name=p.name)


class ImportPresetParams(_Params):
    path: str = Field(description="Preset file to import (it is also selected).")


def _verify_imported(s: ResolveSession, _: ImportPresetParams, r: PresetImported) -> Verification:
    names = _preset_names(s.project)
    ok = bool(r.added) and all(n in names for n in r.added)
    return Verification(
        ok=ok,
        observed={"added": r.added},
        note=None if ok else "No new preset name (it may have replaced a preset of the same name).",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportPresetParams,
    result=PresetImported,
    verify=_verify_imported,
)
def import_render_preset(s: ResolveSession, p: ImportPresetParams) -> PresetImported:
    """Import a render preset from a file and select it."""
    path = check_path(p.path, runtime().allowed_roots, must_exist=True)
    before = set(_preset_names(s.project))
    if not s.resolve.ImportRenderPreset(str(path)):
        raise _fail("Resolve.ImportRenderPreset returned False.", path=str(path))
    return PresetImported(
        path=str(path), added=[n for n in _preset_names(s.project) if n not in before]
    )


class ExportPresetParams(_Params):
    name: str = Field(min_length=1, description="Render preset to export.")
    path: str = Field(description="Destination file (must not exist).")


def _verify_exported(_: ResolveSession, __: ExportPresetParams, r: PresetExported) -> Verification:
    return Verification(ok=Path(r.path).is_file(), observed={"path": r.path})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportPresetParams,
    result=PresetExported,
    verify=_verify_exported,
)
def export_render_preset(s: ResolveSession, p: ExportPresetParams) -> PresetExported:
    """Export a render preset to a file."""
    path = check_path(p.path, runtime().allowed_roots)
    if path.exists():
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"{path} already exists.",
            hint="Pick a new file.",
            retryable=False,
        )
    _require_preset(s.project, p.name)
    if not s.resolve.ExportRenderPreset(p.name, str(path)):
        raise _fail("Resolve.ExportRenderPreset returned False.", path=str(path))
    return PresetExported(name=p.name, path=str(path))


class QuickExportToggleParams(_Params):
    name: str = Field(min_length=1, description="Render preset name.")
    enabled: bool = True


def _verify_quick_toggle(
    s: ResolveSession, p: QuickExportToggleParams, _: QuickExportToggled
) -> Verification:
    present = p.name in _quick_presets(s)
    return Verification(ok=present == p.enabled, observed={"in_quick_export_list": present})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=QuickExportToggleParams,
    result=QuickExportToggled,
    since=_SINCE_21_1,
    idempotent=True,
    verify=_verify_quick_toggle,
)
def set_quick_export_preset(s: ResolveSession, p: QuickExportToggleParams) -> QuickExportToggled:
    """Enable or disable a render preset for Quick Export (21.1+)."""
    project = s.project
    s.caps.require(project, "SetQuickExportEnabledForRenderPreset", since=_SINCE_21_1)
    _require_preset(project, p.name)
    if not project.SetQuickExportEnabledForRenderPreset(p.name, p.enabled):
        raise _fail("SetQuickExportEnabledForRenderPreset returned False.")
    return QuickExportToggled(name=p.name, enabled=p.enabled)


# -- format / settings / mode ------------------------------------------------------


class SetFormatParams(_Params):
    format: str = Field(description="Render format name or extension, e.g. 'QuickTime' or 'mov'.")
    codec: str = Field(description="Codec id or description, e.g. 'ProRes422HQ'.")


def _verify_format(s: ResolveSession, p: SetFormatParams, r: FormatSet) -> Verification:
    current: dict[str, Any] = dict(s.project.GetCurrentRenderFormatAndCodec() or {})
    fmt, codec = str(current.get("format", "")), str(current.get("codec", ""))
    fmt_ok = fmt.lower() in (r.format.lower(), r.extension.lower())
    codec_ok = (
        codec.lower() in {r.codec.lower(), p.codec.lower()} or codec.lower() == r.codec.lower()
    )
    return Verification(ok=fmt_ok and codec_ok, observed=current)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetFormatParams,
    result=FormatSet,
    idempotent=True,
    verify=_verify_format,
)
def set_render_format(s: ResolveSession, p: SetFormatParams) -> FormatSet:
    """Set the render format and codec (validated against the live list)."""
    fmt = _find_format(_formats(s, audio=False), p.format)
    if not fmt.codecs:
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            f"Format {fmt.format!r} has no selectable codec in this Resolve version.",
            hint="Use a render preset instead (e.g. load_render_preset 'Audio Only').",
            retryable=False,
        )
    codec = _find_codec(fmt, p.codec)
    if not s.project.SetCurrentRenderFormatAndCodec(fmt.extension, codec.id):
        raise _fail(
            "SetCurrentRenderFormatAndCodec returned False.", format=fmt.extension, codec=codec.id
        )
    return FormatSet(format=fmt.format, extension=fmt.extension, codec=codec.id)


class SetSettingsParams(_Params):
    settings: RenderSettings = Field(
        description="Render settings. MarkIn/MarkOut are frame offsets from the timeline start "
        "(converted to absolute frames). TargetDir must exist inside the allowed roots."
    )
    base_preset: str | None = Field(
        default=None,
        description="Load this (video) preset first: settings apply on top of the loaded preset.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetSettingsParams,
    result=SettingsApplied,
    verify_reason="Resolve has no render settings getter; add_render_job then list_render_jobs "
    "shows what was applied.",
)
def set_render_settings(s: ResolveSession, p: SetSettingsParams) -> SettingsApplied:
    """Apply render settings (on top of the loaded preset)."""
    payload = _payload(s, p.settings)
    _apply_settings(s, payload, p.base_preset)
    return SettingsApplied(applied=payload, base_preset=p.base_preset)


class SetModeParams(_Params):
    mode: Literal["single", "individual"] = Field(description="Single clip or individual clips.")


def _verify_mode(s: ResolveSession, p: SetModeParams, _: ModeSet) -> Verification:
    with restore_page_after(s.resolve):
        observed = s.project.GetCurrentRenderMode()
    return Verification(ok=observed == _MODES[p.mode], observed={"mode": observed})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetModeParams,
    result=ModeSet,
    idempotent=True,
    verify=_verify_mode,
)
def set_render_mode(s: ResolveSession, p: SetModeParams) -> ModeSet:
    """Set the render mode (single clip or individual clips)."""
    with restore_page_after(s.resolve):
        ok = s.project.SetCurrentRenderMode(_MODES[p.mode])
    if not ok:
        raise _fail("SetCurrentRenderMode returned False.")
    return ModeSet(render_mode=p.mode)


# -- queue ------------------------------------------------------------------------


def _verify_job_added(s: ResolveSession, _: BaseModel, r: JobAdded) -> Verification:
    return Verification(ok=r.job_id in _job_ids(s.project), observed={"job_id": r.job_id})


@action(domain=DOMAIN, kind=Kind.WRITE, result=JobAdded, verify=_verify_job_added)
def add_render_job(s: ResolveSession, _: BaseModel) -> JobAdded:
    """Queue a render job from the current render settings; returns its id."""
    job_id = _add_job(s)
    empty: dict[str, Any] = {}
    info = next((j for j in _job_list(s.project) if j.get("JobId") == job_id), empty)
    return JobAdded(job_id=job_id, info=info)


class StartRenderingParams(_Params):
    job_ids: list[str] | None = Field(default=None, description="Jobs to render (default: all).")
    interactive: bool = Field(
        default=False, description="Interactive mode (shows Resolve UI errors)."
    )


def _resolve_jobs(project: ResolveObject, wanted: list[str] | None) -> list[str]:
    known = _job_ids(project)
    if wanted is None:
        if not known:
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                "The render queue is empty.",
                hint="Call render_write add_render_job first.",
                retryable=False,
            )
        return known
    missing = [j for j in wanted if j not in known]
    if missing or not wanted:
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"Unknown render job id(s): {missing or wanted}.",
            retryable=False,
            details={"available": known},
        )
    return wanted


@action(domain=DOMAIN, kind=Kind.WRITE, params=StartRenderingParams, long_running=True)
def start_rendering(s: ResolveSession, p: StartRenderingParams) -> JobRequest:
    """Start rendering queued jobs (long-running; poll with job_wait)."""
    project = s.project
    _refuse_if_rendering(project)
    ids = _resolve_jobs(project, p.job_ids)
    return JobRequest(
        kind="render",
        label=f"Render {len(ids)} job(s)",
        launch=_launch_start(ids, p.interactive),
        poll=_start_poll(ids),
        cancel=_stop,
    )


class QuickExportParams(_Params):
    preset: str = Field(description="Quick Export preset name (see list_render_presets).")
    target_dir: str | None = Field(default=None, description="Output directory (must exist).")
    name: str | None = Field(default=None, description="Output file name.")
    video_quality: int | None = Field(default=None, ge=0, description="Bit rate limit, 0 = auto.")
    enable_upload: bool = False


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=QuickExportParams,
    since=_SINCE_21_1,
    long_running=True,
)
def quick_export(s: ResolveSession, p: QuickExportParams) -> JobRequest:
    """Render the current timeline with a Quick Export preset (21.1+, long-running)."""
    project = s.project
    s.caps.require(project, "RenderWithQuickExport", since=_SINCE_21_1)
    if p.preset not in _quick_presets(s):
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"Quick Export preset {p.preset!r} not found.",
            retryable=False,
            details={"available": _quick_presets(s)},
        )
    _refuse_if_rendering(project)
    info: dict[str, Any] = {"EnableUpload": p.enable_upload}
    if p.target_dir is not None:
        info["TargetDir"] = str(check_path(p.target_dir, runtime().allowed_roots, must_exist=True))
    if p.name:
        info["CustomName"] = _check_output_name(p.name)
    if p.video_quality is not None:
        info["VideoQuality"] = p.video_quality

    def launch(sess: ResolveSession) -> bool:
        with restore_page_after(sess.resolve):
            status = sess.project.RenderWithQuickExport(p.preset, info)
        return isinstance(status, Mapping) and _error(status) is None  # pyright: ignore[reportUnknownArgumentType]

    def poll(sess: ResolveSession) -> dict[str, Any]:
        status: dict[str, Any] = dict(sess.project.GetRenderWithQuickExportStatus() or {})
        # TimeTakenToRenderInMs is only set on completion; JobStatus is not compared.
        if _error(status) is None and status.get("TimeTakenToRenderInMs") is not None:
            status["done"] = True
        return status

    return JobRequest(
        kind="quick_export",
        label=f"Quick Export ({p.preset})",
        launch=launch,
        poll=poll,
        cancel=_stop,
    )


class RenderTimelineParams(_Params):
    preset: str = Field(description="Render preset to use (a video preset).")
    target_dir: str = Field(description="Output directory (must exist, inside allowed roots).")
    name: str = Field(min_length=1, description="Output file name (without extension).")
    format: str | None = Field(
        default=None,
        description="Container override, e.g. 'mp4' (presets pick their own: H.264 Master = mov).",
    )
    codec: str | None = Field(
        default=None,
        description="Codec with format; natural names work ('ProRes 422 LT', 'H.264').",
    )
    settings: RenderSettings | None = Field(
        default=None, description="Extra settings on top of the preset (MarkIn/MarkOut relative)."
    )


@action(domain=DOMAIN, kind=Kind.WRITE, params=RenderTimelineParams, long_running=True)
def render_timeline(s: ResolveSession, p: RenderTimelineParams) -> JobRequest:
    """One-shot: load preset, set target and name, queue and start (long-running)."""
    project = s.project
    _require_preset(project, p.preset)
    _refuse_if_rendering(project)
    extra = p.settings.model_dump(exclude_none=True) if p.settings else {}
    extra.update({"TargetDir": p.target_dir, "CustomName": p.name})
    payload = _payload(s, RenderSettings.model_validate(extra))
    job_ids: list[str] = []
    inner_poll = _start_poll(job_ids)

    container: tuple[str, str] | None = None
    if p.format is not None:
        fmt = _find_format(_formats(s, audio=False), p.format)
        container = (fmt.extension, _find_codec(fmt, p.codec or fmt.codecs[0].id).id)

    def launch(sess: ResolveSession) -> bool:
        if container is None:
            _apply_settings(sess, payload, p.preset)
        else:
            # Order matters: preset, then container/codec, then settings (the format switch
            # resets per-format settings such as the custom name).
            if not sess.project.LoadRenderPreset(p.preset):
                raise _fail(f"LoadRenderPreset({p.preset!r}) returned False.")
            if not sess.project.SetCurrentRenderFormatAndCodec(*container):
                raise _fail("SetCurrentRenderFormatAndCodec returned False.", format=container[0])
            _apply_settings(sess, payload, None)
        job_id = _add_job(sess)
        job_ids.append(job_id)
        return _launch_start([job_id], False)(sess)

    def poll(sess: ResolveSession) -> dict[str, Any]:
        return inner_poll(sess)

    return JobRequest(
        kind="render",
        label=f"Render timeline '{p.name}' ({p.preset})",
        launch=launch,
        poll=poll,
        cancel=_stop,
    )


def _verify_stopped(s: ResolveSession, _: BaseModel, __: Stopped) -> Verification:
    for _attempt in range(10):
        if not _in_progress(s.project):
            return Verification(ok=True)
        time.sleep(0.2)
    return Verification(ok=False, note="IsRenderingInProgress is still True.")


@action(domain=DOMAIN, kind=Kind.WRITE, result=Stopped, idempotent=True, verify=_verify_stopped)
def stop_rendering(s: ResolveSession, _: BaseModel) -> Stopped:
    """Stop any current render."""
    s.project.StopRendering()
    return Stopped(stopped=True)


# -- destructive queue operations ----------------------------------------------------


class DeleteJobsParams(_Params):
    job_ids: list[str] = Field(min_length=1, description="Render job ids to delete.")


def _plan_delete_jobs(s: ResolveSession, p: DeleteJobsParams) -> PlanDraft:
    project = s.project
    infos = {str(j.get("JobId")): j for j in _job_list(project)}
    missing = [j for j in p.job_ids if j not in infos]
    if missing:
        raise DavinciError(
            ErrorCode.NOT_FOUND, f"Unknown render job id(s): {missing}.", retryable=False
        )
    return PlanDraft(
        summary=f"Delete {len(p.job_ids)} render job(s) from the queue.",
        effects=["The jobs are removed from the queue (rendered files are kept)."],
        targets=[f"render_job:{j}" for j in p.job_ids],
        fingerprint=_fingerprint([infos[j] for j in p.job_ids]),
    )


def _verify_jobs_deleted(s: ResolveSession, p: DeleteJobsParams, _: JobsDeleted) -> Verification:
    left = [j for j in p.job_ids if j in _job_ids(s.project)]
    return Verification(ok=not left, observed={"still_present": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteJobsParams,
    result=JobsDeleted,
    plan=_plan_delete_jobs,
    verify=_verify_jobs_deleted,
)
def delete_render_jobs(s: ResolveSession, p: DeleteJobsParams) -> JobsDeleted:
    """Delete specific render jobs from the queue."""
    project = s.project
    deleted: list[str] = []
    for job_id in p.job_ids:
        if not project.DeleteRenderJob(job_id):
            raise _fail(f"DeleteRenderJob({job_id!r}) returned False.", deleted=deleted)
        deleted.append(job_id)
    return JobsDeleted(deleted=deleted)


def _plan_delete_all(s: ResolveSession, _: BaseModel) -> PlanDraft:
    jobs = _job_list(s.project)
    return PlanDraft(
        summary=f"Delete all {len(jobs)} render job(s) from the queue.",
        effects=["The whole render queue is emptied (rendered files are kept)."],
        targets=[f"render_job:{j.get('JobId')}" for j in jobs],
        fingerprint=_fingerprint(jobs),
    )


def _verify_all_deleted(s: ResolveSession, _: BaseModel, __: AllJobsDeleted) -> Verification:
    left = _job_ids(s.project)
    return Verification(ok=not left, observed={"remaining": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    result=AllJobsDeleted,
    plan=_plan_delete_all,
    verify=_verify_all_deleted,
)
def delete_all_render_jobs(s: ResolveSession, _: BaseModel) -> AllJobsDeleted:
    """Delete every render job in the queue."""
    project = s.project
    count = len(_job_ids(project))
    if not project.DeleteAllRenderJobs():
        raise _fail("DeleteAllRenderJobs returned False.")
    return AllJobsDeleted(deleted=count)


ACTIONS = [
    list_render_formats,
    list_render_presets,
    get_render_config,
    list_render_jobs,
    get_render_resolutions,
    load_render_preset,
    save_render_preset,
    update_render_preset,
    import_render_preset,
    export_render_preset,
    set_quick_export_preset,
    set_render_format,
    set_render_settings,
    set_render_mode,
    add_render_job,
    start_rendering,
    quick_export,
    stop_rendering,
    render_timeline,
    delete_render_jobs,
    delete_all_render_jobs,
    delete_render_preset,
]
