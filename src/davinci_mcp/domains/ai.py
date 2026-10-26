# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""AI and analysis domain: transcription, subtitles, scene cuts, reframe, slate, speech, deblur.

Resolve 21.1.1 pairs each long AI launch (``TranscribeAudio``, ``DetectSceneCuts``,
``SmartReframe``, ``CreateSubtitlesFromAudio``, ``AnalyzeForSlate``) with a
``Project.Get*Status()`` getter, so those actions are ``long_running`` jobs (``job_wait``).
The others (Intellisearch, audio classification, deblur, speech) have no status getter and run
synchronously with a long ``timeout_s``.

API traps encoded here:

* AI methods return an error STRING (``Required package ... not installed``) when the Extras
  pack is missing, and ``AnalyzeForSlate`` returns a bare ``False``. A return value is never
  coerced with ``bool()``: strings raise ``EXTRAS_MISSING``, falsy values ``API_CALL_FAILED``
  with the Extras hint.
* ``resolve.MARKER_*`` constants are missing on some builds: ``const()`` raises
  ``CAPABILITY_UNAVAILABLE`` instead of silently passing a string.
* Enum-keyed settings (``SUBTITLE_*`` / ``AUTO_CAPTION_*``) are always resolved to live constants.
* ``GetClipProperty('Transcription')`` is truncated: transcripts come from ``GetTranscription``.
* AI job statuses use the English values documented in the stub ('Complete', 'Cancelled',
  'Failed'); unknown values fall back to ``CompletionPercentage`` / ``Error``.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import Empty, JobRequest, JobStatusPoll, Kind, PlanDraft, action
from davinci_mcp.api._generated.resolve_21_1 import (
    AutoCaptionLanguage,
    AutoCaptionLineBreak,
    AutoCaptionPreset,
    DeblurOptions,
    SlateMarkerColor,
)
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import (
    ClipRef,
    FolderRef,
    ItemRef,
    const,
    proxies,
    resolve_clip,
    resolve_folder,
    resolve_item,
    resolve_timeline,
    walk_folders,
)
from davinci_mcp.safety.paths import SafeFileName, check_path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "ai"
_JOB_SINCE = (21, 1, 1)
_SYNC_TIMEOUT_S = 1800.0
_IDLE_POLLS_BEFORE_DONE = 10
_EXTRAS_WORDS = ("package", "not installed", "extras", "required")
_EXTRAS_HINT = (
    "Install the matching package from the Extras Download Manager (Resolve Studio application "
    "menu) before calling again; AnalyzeForSlate needs AI Slate ID, Intellisearch needs AI "
    "IntelliSearch, GenerateSpeech needs AI Speech Generator."
)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(_Params):
    """A Media Pool clip or a folder (all its clips, nested folders included)."""

    clip: ClipRef | None = None
    folder: FolderRef | None = Field(default=None, description="Folder; {} means the root.")

    @model_validator(mode="after")
    def _one(self) -> Target:
        if (self.clip is None) == (self.folder is None):
            raise ValueError("use exactly one of clip or folder")
        return self


class Done(BaseModel):
    target: str
    detail: str | None = None


# -- helpers ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast("dict[Any, Any]", value).items()}


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _target(s: ResolveSession, t: Target) -> tuple[ResolveObject, str]:
    if t.clip is not None:
        clip = resolve_clip(s, t.clip)
        return clip, f"clip:{clip.GetUniqueId()}"
    folder = resolve_folder(s, t.folder)
    return folder, f"folder:{folder.GetName()}"


def _check_ai(value: object, what: str) -> bool:
    """Interpret an AI return: error STRING -> EXTRAS_MISSING, falsy -> API_CALL_FAILED."""
    if isinstance(value, str):
        text = value.strip()
        extras = any(word in text.lower() for word in _EXTRAS_WORDS)
        raise DavinciError(
            ErrorCode.EXTRAS_MISSING if extras else ErrorCode.API_CALL_FAILED,
            f"{what}: {text or 'empty error string'}",
            hint=_EXTRAS_HINT if extras else None,
            retryable=False,
        )
    if not value:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            f"{what} returned False.",
            hint="Resolve returns a bare False when an Extras package is missing, the Studio "
            "requirements are not met, the media has no suitable stream, or another AI job "
            f"is running. {_EXTRAS_HINT}",
            retryable=False,
        )
    return True


# "Inactive" is excluded: it is reported both before start and after completion.
_KNOWN_ACTIVE_STATUSES = frozenset({"Initializing", "Analyzing", "Cancelling"})


def _status_poll(getter: str) -> JobStatusPoll:
    """Poll ``Project.<getter>()``. Adds ``done`` when the status falls idle after progress."""
    seen_progress = False
    idle = 0

    def poll(s: ResolveSession) -> Mapping[str, Any]:
        nonlocal seen_progress, idle
        project = s.project
        s.caps.require(project, getter, since=_JOB_SINCE)
        raw = _as_dict(getattr(project, getter)())
        # The stub documents English JobStatus values for these analyses; trust them when
        # recognised and fall back to the progress heuristic for unknown (localized) values.
        status = raw.get("JobStatus")
        if status == "Complete":
            raw["done"] = True
            return raw
        if status in {"Cancelled", "Failed"} and not raw.get("Error"):
            raw["Error"] = f"Resolve reported the job as {status}."
            return raw
        if status in _KNOWN_ACTIVE_STATUSES:
            seen_progress = seen_progress or "CompletionPercentage" in raw
            idle = 0
            return raw
        if "CompletionPercentage" in raw:
            seen_progress = True
            idle = 0
        elif not raw.get("Error"):
            idle += 1
            if seen_progress or idle >= _IDLE_POLLS_BEFORE_DONE:
                raw["done"] = True
        return raw

    return poll


def _require_status(s: ResolveSession, getter: str) -> None:
    s.caps.require(s.project, getter, since=_JOB_SINCE)


# -- long-running jobs ------------------------------------------------------------------


class TranscribeParams(Target):
    speaker_detection: bool | None = Field(default=None, description="Detect speakers.")
    as_nested_clip: bool = False


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TranscribeParams,
    long_running=True,
    since=_JOB_SINCE,
    studio_only=True,
)
def transcribe_audio(s: ResolveSession, p: TranscribeParams) -> JobRequest:
    """Transcribe the audio of a clip or folder (job; read it with get_transcription)."""
    obj, label = _target(s, p)
    s.caps.require(obj, "TranscribeAudio", since=_JOB_SINCE)
    _require_status(s, "GetTranscribeAudioStatus")
    args: list[Any] = [] if p.speaker_detection is None else [p.speaker_detection]
    if p.as_nested_clip:
        args = [p.speaker_detection, True]
    return JobRequest(
        kind="ai.transcribe_audio",
        label=f"Transcribe {label}",
        launch=lambda _s: _check_ai(obj.TranscribeAudio(*args), "TranscribeAudio"),
        poll=_status_poll("GetTranscribeAudioStatus"),
    )


class SubtitleParams(_Params):
    language: AutoCaptionLanguage | None = Field(
        default=None, description="resolve.AUTO_CAPTION_* constant name (default auto-detect)."
    )
    caption_preset: AutoCaptionPreset | None = None
    chars_per_line: int | None = Field(default=None, ge=1, le=60)
    line_break: AutoCaptionLineBreak | None = None
    gap: int | None = Field(default=None, ge=0, le=10, description="Frames between subtitles.")


def _caption_settings(s: ResolveSession, p: SubtitleParams) -> dict[object, object]:
    settings: dict[object, object] = {}
    if p.language is not None:
        settings[const(s, "SUBTITLE_LANGUAGE")] = const(s, p.language)
    if p.caption_preset is not None:
        settings[const(s, "SUBTITLE_CAPTION_PRESET")] = const(s, p.caption_preset)
    if p.chars_per_line is not None:
        settings[const(s, "SUBTITLE_CHARS_PER_LINE")] = p.chars_per_line
    if p.line_break is not None:
        settings[const(s, "SUBTITLE_LINE_BREAK")] = const(s, p.line_break)
    if p.gap is not None:
        settings[const(s, "SUBTITLE_GAP")] = p.gap
    return settings


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SubtitleParams,
    long_running=True,
    since=_JOB_SINCE,
    studio_only=True,
)
def create_subtitles_from_audio(s: ResolveSession, p: SubtitleParams) -> JobRequest:
    """Create subtitles from the audio of the current timeline (job)."""
    timeline = resolve_timeline(s, None)
    s.caps.require(timeline, "CreateSubtitlesFromAudio", since=_JOB_SINCE)
    _require_status(s, "GetCreateSubtitlesFromAudioStatus")
    settings = _caption_settings(s, p)

    def launch(_s: ResolveSession) -> bool:
        raw = (
            timeline.CreateSubtitlesFromAudio(settings)
            if settings
            else (timeline.CreateSubtitlesFromAudio())
        )
        return _check_ai(raw, "CreateSubtitlesFromAudio")

    return JobRequest(
        kind="ai.create_subtitles_from_audio",
        label=f"Subtitles from audio for {timeline.GetName()}",
        launch=launch,
        poll=_status_poll("GetCreateSubtitlesFromAudioStatus"),
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=Empty,
    long_running=True,
    since=_JOB_SINCE,
    studio_only=True,
)
def detect_scene_cuts(s: ResolveSession, _: BaseModel) -> JobRequest:
    """Detect and make scene cuts along the current timeline (job)."""
    timeline = resolve_timeline(s, None)
    s.caps.require(timeline, "DetectSceneCuts", since=_JOB_SINCE)
    _require_status(s, "GetDetectSceneCutsStatus")
    return JobRequest(
        kind="ai.detect_scene_cuts",
        label=f"Detect scene cuts in {timeline.GetName()}",
        launch=lambda _s: _check_ai(timeline.DetectSceneCuts(), "DetectSceneCuts"),
        poll=_status_poll("GetDetectSceneCutsStatus"),
    )


class ReframeParams(_Params):
    item: ItemRef


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ReframeParams,
    long_running=True,
    since=_JOB_SINCE,
    studio_only=True,
)
def smart_reframe(s: ResolveSession, p: ReframeParams) -> JobRequest:
    """Run Smart Reframe on a timeline item (job)."""
    item = resolve_item(s, p.item)
    s.caps.require(item, "SmartReframe", since=_JOB_SINCE)
    _require_status(s, "GetSmartReframeStatus")
    return JobRequest(
        kind="ai.smart_reframe",
        label=f"Smart reframe {item.GetName()}",
        launch=lambda _s: _check_ai(item.SmartReframe(), "SmartReframe"),
        poll=_status_poll("GetSmartReframeStatus"),
    )


class SlateParams(Target):
    marker_color: SlateMarkerColor = Field(
        default="MARKER_BLUE", description="resolve.MARKER_* constant name (not MARKER_NONE)."
    )

    @model_validator(mode="after")
    def _color(self) -> SlateParams:
        if self.marker_color == "MARKER_NONE":
            raise ValueError("MARKER_NONE is not a valid slate marker color")
        return self


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SlateParams,
    long_running=True,
    since=_JOB_SINCE,
    studio_only=True,
)
def analyze_for_slate(s: ResolveSession, p: SlateParams) -> JobRequest:
    """Run Slate ID analysis on a clip or folder (job, needs the AI Slate ID Extras)."""
    obj, label = _target(s, p)
    s.caps.require(obj, "AnalyzeForSlate", since=_JOB_SINCE)
    _require_status(s, "GetAnalyzeForSlateStatus")
    color = const(s, p.marker_color)  # MARKER_* constants are missing on some builds
    return JobRequest(
        kind="ai.analyze_for_slate",
        label=f"Slate analysis of {label}",
        launch=lambda _s: _check_ai(obj.AnalyzeForSlate(color), "AnalyzeForSlate"),
        poll=_status_poll("GetAnalyzeForSlateStatus"),
    )


# -- synchronous analysis ---------------------------------------------------------------------


class IntellisearchParams(Target):
    identify_faces: bool = False
    better_mode: bool = Field(default=False, description="Needs 'AI IntelliSearch - Better'.")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=IntellisearchParams,
    result=Done,
    studio_only=True,
    timeout_s=_SYNC_TIMEOUT_S,
    verify_reason="Resolve exposes no getter for Intellisearch analysis state.",
)
def analyze_for_intellisearch(s: ResolveSession, p: IntellisearchParams) -> Done:
    """Run Intellisearch analysis on a clip or folder (blocking, needs AI Extras)."""
    obj, label = _target(s, p)
    s.caps.require(obj, "AnalyzeForIntellisearch")
    _check_ai(
        obj.AnalyzeForIntellisearch(p.identify_faces, p.better_mode), "AnalyzeForIntellisearch"
    )
    return Done(target=label)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=Target,
    result=Done,
    studio_only=True,
    timeout_s=_SYNC_TIMEOUT_S,
    verify_reason="Classification is written to the clip Category property; no direct getter.",
)
def perform_audio_classification(s: ResolveSession, p: Target) -> Done:
    """Classify the audio of a clip or folder (blocking; works without Extras)."""
    obj, label = _target(s, p)
    s.caps.require(obj, "PerformAudioClassification")
    _check_ai(obj.PerformAudioClassification(), "PerformAudioClassification")
    return Done(target=label)


def _plan_clear_classification(s: ResolveSession, p: Target) -> PlanDraft:
    obj, label = _target(s, p)
    clips = [obj] if p.clip is not None else _folder_clips(obj)
    state = {c.GetUniqueId(): c.GetClipProperty("Category") for c in clips}
    return PlanDraft(
        summary=f"Clear audio classification of {label} ({len(clips)} clip(s)).",
        effects=["Resets Category to 'Uncategorized' (not to an empty string)."],
        targets=[label],
        fingerprint=_fingerprint(state),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=Target,
    result=Done,
    studio_only=True,
    plan=_plan_clear_classification,
    verify_reason="Category is reset to 'Uncategorized'; Resolve exposes no dedicated getter.",
)
def clear_audio_classification(s: ResolveSession, p: Target) -> Done:
    """Clear the audio classification of a clip or folder."""
    obj, label = _target(s, p)
    s.caps.require(obj, "ClearAudioClassification")
    _check_ai(obj.ClearAudioClassification(), "ClearAudioClassification")
    return Done(target=label)


class DeblurParams(Target):
    options: DeblurOptions | None = None


class DeblurResult(BaseModel):
    target: str
    created_clip_ids: list[str]


def _flatten(value: object) -> list[ResolveObject]:
    out: list[ResolveObject] = []
    for entry in proxies(value):
        if isinstance(entry, list):
            out.extend(cast("list[ResolveObject]", entry))
        else:
            out.append(entry)
    return out


def _verify_deblur(_s: ResolveSession, _p: DeblurParams, result: DeblurResult) -> Verification:
    return Verification(ok=bool(result.created_clip_ids), observed=result.model_dump())


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=DeblurParams,
    result=DeblurResult,
    studio_only=True,
    timeout_s=_SYNC_TIMEOUT_S,
    verify=_verify_deblur,
)
def remove_motion_blur(s: ResolveSession, p: DeblurParams) -> DeblurResult:
    """Render motion-deblurred copies of a clip or folder's clips (blocking, creates clips)."""
    obj, label = _target(s, p)
    s.caps.require(obj, "RemoveMotionBlur")
    options = p.options.model_dump(exclude_none=True) if p.options else {}
    raw: Any = obj.RemoveMotionBlur(options)
    if isinstance(raw, str):
        _check_ai(raw, "RemoveMotionBlur")
    created: list[ResolveObject] = (
        [cast("ResolveObject", raw)] if p.clip is not None and raw else _flatten(raw)
    )
    if not created:
        _check_ai(None, "RemoveMotionBlur")
    return DeblurResult(target=label, created_clip_ids=[str(c.GetUniqueId()) for c in created])


# -- speech ----------------------------------------------------------------------------------


class SpeechParams(_Params):
    text: str = Field(min_length=1, max_length=350, description="Text to synthesize (<= 350).")
    voice_model: str = Field(default="Female 1", description="e.g. 'Female 1', 'Custom Voice'.")
    custom_voice_file: str | None = Field(default=None, description="Needed for 'Custom Voice'.")
    speed: float | None = Field(default=None, ge=-10, le=10)
    variation: float | None = Field(default=None, ge=0, le=1)
    pitch: float | None = Field(default=None, ge=-2, le=2)
    generation_id: int | None = Field(default=None, ge=1)
    filename: SafeFileName | None = None
    add_to_timeline: bool = False
    audio_track: int | None = Field(default=None, ge=0, description="0 = new track.")


class SpeechResult(BaseModel):
    clip_id: str
    name: str


def _verify_speech(_s: ResolveSession, _p: SpeechParams, result: SpeechResult) -> Verification:
    return Verification(ok=bool(result.clip_id), observed=result.model_dump())


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SpeechParams,
    result=SpeechResult,
    studio_only=True,
    timeout_s=600.0,
    verify=_verify_speech,
)
def generate_speech(s: ResolveSession, p: SpeechParams) -> SpeechResult:
    """Generate speech audio from text into the Media Pool (needs AI Speech Generator)."""
    project = s.project
    s.caps.require(project, "GenerateSpeech")
    settings: dict[str, Any] = {"TextInput": p.text, "VoiceModel": p.voice_model}
    if p.custom_voice_file is not None:
        settings["CustomVoiceFile"] = str(
            check_path(p.custom_voice_file, runtime().allowed_roots, must_exist=True)
        )
    elif p.voice_model == "Custom Voice":
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            "voice_model 'Custom Voice' requires custom_voice_file.",
            retryable=False,
        )
    optional: dict[str, Any] = {
        "Speed": p.speed,
        "Variation": p.variation,
        "Pitch": p.pitch,
        "GenerationID": p.generation_id,
        "Filename": p.filename,
        "AudioTrack": p.audio_track,
    }
    settings.update({k: v for k, v in optional.items() if v is not None})
    if p.add_to_timeline:
        settings["AddToTimeline"] = True
    clip = project.GenerateSpeech(settings)
    _check_ai(clip, "GenerateSpeech")
    return SpeechResult(clip_id=str(clip.GetUniqueId()), name=str(clip.GetName()))


# -- transcription -------------------------------------------------------------------------------


class TranscriptionWord(BaseModel):
    start: str
    end: str
    text: str


class TranscriptionSegment(BaseModel):
    start: str
    end: str
    text: str
    speaker: str | None = None
    words: list[TranscriptionWord] | None = None


class GetTranscriptionParams(_Params):
    clip: ClipRef
    nested: bool = Field(default=False, description="Use the nested clip transcription.")
    offset: int = Field(default=0, ge=0, description="First segment (0-based).")
    limit: int = Field(default=50, ge=1, le=500)
    include_words: bool = Field(default=False, description="Include per-word timings.")


class TranscriptionPage(BaseModel):
    clip_id: str
    transcribed: bool
    language: str | None = None
    total_segments: int = 0
    offset: int = 0
    next_offset: int | None = None
    segments: list[TranscriptionSegment] = Field(default_factory=list[TranscriptionSegment])


def _transcription(clip: ResolveObject, nested: bool) -> dict[str, Any]:
    raw = clip.GetTranscription(True) if nested else clip.GetTranscription()
    return _as_dict(raw)


def _segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw: Any = data.get("segments") or []
    return [_as_dict(seg) for seg in cast("list[Any]", raw)]


@action(
    domain=DOMAIN,
    kind=Kind.READ,
    params=GetTranscriptionParams,
    result=TranscriptionPage,
    since=(21, 1, 0),
)
def get_transcription(s: ResolveSession, p: GetTranscriptionParams) -> TranscriptionPage:
    """Read a clip transcription (segments, optional words), paginated."""
    clip = resolve_clip(s, p.clip)
    s.caps.require(clip, "GetTranscription", since=(21, 1, 0))
    data = _transcription(clip, p.nested)
    segments = _segments(data)
    clip_id = str(clip.GetUniqueId())
    if not segments:
        return TranscriptionPage(clip_id=clip_id, transcribed=False)
    page = segments[p.offset : p.offset + p.limit]
    out: list[TranscriptionSegment] = []
    for seg in page:
        words: list[TranscriptionWord] | None = None
        if p.include_words:
            words = [
                TranscriptionWord(
                    start=str(w.get("start", "")),
                    end=str(w.get("end", "")),
                    text=str(w.get("text", "")),
                )
                for w in (_as_dict(x) for x in cast("list[Any]", seg.get("words") or []))
            ]
        speaker = seg.get("speaker")
        out.append(
            TranscriptionSegment(
                start=str(seg.get("start", "")),
                end=str(seg.get("end", "")),
                text=str(seg.get("text", "")),
                speaker=str(speaker) if speaker is not None else None,
                words=words,
            )
        )
    end = p.offset + len(page)
    return TranscriptionPage(
        clip_id=clip_id,
        transcribed=True,
        language=str(data["language"]) if data.get("language") is not None else None,
        total_segments=len(segments),
        offset=p.offset,
        next_offset=end if end < len(segments) else None,
        segments=out,
    )


class ClearTranscriptionParams(Target):
    nested: bool = Field(default=False, description="Clip only: clear the nested transcription.")


def _folder_clips(folder: ResolveObject) -> list[ResolveObject]:
    return [c for _, f in walk_folders(folder) for c in proxies(f.GetClipList())]


def _transcribed_state(s: ResolveSession, p: ClearTranscriptionParams) -> dict[str, int]:
    obj, _ = _target(s, p)
    clips = [obj] if p.clip is not None else _folder_clips(obj)
    return {
        str(c.GetUniqueId()): len(_segments(_transcription(c, p.nested)))
        for c in clips
        if s.caps.has(c, "GetTranscription")
    }


def _plan_clear_transcription(s: ResolveSession, p: ClearTranscriptionParams) -> PlanDraft:
    _, label = _target(s, p)
    state = _transcribed_state(s, p)
    return PlanDraft(
        summary=f"Clear the transcription of {label} ({len(state)} clip(s)).",
        effects=[f"Deletes {sum(state.values())} transcription segment(s); cannot be undone."],
        targets=[label],
        fingerprint=_fingerprint(state),
    )


def _verify_cleared(s: ResolveSession, p: ClearTranscriptionParams, _: Done) -> Verification:
    left = {k: v for k, v in _transcribed_state(s, p).items() if v}
    return Verification(ok=not left, observed={"clips_with_segments": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=ClearTranscriptionParams,
    result=Done,
    studio_only=True,
    plan=_plan_clear_transcription,
    verify=_verify_cleared,
)
def clear_transcription(s: ResolveSession, p: ClearTranscriptionParams) -> Done:
    """Clear the transcription of a clip or folder (irreversible)."""
    obj, label = _target(s, p)
    s.caps.require(obj, "ClearTranscription")
    raw = (
        obj.ClearTranscription(True)
        if p.clip is not None and p.nested
        else obj.ClearTranscription()
    )
    _check_ai(raw, "ClearTranscription")
    return Done(target=label)


ACTIONS = [
    transcribe_audio,
    create_subtitles_from_audio,
    detect_scene_cuts,
    smart_reframe,
    analyze_for_slate,
    analyze_for_intellisearch,
    perform_audio_classification,
    clear_audio_classification,
    remove_motion_blur,
    generate_speech,
    get_transcription,
    clear_transcription,
]
