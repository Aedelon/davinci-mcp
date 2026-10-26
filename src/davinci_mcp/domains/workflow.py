# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Workflow domain: one call per multi-step job, each step read back.

Agents spend 10-30 tool round-trips on routine jobs (see eval/RESULTS.md). These actions
chain the proven single-step helpers of the other domains and verify the final state.

Traps encoded here:

- ``ImportMedia`` takes plain path strings (dict entries return None on Resolve 21.1.1) and
  lands in the current folder; media already in the pool (same ``File Path``) is not re-imported.
- An EDL event links to a clip only when its reel/clip name matches AND its source-in lies in
  the clip's timecode range: offline items are fixed by setting the clip "Start TC" to the
  source-in floored to the hour (camera convention), then deleting and re-importing once.
- Render presets choose the container ("H.264 Master" writes .mov): the files really
  produced are read from ``GetRenderJobList`` (TargetDir + OutputFilename). Completion comes
  from ``CompletionPercentage``/``Error`` and never from the localized ``JobStatus``.
- Vertical timelines: ``Timeline.SetSetting`` may not stick; the fallback sets the project
  resolution BEFORE creating the timeline (the timeline then follows the project settings).
- Subtitles from audio need Studio plus the AI Extras package (an error string means missing).
- Fusion: structural edits under ``Lock``, input edits unlocked (SetInput under Lock is not
  applied at render); the Text+ size input is the exact id ``Size`` (not ``TransformSize``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import Kind, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.domains.ai import _check_ai, _status_poll  # pyright: ignore[reportPrivateUsage]
from davinci_mcp.domains.color import (
    _check_node,  # pyright: ignore[reportPrivateUsage]
    _lut_reference,  # pyright: ignore[reportPrivateUsage]
)
from davinci_mcp.domains.fusion import (
    _AUTO_POS,  # pyright: ignore[reportPrivateUsage]
    _comp_names,  # pyright: ignore[reportPrivateUsage]
    _CompParams,  # pyright: ignore[reportPrivateUsage]
    _find_input,  # pyright: ignore[reportPrivateUsage]
    _is_animated,  # pyright: ignore[reportPrivateUsage]
    _open_comp,  # pyright: ignore[reportPrivateUsage]
    _output_tool_name,  # pyright: ignore[reportPrivateUsage]
    _read_input,  # pyright: ignore[reportPrivateUsage]
    _same,  # pyright: ignore[reportPrivateUsage]
    _tool_id,  # pyright: ignore[reportPrivateUsage]
    _tool_name,  # pyright: ignore[reportPrivateUsage]
    _tools,  # pyright: ignore[reportPrivateUsage]
    _transaction,  # pyright: ignore[reportPrivateUsage]
    check_tool_name,
)
from davinci_mcp.domains.gallery import _resolve_album  # pyright: ignore[reportPrivateUsage]
from davinci_mcp.domains.interchange import frames_to_timecode, timecode_to_frames
from davinci_mcp.domains.render import (
    _add_job,  # pyright: ignore[reportPrivateUsage]
    _apply_settings,  # pyright: ignore[reportPrivateUsage]
    _error,  # pyright: ignore[reportPrivateUsage]
    _find_codec,  # pyright: ignore[reportPrivateUsage]
    _find_format,  # pyright: ignore[reportPrivateUsage]
    _formats,  # pyright: ignore[reportPrivateUsage]
    _in_progress,  # pyright: ignore[reportPrivateUsage]
    _pct,  # pyright: ignore[reportPrivateUsage]
    _refuse_if_rendering,  # pyright: ignore[reportPrivateUsage]
    _require_preset,  # pyright: ignore[reportPrivateUsage]
)
from davinci_mcp.domains.timeline import (
    _as_current,  # pyright: ignore[reportPrivateUsage]
    _imported,  # pyright: ignore[reportPrivateUsage]
    _names,  # pyright: ignore[reportPrivateUsage]
    _require_free_name,  # pyright: ignore[reportPrivateUsage]
)
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import (
    ItemRef,
    TimelineRef,
    iter_items,
    iter_timelines,
    proxies,
    resolve_item,
    resolve_timeline,
    walk_folders,
)
from davinci_mcp.safety.pages import restore_page_after
from davinci_mcp.safety.paths import SafeFileName, check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "workflow"
_COMPLETE = 100
_POLL_S = 1.0
_IDLE_POLLS = 3
_SUBTITLE_SINCE = (21, 1, 1)
_SUBTITLE_GETTER = "GetCreateSubtitlesFromAudioStatus"
_MAX_MEDIA_FILES = 2000
_MIN_EVENT_TOKENS = 8
_MIN_KEYS = 2
_ACTION_TIMEOUT_S = 3600.0
_FRESH_SLACK_S = 5.0
_TC_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[:;]\d{2}$")
_EVENT_RE = re.compile(r"^\d{3,6}\s+\S+\s+\S+\s+\S+")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _fail(message: str, hint: str | None = None, **details: object) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, message, hint=hint, retryable=False, details=details or None
    )


def _invalid(message: str, hint: str | None = None) -> DavinciError:
    return DavinciError(ErrorCode.INVALID_PARAMS, message, hint=hint, retryable=False)


# -- shared models -------------------------------------------------------------------


class Deliverable(_Params):
    """One render output. A preset picks the container; format/codec override it."""

    name: SafeFileName = Field(description="Output file name without extension.")
    preset: str | None = Field(default=None, description="Render preset to load first.")
    format: str | None = Field(default=None, description="Container override, e.g. 'mp4'.")
    codec: str | None = Field(default=None, description="Codec id or description (needs format).")
    width: int | None = Field(default=None, ge=16, le=16384)
    height: int | None = Field(default=None, ge=16, le=16384)

    @model_validator(mode="after")
    def _codec_needs_format(self) -> Deliverable:
        if self.codec is not None and self.format is None:
            raise ValueError("codec requires format")
        return self


class OutputFile(BaseModel):
    name: str
    job_id: str
    path: str
    exists: bool
    fresh: bool = Field(description="File modified during this call (not a stale leftover).")
    size_bytes: int | None = None
    codec: str | None = Field(default=None, description="ffprobe video codec, when available.")
    width: int | None = None
    height: int | None = None


# -- shared helpers: media -----------------------------------------------------------


def _norm(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _pool_clips(s: ResolveSession) -> list[ResolveObject]:
    """Every clip of the Media Pool, read live (the shared index is cached across writes)."""
    root = s.project.GetMediaPool().GetRootFolder()
    return [c for _, folder in walk_folders(root) for c in proxies(folder.GetClipList())]


def _clip_path(clip: ResolveObject) -> str:
    return str(clip.GetClipProperty("File Path") or "")


def _expand_media(paths: list[str]) -> list[Path]:
    """Validated files; folders contribute every file below them (hidden files skipped)."""
    roots = runtime().allowed_roots
    files: list[Path] = []
    for raw in paths:
        path = check_path(raw, roots, must_exist=True)
        if path.is_dir():
            for f in sorted(path.rglob("*")):
                rel = f.relative_to(path)
                if f.is_symlink() or not f.is_file() or any(p.startswith(".") for p in rel.parts):
                    continue  # symlinks could point outside the sandbox: never followed
                files.append(check_path(f, roots, must_exist=True))
        else:
            files.append(path)
        if len(files) > _MAX_MEDIA_FILES:
            raise _invalid(f"More than {_MAX_MEDIA_FILES} media files.")
    seen: set[Path] = set()
    unique = [f for f in files if not (f in seen or seen.add(f))]
    return unique


def _import_missing(
    s: ResolveSession, files: list[Path]
) -> tuple[dict[str, ResolveObject], list[str], list[str]]:
    """Import the files absent from the pool. Returns (path -> clip, new clip ids, warnings)."""
    known = {_norm(p): c for c in _pool_clips(s) if (p := _clip_path(c))}
    missing = [f for f in files if _norm(f) not in known]
    imported: list[str] = []
    warnings: list[str] = []
    if missing:
        # Plain path strings: dict entries return None on Resolve 21.1.1.
        created = proxies(s.project.GetMediaPool().ImportMedia([str(f) for f in missing]))
        if not created:
            raise _fail(
                "MediaPool.ImportMedia returned nothing.",
                "Check that the files are supported media.",
                paths=[str(f) for f in missing],
            )
        for clip in created:
            imported.append(clip.GetUniqueId())
            if path := _clip_path(clip):
                known[_norm(path)] = clip
        unmatched = [str(f) for f in missing if _norm(f) not in known]
        if unmatched:
            warnings.append(f"Not imported by Resolve: {unmatched}")
    return known, imported, warnings


def _timeline_by_id(s: ResolveSession, timeline_id: str) -> ResolveObject | None:
    return next(
        (tl for _, tl in iter_timelines(s.project) if tl.GetUniqueId() == timeline_id), None
    )


def _offline_count(timeline: ResolveObject) -> tuple[int, int]:
    items = [it for _, _, _, it in iter_items(timeline, ("video", "audio"))]
    return len(items), sum(1 for it in items if it.GetMediaPoolItem() is None)


def _make_current(s: ResolveSession, timeline: ResolveObject) -> None:
    current = s.project.GetCurrentTimeline()
    if current is not None and current.GetUniqueId() == timeline.GetUniqueId():
        return
    if not s.project.SetCurrentTimeline(timeline):
        raise _fail("Project.SetCurrentTimeline returned False.")


# =====================================================================================
# deliver (also used by build_vertical_reel)
# =====================================================================================


@dataclass(frozen=True)
class _Plan:
    deliverable: Deliverable
    container: tuple[str, str] | None  # (format extension, codec id)


def _plan_outputs(s: ResolveSession, outputs: list[Deliverable]) -> list[_Plan]:
    """Validate presets/formats/codecs against the live lists before anything changes."""
    names = [d.name for d in outputs]
    if len(set(names)) != len(names):
        raise _invalid("Output names must be unique.")
    formats = _formats(s, audio=False) if any(d.format for d in outputs) else []
    plans: list[_Plan] = []
    for d in outputs:
        if d.preset is not None:
            _require_preset(s.project, d.preset)
        container: tuple[str, str] | None = None
        if d.format is not None:
            fmt = _find_format(formats, d.format)
            codec = _find_codec(fmt, d.codec) if d.codec else fmt.codecs[0]
            container = (fmt.extension, codec.id)
        plans.append(_Plan(d, container))
    return plans


def _target_dir(target_dir: str) -> Path:
    target = check_path(target_dir, runtime().allowed_roots, must_exist=True)
    if not target.is_dir():
        raise _invalid(f"{target} is not a directory.")
    return target


def _queue_jobs(
    s: ResolveSession, timeline: ResolveObject, target: Path, plans: list[_Plan]
) -> list[str]:
    project = s.project
    job_ids: list[str] = []
    with _as_current(s, timeline):
        for plan in plans:
            d = plan.deliverable
            # Order matters: preset, then container/codec (the format switch resets the
            # per-format settings such as the custom name), then the settings.
            if d.preset is not None and not project.LoadRenderPreset(d.preset):
                raise _fail(f"LoadRenderPreset({d.preset!r}) returned False.")
            if plan.container is not None and not project.SetCurrentRenderFormatAndCodec(
                *plan.container
            ):
                raise _fail(
                    "SetCurrentRenderFormatAndCodec returned False.", format=plan.container[0]
                )
            payload: dict[str, Any] = {
                "TargetDir": str(target),
                "CustomName": d.name,
                "SelectAllFrames": True,
            }
            if d.width is not None:
                payload["FormatWidth"] = d.width
            if d.height is not None:
                payload["FormatHeight"] = d.height
            _apply_settings(s, payload, None)
            job_ids.append(_add_job(s))
    return job_ids


def _wait_jobs(s: ResolveSession, job_ids: list[str], timeout_s: int) -> bool:
    """Poll until every job is at 100 %. Returns True on timeout; raises on a job error."""
    project = s.project
    deadline = time.monotonic() + timeout_s
    idle = 0
    while True:
        pcts: list[float] = []
        errors: list[str] = []
        for job_id in job_ids:
            status: dict[str, Any] = dict(project.GetRenderJobStatus(job_id) or {})
            if not status:
                errors.append(f"{job_id}: job no longer exists")
            elif (err := _error(status)) is not None:
                errors.append(f"{job_id}: {err}")
            pcts.append(_pct(status) or 0.0)
        if errors:
            raise _fail("Render failed: " + "; ".join(errors), job_ids=job_ids)
        if all(p >= _COMPLETE for p in pcts):
            return False
        idle = 0 if _in_progress(project) else idle + 1
        if idle >= _IDLE_POLLS:
            raise _fail("Rendering stopped before every job completed.", job_ids=job_ids)
        if time.monotonic() >= deadline:
            return True
        time.sleep(_POLL_S)


def _probe(path: Path) -> dict[str, Any] | None:
    """Video codec/size of a file through ffprobe; None when unavailable or unreadable."""
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                binary,
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
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            timeout=30,
        )
        streams: list[dict[str, Any]] = json.loads(proc.stdout or b"{}").get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return streams[0] if streams else None


def _collect_outputs(
    s: ResolveSession, plans: list[_Plan], job_ids: list[str], target: Path, started: float
) -> list[OutputFile]:
    """The files really produced: presets choose the container, so read the job list."""
    jobs = {
        str(j.get("JobId")): dict(j)
        for j in proxies(s.project.GetRenderJobList())
        if j.get("JobId")
    }
    out: list[OutputFile] = []
    for plan, job_id in zip(plans, job_ids, strict=True):
        name = plan.deliverable.name
        info = jobs.get(job_id, {})
        filename = str(info.get("OutputFilename") or "")
        base = Path(str(info.get("TargetDir") or target))
        path = base / filename if filename else None
        if path is None or not path.is_file():
            # No OutputFilename reported: look for what the job wrote under the requested name.
            found = sorted(base.glob(f"{name}.*"))
            path = found[0] if found else (path or base / name)
        exists = path.is_file()
        stat = path.stat() if exists else None
        probe = _probe(path) if exists else None
        out.append(
            OutputFile(
                name=name,
                job_id=job_id,
                path=str(path),
                exists=exists,
                fresh=stat is not None and stat.st_mtime >= started - _FRESH_SLACK_S,
                size_bytes=stat.st_size if stat else None,
                codec=str(probe["codec_name"]) if probe and "codec_name" in probe else None,
                width=int(probe["width"]) if probe and "width" in probe else None,
                height=int(probe["height"]) if probe and "height" in probe else None,
            )
        )
    return out


class _Rendered(BaseModel):
    job_ids: list[str]
    waited: bool
    timed_out: bool
    outputs: list[OutputFile]


def _render(
    s: ResolveSession,
    timeline: ResolveObject,
    target: Path,
    plans: list[_Plan],
    *,
    wait: bool,
    timeout_s: int,
) -> _Rendered:
    project = s.project
    _refuse_if_rendering(project)
    started = time.time()
    job_ids = _queue_jobs(s, timeline, target, plans)
    with restore_page_after(s.resolve):
        begun = bool(project.StartRendering(job_ids, False)) or _in_progress(project)
    if not begun:
        raise _fail("Project.StartRendering did not start.", job_ids=job_ids)
    timed_out = _wait_jobs(s, job_ids, timeout_s) if wait else False
    outputs = _collect_outputs(s, plans, job_ids, target, started) if wait else []
    return _Rendered(job_ids=job_ids, waited=wait, timed_out=timed_out, outputs=outputs)


def _outputs_problems(
    outputs: list[OutputFile], deliverables: list[Deliverable]
) -> list[dict[str, Any]]:
    """Per-output problems: missing/stale file, or ffprobe dimensions that differ."""
    problems: list[dict[str, Any]] = []
    for o, d in zip(outputs, deliverables, strict=True):
        issue: str | None = None
        if not Path(o.path).is_file():
            issue = "missing"
        elif not o.fresh:
            issue = "stale file (not written by this render)"
        elif d.width is not None and o.width is not None and o.width != d.width:
            issue = f"width {o.width} != {d.width}"
        elif d.height is not None and o.height is not None and o.height != d.height:
            issue = f"height {o.height} != {d.height}"
        if issue:
            problems.append({"path": o.path, "problem": issue})
    return problems


class DeliverParams(_Params):
    timeline: TimelineRef | None = None
    target_dir: str = Field(description="Existing output directory inside the allowed roots.")
    outputs: list[Deliverable] = Field(min_length=1, max_length=8)
    wait: bool = Field(default=True, description="Block until every render is finished.")
    timeout_s: int = Field(default=900, ge=1, le=3000, description="Max wait for the renders.")


class DeliverResult(BaseModel):
    timeline_id: str
    timeline_name: str
    target_dir: str
    job_ids: list[str]
    waited: bool
    timed_out: bool
    outputs: list[OutputFile]


def _verify_deliver(s: ResolveSession, p: DeliverParams, r: DeliverResult) -> Verification:
    if not r.waited:
        queued = {str(j.get("JobId")) for j in proxies(s.project.GetRenderJobList())}
        missing = [j for j in r.job_ids if j not in queued]
        return Verification(
            ok=not missing,
            observed={"queued": sorted(queued), "missing": missing},
            note="wait=false: files are not checked yet; use job_status or list_render_jobs.",
        )
    problems = _outputs_problems(r.outputs, p.outputs)
    note = "Render timed out before completion." if r.timed_out else None
    return Verification(
        ok=not problems and not r.timed_out, observed={"problems": problems}, note=note
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=DeliverParams,
    result=DeliverResult,
    verify=_verify_deliver,
    timeout_s=_ACTION_TIMEOUT_S,
)
def deliver(s: ResolveSession, p: DeliverParams) -> DeliverResult:
    """Render one or more outputs (preset/format/size) and report the real files produced."""
    target = _target_dir(p.target_dir)
    timeline = resolve_timeline(s, p.timeline)
    _refuse_if_rendering(s.project)
    plans = _plan_outputs(s, p.outputs)
    rendered = _render(s, timeline, target, plans, wait=p.wait, timeout_s=p.timeout_s)
    return DeliverResult(
        timeline_id=timeline.GetUniqueId(),
        timeline_name=timeline.GetName(),
        target_dir=str(target),
        **rendered.model_dump(),
    )


# =====================================================================================
# conform_from_edl
# =====================================================================================


@dataclass(frozen=True)
class _EdlEvent:
    reel: str
    clip_name: str | None
    src_in: str


def _parse_edl_events(text: str) -> list[_EdlEvent]:
    """CMX3600 events: reel, ``* FROM CLIP NAME`` and source-in (lenient on reel length)."""
    events: list[_EdlEvent] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("*") and events:
            key, _, value = line.lstrip("* ").strip().partition(":")
            if key.strip().upper() == "FROM CLIP NAME" and value.strip():
                events[-1] = _EdlEvent(events[-1].reel, value.strip(), events[-1].src_in)
        elif _EVENT_RE.match(line):
            tokens = line.split()
            tcs = tokens[-4:]
            if len(tokens) >= _MIN_EVENT_TOKENS and all(_TC_RE.match(t) for t in tcs):
                events.append(_EdlEvent(tokens[1], None, tcs[0]))
    return events


class ConformParams(_Params):
    media: list[str] = Field(description="Media files or folders (all their files are imported).")
    edl: str = Field(description="CMX3600 EDL file.")
    timeline_name: str = Field(min_length=1)
    fix_start_tc: bool = Field(
        default=True,
        description="When items are offline, set clip Start TC to the event source-in "
        "floored to the hour, then re-import once.",
    )


class StartTcFix(BaseModel):
    clip: str
    old: str
    new: str


class ConformResult(BaseModel):
    timeline_id: str
    timeline_name: str
    items: int
    linked: int
    offline: int
    start_tc_fixes: list[StartTcFix]
    imported_clip_ids: list[str]
    reimported: bool
    warnings: list[str]


def _import_edl(s: ResolveSession, path: Path, name: str) -> ResolveObject:
    before = {tl.GetUniqueId() for _, tl in iter_timelines(s.project)}
    tl = s.project.GetMediaPool().ImportTimelineFromFile(
        str(path), {"timelineName": name, "importSourceClips": False}
    )
    if tl is None:
        raise _fail(
            "MediaPool.ImportTimelineFromFile returned None.",
            "The timeline name may exist, or the EDL is not readable.",
        )
    if tl.GetUniqueId() in before:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "The import returned an EXISTING timeline; nothing new was created.",
            retryable=False,
        )
    return tl


def _tc_frames(tc: str, fps: float) -> int | None:
    try:
        return timecode_to_frames(tc, fps, ";" in tc)
    except DavinciError:
        return None


def _clip_keys(clip: ResolveObject) -> set[str]:
    name = str(clip.GetName() or "")
    file_name = Path(_clip_path(clip)).name
    return {k.lower() for k in (name, Path(name).stem, file_name, Path(file_name).stem) if k}


def _event_keys(ev: _EdlEvent) -> set[str]:
    values = [ev.reel, ev.clip_name or "", Path(ev.clip_name or "").stem]
    return {v.lower() for v in values if v}


def _plan_start_tc_fixes(
    clips: list[ResolveObject], events: list[_EdlEvent]
) -> list[tuple[ResolveObject, str, str]]:
    """(clip, old Start TC, new Start TC) for clips whose events fall outside their range."""
    fixes: list[tuple[ResolveObject, str, str]] = []
    for clip in clips:
        keys = _clip_keys(clip)
        mine = [e for e in events if _event_keys(e) & keys]
        if not mine:
            continue
        try:
            fps = float(clip.GetClipProperty("FPS") or 24)
        except ValueError:
            fps = 24.0
        old = str(clip.GetClipProperty("Start TC") or "00:00:00:00")
        start = _tc_frames(old, fps)
        ins = [(f, e.src_in) for e in mine if (f := _tc_frames(e.src_in, fps)) is not None]
        if start is None or not ins:
            continue
        first_frames, first_tc = min(ins)
        try:
            total = int(str(clip.GetClipProperty("Frames") or "0"))
        except ValueError:
            total = 0
        if total > 0:
            outside = first_frames < start or first_frames >= start + total
        else:
            outside = first_tc[:2] != old[:2]
        new = f"{first_tc[:2]}:00:00{first_tc[8]}00"
        if outside and new != old:
            fixes.append((clip, old, new))
    return fixes


def _conform_verify_state(s: ResolveSession, timeline_id: str) -> tuple[bool, int, int]:
    tl = _timeline_by_id(s, timeline_id)
    if tl is None:
        return False, 0, 0
    items, offline = _offline_count(tl)
    return True, items, offline


def _verify_conform(s: ResolveSession, _: ConformParams, r: ConformResult) -> Verification:
    exists, items, offline = _conform_verify_state(s, r.timeline_id)
    ok = exists and items > 0 and offline == 0
    note = None
    if exists and offline:
        note = f"{offline} of {items} items are still offline (check reel/clip names and Start TC)."
    return Verification(
        ok=ok, observed={"exists": exists, "items": items, "offline": offline}, note=note
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ConformParams,
    result=ConformResult,
    verify=_verify_conform,
    timeout_s=600.0,
)
def conform_from_edl(s: ResolveSession, p: ConformParams) -> ConformResult:
    """Import media + EDL as a timeline, fix offline events via Start TC, report linkage."""
    roots = runtime().allowed_roots
    edl_path = check_path(p.edl, roots, must_exist=True)
    if not edl_path.is_file():
        raise _invalid(f"{edl_path} is not a file.")
    events = _parse_edl_events(edl_path.read_text(encoding="utf-8", errors="replace"))
    if not events:
        raise _invalid("The EDL has no CMX3600 event.")
    files = _expand_media(p.media)
    _require_free_name(s, p.timeline_name)

    _, imported_ids, warnings = _import_missing(s, files)
    timeline = _import_edl(s, edl_path, p.timeline_name)
    info = _imported(s, timeline)
    fixes: list[StartTcFix] = []
    reimported = False
    if info.offline and p.fix_start_tc:
        planned = _plan_start_tc_fixes(_pool_clips(s), events)
        for clip, old, new in planned:
            clip.SetClipProperty("Start TC", new)
            got = str(clip.GetClipProperty("Start TC") or "")
            if got != new:
                raise _fail(
                    f"Start TC of {clip.GetName()!r} did not change (is {got!r}).",
                    "SetClipProperty can return True without applying.",
                    clip=clip.GetName(),
                )
            fixes.append(StartTcFix(clip=str(clip.GetName()), old=old, new=new))
        if fixes:
            if not s.project.GetMediaPool().DeleteTimelines([timeline]):
                raise _fail("MediaPool.DeleteTimelines returned False.")
            if p.timeline_name in _names(s):
                raise _fail("The offline timeline is still present after DeleteTimelines.")
            timeline = _import_edl(s, edl_path, p.timeline_name)
            info = _imported(s, timeline)
            reimported = True
        else:
            warnings.append(
                "Offline items remain but no clip matches the EDL reels/clip names or needs a "
                "Start TC change: check the reel names."
            )
    if info.warning and info.offline:
        warnings.append(info.warning)
    return ConformResult(
        timeline_id=info.id,
        timeline_name=info.name,
        items=info.items,
        linked=info.linked,
        offline=info.offline,
        start_tc_fixes=fixes,
        imported_clip_ids=imported_ids,
        reimported=reimported,
        warnings=warnings,
    )


# =====================================================================================
# build_vertical_reel
# =====================================================================================


class ReelParams(_Params):
    clips: list[str] = Field(min_length=1, description="Media files in timeline order.")
    timeline_name: str = Field(min_length=1)
    width: int = Field(default=1080, ge=16, le=16384)
    height: int = Field(default=1920, ge=16, le=16384)
    subtitles: bool = Field(default=True, description="Subtitles from audio (Studio + Extras).")
    render: Deliverable | None = Field(default=None, description="Render the reel afterwards.")
    target_dir: str | None = Field(default=None, description="Output folder (required to render).")
    timeout_s: int = Field(default=900, ge=1, le=3000, description="Max wait for subtitles/render.")

    @model_validator(mode="after")
    def _render_needs_target(self) -> ReelParams:
        if self.render is not None and not self.target_dir:
            raise ValueError("target_dir is required when render is given")
        return self


class ReelResult(BaseModel):
    timeline_id: str
    timeline_name: str
    width: int
    height: int
    resolution_via: Literal["timeline", "project"]
    clip_order: list[str]
    imported_clip_ids: list[str]
    subtitle_count: int
    outputs: list[OutputFile]
    warnings: list[str]


def _create_timeline(s: ResolveSession, name: str) -> ResolveObject:
    tl = s.project.GetMediaPool().CreateEmptyTimeline(name)
    if tl is None:
        raise _fail("MediaPool.CreateEmptyTimeline returned None.")
    return tl


def _has_resolution(obj: ResolveObject, width: int, height: int) -> bool:
    return str(obj.GetSetting("timelineResolutionWidth")) == str(width) and str(
        obj.GetSetting("timelineResolutionHeight")
    ) == str(height)


def _set_timeline_resolution(tl: ResolveObject, width: int, height: int) -> bool:
    tl.SetSetting("useCustomSettings", "1")
    tl.SetSetting("timelineResolutionWidth", str(width))
    tl.SetSetting("timelineResolutionHeight", str(height))
    return _has_resolution(tl, width, height)


def _subtitle_items(tl: ResolveObject) -> list[ResolveObject]:
    return [it for _, _, _, it in iter_items(tl, ("subtitle",))]


def _make_subtitles(s: ResolveSession, tl: ResolveObject, timeout_s: int) -> int:
    s.caps.require_studio("Subtitles from audio")
    s.caps.require(tl, "CreateSubtitlesFromAudio", since=_SUBTITLE_SINCE)
    _make_current(s, tl)
    # An error STRING means the AI Extras package is missing (EXTRAS_MISSING).
    _check_ai(tl.CreateSubtitlesFromAudio(), "CreateSubtitlesFromAudio")
    deadline = time.monotonic() + timeout_s
    has_getter = s.caps.has(s.project, _SUBTITLE_GETTER)
    poll = _status_poll(_SUBTITLE_GETTER) if has_getter else None
    while True:
        if poll is not None:
            status = dict(poll(s))
            if status.get("Error"):
                raise _fail(f"Subtitle generation failed: {status['Error']}")
            if status.get("done"):
                break
        elif _subtitle_items(tl):  # no status getter: wait for the items themselves
            break
        if time.monotonic() >= deadline:
            raise DavinciError(
                ErrorCode.TIMEOUT, "Subtitle generation did not finish in time.", retryable=True
            )
        time.sleep(_POLL_S)
    return len(_subtitle_items(tl))


def _video_paths(tl: ResolveObject) -> list[str]:
    """Source file (or name) of every video item of track 1, in timeline order."""
    out: list[str] = []
    for item in proxies(tl.GetItemListInTrack("video", 1)):
        clip = item.GetMediaPoolItem()
        path = _clip_path(clip) if clip is not None else ""
        out.append(_norm(path) if path else str(item.GetName()))
    return out


def _verify_reel(s: ResolveSession, p: ReelParams, r: ReelResult) -> Verification:
    tl = _timeline_by_id(s, r.timeline_id)
    if tl is None:
        return Verification(ok=False, note="The timeline no longer exists.")
    expected = [_norm(c) for c in p.clips]
    got = _video_paths(tl)
    subs = len(_subtitle_items(tl))
    problems = _outputs_problems(r.outputs, [p.render] * len(r.outputs)) if p.render else []
    checks = {
        "resolution": _has_resolution(tl, p.width, p.height),
        "clip_order": got == expected,
        "subtitles": subs > 0 or not p.subtitles,
        "outputs": not problems and (p.render is None or len(r.outputs) == 1),
    }
    return Verification(
        ok=all(checks.values()),
        observed={"checks": checks, "video_items": got, "subtitles": subs, "problems": problems},
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ReelParams,
    result=ReelResult,
    verify=_verify_reel,
    timeout_s=_ACTION_TIMEOUT_S,
)
def build_vertical_reel(s: ResolveSession, p: ReelParams) -> ReelResult:
    """Build a vertical timeline from clips, add subtitles and optionally render it."""
    files = [check_path(c, runtime().allowed_roots, must_exist=True) for c in p.clips]
    if any(f.is_dir() for f in files):
        raise _invalid("clips must be files, not folders.")
    _require_free_name(s, p.timeline_name)
    plans: list[_Plan] = []
    target: Path | None = None
    if p.render is not None and p.target_dir:
        target = _target_dir(p.target_dir)
        _refuse_if_rendering(s.project)
        plans = _plan_outputs(s, [p.render])
    if p.subtitles:
        s.caps.require_studio("Subtitles from audio")

    known, imported_ids, warnings = _import_missing(s, files)
    missing = [str(f) for f in files if _norm(f) not in known]
    if missing:
        raise _fail("Some clips are not in the Media Pool after import.", paths=missing)
    clips = [known[_norm(f)] for f in files]

    project = s.project
    pool = project.GetMediaPool()
    via: Literal["timeline", "project"] = "timeline"
    tl = _create_timeline(s, p.timeline_name)
    if not _set_timeline_resolution(tl, p.width, p.height):
        # The timeline-level setting did not stick: set the project resolution BEFORE
        # creating the timeline, which then follows the project settings.
        if not pool.DeleteTimelines([tl]) or p.timeline_name in _names(s):
            raise _fail("Could not delete the timeline whose resolution did not stick.")
        previous = {
            k: project.GetSetting(k)
            for k in ("timelineResolutionWidth", "timelineResolutionHeight")
        }
        for key, value in (
            ("timelineResolutionWidth", p.width),
            ("timelineResolutionHeight", p.height),
        ):
            project.SetSetting(key, str(value))
        if not _has_resolution(project, p.width, p.height):
            raise _fail("Project resolution did not change either.", previous=previous)
        tl = _create_timeline(s, p.timeline_name)
        if not _has_resolution(tl, p.width, p.height):
            raise _fail("The new timeline does not follow the project resolution.")
        via = "project"
        warnings.append(
            f"Timeline.SetSetting did not stick: the PROJECT resolution was changed instead "
            f"(previous values: {previous}); new timelines now default to {p.width}x{p.height}."
        )

    _make_current(s, tl)
    placed = proxies(pool.AppendToTimeline(clips))
    if len(placed) != len(clips):
        raise _fail(
            f"AppendToTimeline placed {len(placed)} of {len(clips)} clips.",
            timeline_id=tl.GetUniqueId(),
        )
    subtitle_count = _make_subtitles(s, tl, p.timeout_s) if p.subtitles else 0

    outputs: list[OutputFile] = []
    if p.render is not None and target is not None:
        outputs = _render(s, tl, target, plans, wait=True, timeout_s=p.timeout_s).outputs
    return ReelResult(
        timeline_id=tl.GetUniqueId(),
        timeline_name=tl.GetName(),
        width=p.width,
        height=p.height,
        resolution_via=via,
        clip_order=[str(c.GetName()) for c in clips],
        imported_clip_ids=imported_ids,
        subtitle_count=subtitle_count,
        outputs=outputs,
        warnings=warnings,
    )


# =====================================================================================
# apply_look
# =====================================================================================


class LookParams(_Params):
    timeline: TimelineRef | None = None
    lut: str | None = Field(default=None, description="LUT file (installed under the LUT root).")
    node: int = Field(default=1, ge=1, description="1-based node receiving the LUT.")
    color_group: str | None = Field(
        default=None, description="Color group for every video item (created if missing)."
    )
    grab_still: bool = Field(default=False, description="Grab a still of the first item.")

    @model_validator(mode="after")
    def _something(self) -> LookParams:
        if self.lut is None and self.color_group is None and not self.grab_still:
            raise ValueError("give at least one of lut, color_group or grab_still")
        return self


class LookItem(BaseModel):
    item_id: str
    name: str
    lut: str | None = None
    group: str | None = None


class LookResult(BaseModel):
    timeline_id: str
    items: list[LookItem]
    lut_reference: str | None
    color_group: str | None
    stills_before: int | None
    stills_after: int | None


def _group_name(item: ResolveObject) -> str | None:
    group = item.GetColorGroup()
    return None if group is None else str(group.GetName())


def _find_or_create_group(s: ResolveSession, name: str) -> ResolveObject:
    for group in proxies(s.project.GetColorGroupsList()):
        if group.GetName() == name:
            return group
    created = s.project.AddColorGroup(name)
    if created is None:
        raise _fail("Project.AddColorGroup returned None.")
    return created


def _still_count(s: ResolveSession) -> int:
    return len(proxies(_resolve_album(s, None).GetStills()))


def _verify_look(s: ResolveSession, p: LookParams, r: LookResult) -> Verification:
    tl = resolve_timeline(s, p.timeline)
    video = {it.GetUniqueId(): it for _, _, _, it in iter_items(tl, ("video",))}
    problems: list[str] = []
    for entry in r.items:
        item = video.get(entry.item_id)
        if item is None:
            problems.append(f"{entry.name}: item gone")
            continue
        if r.lut_reference is not None:
            got = str(item.GetNodeGraph().GetLUT(p.node) or "")
            if Path(got).name != Path(r.lut_reference).name:
                problems.append(f"{entry.name}: LUT is {got!r}")
        if p.color_group is not None and _group_name(item) != p.color_group:
            problems.append(f"{entry.name}: group is {_group_name(item)!r}")
    if p.color_group is not None:
        group = _existing_group(s, p.color_group)
        members: set[str] = (
            {i.GetUniqueId() for i in proxies(group.GetClipsInTimeline(tl))} if group else set()
        )
        absent = [e.name for e in r.items if e.item_id not in members]
        if absent:
            problems.append(f"not listed by ColorGroup.GetClipsInTimeline: {absent}")
    now = _still_count(s) if p.grab_still else None
    if p.grab_still and (now is None or r.stills_before is None or now <= r.stills_before):
        problems.append("still count did not increase")
    return Verification(ok=not problems, observed={"problems": problems, "stills": now})


def _existing_group(s: ResolveSession, name: str) -> ResolveObject | None:
    return next((g for g in proxies(s.project.GetColorGroupsList()) if g.GetName() == name), None)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=LookParams,
    result=LookResult,
    verify=_verify_look,
    page="color",
    timeout_s=300.0,
)
def apply_look(s: ResolveSession, p: LookParams) -> LookResult:
    """Apply a LUT to every video item, group them in a color group and grab a still."""
    tl = resolve_timeline(s, p.timeline)
    items = [it for _, _, _, it in iter_items(tl, ("video",))]
    if not items:
        raise DavinciError(ErrorCode.NOT_FOUND, "The timeline has no video item.", retryable=False)
    graphs: list[ResolveObject] = []
    for item in items:  # fail before touching anything
        graph = item.GetNodeGraph()
        if graph is None:
            raise DavinciError(
                ErrorCode.WRONG_STATE, f"No node graph for {item.GetName()!r}.", retryable=False
            )
        _check_node(graph, p.node)
        graphs.append(graph)
    reference = _lut_reference(s, p.lut) if p.lut is not None else None
    if p.grab_still:
        _resolve_album(s, None)  # a current still album must exist

    results: list[LookItem] = []
    with _as_current(s, tl):
        if reference is not None:
            for item, graph in zip(items, graphs, strict=True):
                if not graph.SetLUT(p.node, reference):
                    raise _fail(
                        f"Graph.SetLUT failed on {item.GetName()!r}.",
                        "The LUT may be invalid or outside the LUT root.",
                    )
        if p.color_group is not None:
            group = _find_or_create_group(s, p.color_group)
            for item in items:
                if not item.AssignToColorGroup(group):
                    raise _fail(f"AssignToColorGroup failed on {item.GetName()!r}.")
        before = after = None
        if p.grab_still:
            before = _still_count(s)
            _grab_first(s, tl, items[0])
            after = _still_count(s)
    for item, graph in zip(items, graphs, strict=True):
        results.append(
            LookItem(
                item_id=item.GetUniqueId(),
                name=str(item.GetName()),
                lut=str(graph.GetLUT(p.node) or "") if reference is not None else None,
                group=_group_name(item) if p.color_group is not None else None,
            )
        )
    return LookResult(
        timeline_id=tl.GetUniqueId(),
        items=results,
        lut_reference=reference,
        color_group=p.color_group,
        stills_before=before,
        stills_after=after,
    )


def _grab_first(s: ResolveSession, tl: ResolveObject, first: ResolveObject) -> None:
    """Park the playhead on the first item (absolute frame -> timecode), grab, restore."""
    raw = tl.GetSetting("timelineFrameRate") or s.project.GetSetting("timelineFrameRate")
    try:
        fps = float(str(raw))
    except ValueError:
        fps = 24.0
    drop = str(tl.GetSetting("timelineDropFrameTimecode")) in ("1", "True", "true")
    timecode = frames_to_timecode(int(first.GetStart()), fps, drop)
    saved = tl.GetCurrentTimecode()
    if not tl.SetCurrentTimecode(timecode):
        raise _fail(f"Timeline.SetCurrentTimecode({timecode!r}) returned False.")
    try:
        if tl.GrabStill() is None:
            raise _fail("Timeline.GrabStill returned None.", "Needs the Color page and a clip.")
    finally:
        if saved:
            tl.SetCurrentTimecode(saved)


# =====================================================================================
# title_overlay
# =====================================================================================


class TitleParams(_Params):
    item: ItemRef
    text: str = Field(min_length=1, max_length=2000)
    font: str | None = None
    size_from: float = Field(default=0.0, ge=0.0, le=10.0, description="Text+ Size at frame 0.")
    size_to: float = Field(default=0.1, ge=0.0, le=10.0, description="Text+ Size at `frames`.")
    frames: int = Field(default=24, ge=1, le=100000, description="Animation length in frames.")
    merge: bool = Field(default=True, description="Merge the text over the clip into MediaOut.")


class TitleResult(BaseModel):
    item_id: str
    comp: str
    text_tool: str
    merge_tool: str | None
    keyframes: dict[str, Any]


@contextmanager
def _locked(comp: ResolveObject) -> Generator[None]:
    """Lock for structural edits only; always unlocked after."""
    comp.Lock()
    try:
        yield
    finally:
        comp.Unlock()


def _free_name(comp: ResolveObject, base: str) -> str:
    taken = {_tool_name(t) for t in _tools(comp)}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}{n}"
    return check_tool_name(name)


def _add_named(comp: ResolveObject, reg_id: str, name: str) -> ResolveObject:
    tool = comp.AddTool(reg_id, _AUTO_POS, _AUTO_POS)
    if tool is None:
        raise _fail(f"Comp.AddTool({reg_id!r}) returned nothing.")
    if _tool_name(tool) != name:
        tool.SetAttrs({"TOOLS_Name": name})
        if _tool_name(tool) != name:
            raise _fail(f"Could not rename the {reg_id} tool to {name!r}.")
    return tool


def _tool_by_reg_id(comp: ResolveObject, reg_id: str) -> ResolveObject:
    for tool in _tools(comp):
        if _tool_id(tool) == reg_id:
            return tool
    raise DavinciError(
        ErrorCode.WRONG_STATE,
        f"The comp has no {reg_id} tool.",
        hint="A Fusion comp on a clip normally holds MediaIn and MediaOut.",
        retryable=False,
    )


def _connect(tool: ResolveObject, input_id: str, output: ResolveObject) -> None:
    if not tool.ConnectInput(input_id, output):
        raise _fail(f"ConnectInput({input_id!r}) returned False on {_tool_name(tool)!r}.")


def _verify_title(s: ResolveSession, p: TitleParams, r: TitleResult) -> Verification:
    _, comp, _ = _open_comp(s, _CompParams(item=p.item, comp=r.comp))
    text = comp.FindTool(r.text_tool)
    if text is None:
        return Verification(ok=False, note=f"Tool {r.text_tool!r} not found.")
    checks: dict[str, bool] = {
        "is_text_plus": _tool_id(text) == "TextPlus",
        "styled_text": text.GetInput("StyledText") == p.text,
        "font": p.font is None or text.GetInput("Font") == p.font,
    }
    size = _find_input(s, text, "Size")
    keys: Any = size.GetKeyFrames()
    key_count = len(cast("dict[Any, Any]", keys)) if isinstance(keys, dict) else 0
    checks["size_animated"] = _is_animated(size) and key_count >= _MIN_KEYS
    checks["size_from"] = _same(_read_input(text, "Size", 0.0), p.size_from)
    checks["size_to"] = _same(_read_input(text, "Size", float(p.frames)), p.size_to)
    if p.merge and r.merge_tool is not None:
        merge = comp.FindTool(r.merge_tool)
        media_out = _tool_by_reg_id(comp, "MediaOut")
        fg = _find_input(s, merge, "Foreground").GetConnectedOutput() if merge else None
        out = _find_input(s, media_out, "Input").GetConnectedOutput()
        checks["text_into_merge"] = fg is not None and _output_tool_name(fg) == r.text_tool
        checks["merge_into_mediaout"] = out is not None and _output_tool_name(out) == r.merge_tool
    return Verification(ok=all(checks.values()), observed={"checks": checks})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TitleParams,
    result=TitleResult,
    verify=_verify_title,
    timeout_s=120.0,
)
def title_overlay(s: ResolveSession, p: TitleParams) -> TitleResult:
    """Add an animated Text+ title (and Merge over the clip) to a timeline item's Fusion comp."""
    item = resolve_item(s, p.item)
    s.caps.require(item, "AddFusionComp")
    if not _comp_names(item) and item.AddFusionComp() is None:
        raise _fail("TimelineItem.AddFusionComp returned nothing.")
    _, comp, comp_name = _open_comp(s, _CompParams(item=p.item))
    text_name = _free_name(comp, "MCP_Title")
    merge_name = _free_name(comp, "MCP_TitleMerge") if p.merge else None
    media_in = _tool_by_reg_id(comp, "MediaIn") if p.merge else None
    media_out = _tool_by_reg_id(comp, "MediaOut") if p.merge else None

    # What already feeds MediaOut stays as the background; else MediaIn.
    feed: ResolveObject | None = None
    if media_in is not None and media_out is not None:
        feed = _find_input(s, media_out, "Input").GetConnectedOutput()
        if feed is None:
            feed = media_in.FindMainOutput(1)

    with _transaction(comp, "MCP title overlay"):
        with _locked(comp):
            text = _add_named(comp, "TextPlus", text_name)
            if merge_name is not None and feed is not None:
                merge = _add_named(comp, "Merge", merge_name)
                _connect(merge, "Background", feed)
                _connect(merge, "Foreground", text.FindMainOutput(1))
                _connect(media_out, "Input", merge.FindMainOutput(1))
        # Input edits stay unlocked: SetInput under Lock is not applied at render.
        text.SetInput("StyledText", p.text)
        if p.font is not None:
            text.SetInput("Font", p.font)
        size = _find_input(s, text, "Size")  # exact id; "TransformSize" is another input
        if not _is_animated(size) and not text.AddModifier("Size", "BezierSpline"):
            raise _fail("AddModifier('Size', 'BezierSpline') returned False.")
        text.SetInput("Size", p.size_from, 0.0)
        text.SetInput("Size", p.size_to, float(p.frames))
    return TitleResult(
        item_id=item.GetUniqueId(),
        comp=comp_name,
        text_tool=text_name,
        merge_tool=merge_name,
        keyframes={
            "0": _read_input(text, "Size", 0.0),
            str(p.frames): _read_input(text, "Size", float(p.frames)),
        },
    )


ACTIONS = [conform_from_edl, deliver, build_vertical_reel, apply_look, title_overlay]
