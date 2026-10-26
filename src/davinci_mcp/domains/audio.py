# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Audio domain: voice isolation, audio mapping, normalization, Fairlight presets, ffmpeg tools.

Two families of actions live here:

* Resolve-bound actions (voice isolation, mapping, normalize, presets, item audio properties).
* External ffmpeg tools (``analyze_loudness``, ``detect_silence``, ``normalize_file``) that work
  on a file path (or a Media Pool clip's ``File Path``) and never need Resolve itself.
  ``normalize_file`` is a long-running job: ``launch`` spawns an ffmpeg subprocess and ``poll``
  advances a small two-pass state machine (loudnorm analysis, then encode) without blocking the
  Resolve executor.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import JobRequest, Kind, action
from davinci_mcp.api._generated.resolve_21_1 import DialogueLevelerMode, NormalizeAudioOptions
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import (
    ClipRef,
    ItemRef,
    TimelineRef,
    const,
    proxies,
    resolve_clip,
    resolve_item,
    resolve_timeline,
)
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "audio"
_FFMPEG_TIMEOUT_S = 300.0
_MAX_SILENCES = 500
_PCM_SUFFIXES = {".wav", ".aif", ".aiff"}


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- voice isolation ----------------------------------------------------------------


class VoiceTarget(_Params):
    item: ItemRef | None = Field(default=None, description="Timeline item (audio clip).")
    track: int | None = Field(
        default=None, ge=1, description="1-based audio track of the timeline."
    )
    timeline: TimelineRef | None = Field(default=None, description="Used with track.")

    @model_validator(mode="after")
    def _one(self) -> VoiceTarget:
        if (self.item is None) == (self.track is None):
            raise ValueError("use exactly one of item or track")
        return self


class SetVoiceIsolationParams(VoiceTarget):
    enabled: bool
    amount: int | None = Field(default=None, ge=0, le=100, description="Strength 0-100.")


class VoiceState(BaseModel):
    scope: str
    enabled: bool | None
    amount: int | None


def _voice_read(s: ResolveSession, p: VoiceTarget) -> VoiceState:
    if p.item is not None:
        obj = resolve_item(s, p.item)
        s.caps.require(obj, "GetVoiceIsolationState")
        raw: Any = obj.GetVoiceIsolationState()
        scope = "item"
    else:
        obj = resolve_timeline(s, p.timeline)
        s.caps.require(obj, "GetVoiceIsolationState")
        raw = obj.GetVoiceIsolationState(p.track)
        scope = f"track:{p.track}"
    state = _as_dict(raw)
    enabled = state.get("isEnabled")
    amount = state.get("amount")
    return VoiceState(
        scope=scope,
        enabled=bool(enabled) if enabled is not None else None,
        amount=int(amount) if amount is not None else None,
    )


@action(domain=DOMAIN, kind=Kind.READ, params=VoiceTarget, result=VoiceState)
def get_voice_isolation(s: ResolveSession, p: VoiceTarget) -> VoiceState:
    """Read voice isolation state of a timeline item or audio track."""
    return _voice_read(s, p)


def _verify_voice(s: ResolveSession, p: SetVoiceIsolationParams, _: VoiceState) -> Verification:
    now = _voice_read(s, p)
    ok = now.enabled == p.enabled and (p.amount is None or now.amount == p.amount)
    return Verification(ok=ok, observed=now.model_dump())


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetVoiceIsolationParams,
    result=VoiceState,
    verify=_verify_voice,
    idempotent=True,
)
def set_voice_isolation(s: ResolveSession, p: SetVoiceIsolationParams) -> VoiceState:
    """Enable/disable voice isolation on an item or audio track (active timeline only)."""
    current = _voice_read(s, p)
    state: dict[str, Any] = {
        "isEnabled": p.enabled,
        "amount": p.amount if p.amount is not None else (current.amount or 100),
    }
    if p.item is not None:
        ok = resolve_item(s, p.item).SetVoiceIsolationState(state)
    else:
        ok = resolve_timeline(s, p.timeline).SetVoiceIsolationState(p.track, state)
    if not ok:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "SetVoiceIsolationState returned False.",
            hint="Voice isolation only applies to the active timeline, on an audio item/track, "
            "and requires Resolve Studio.",
            retryable=False,
        )
    return VoiceState(scope=current.scope, enabled=p.enabled, amount=state["amount"])


# -- audio mapping ----------------------------------------------------------------------


class MappingTarget(_Params):
    clip: ClipRef | None = Field(default=None, description="Media Pool clip (GetAudioMapping).")
    item: ItemRef | None = Field(
        default=None, description="Timeline item (source audio channel mapping, 21.1)."
    )

    @model_validator(mode="after")
    def _one(self) -> MappingTarget:
        if (self.clip is None) == (self.item is None):
            raise ValueError("use exactly one of clip or item")
        return self


class SetMappingParams(MappingTarget):
    mapping: dict[str, Any] | list[Any] = Field(
        description="Audio mapping as JSON (same structure as returned by get_audio_mapping)."
    )


class AudioMapping(BaseModel):
    scope: str
    raw: str
    mapping: Any | None = Field(default=None, description="Parsed JSON when valid.")


def _as_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast("dict[Any, Any]", value).items()}


def _names(value: object) -> list[str]:
    return [str(x) for x in proxies(value)]


def _parse_json(raw: str) -> Any | None:  # noqa: ANN401
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _mapping_read(s: ResolveSession, p: MappingTarget) -> AudioMapping:
    if p.clip is not None:
        obj = resolve_clip(s, p.clip)
        s.caps.require(obj, "GetAudioMapping")
        raw = obj.GetAudioMapping()
        scope = "clip"
    else:
        assert p.item is not None
        obj = resolve_item(s, p.item)
        s.caps.require(obj, "GetSourceAudioChannelMapping", since=(21, 1, 0))
        raw = obj.GetSourceAudioChannelMapping()
        scope = "item"
    text = raw if isinstance(raw, str) else ""
    return AudioMapping(scope=scope, raw=text, mapping=_parse_json(text) if text else None)


@action(domain=DOMAIN, kind=Kind.READ, params=MappingTarget, result=AudioMapping)
def get_audio_mapping(s: ResolveSession, p: MappingTarget) -> AudioMapping:
    """Read the audio mapping of a clip or the source channel mapping of an item."""
    return _mapping_read(s, p)


def _verify_mapping(s: ResolveSession, p: SetMappingParams, _: AudioMapping) -> Verification:
    now = _mapping_read(s, p)
    return Verification(
        ok=now.mapping == p.mapping,
        observed={"mapping": now.mapping, "raw": now.raw},
        note="Resolve may normalize the mapping; compare observed with the requested value.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetMappingParams,
    result=AudioMapping,
    since=(21, 1, 0),
    verify=_verify_mapping,
    idempotent=True,
)
def set_audio_mapping(s: ResolveSession, p: SetMappingParams) -> AudioMapping:
    """Write the audio mapping of a clip, or the source channel mapping of an item (21.1)."""
    payload = json.dumps(p.mapping)
    if p.clip is not None:
        obj = resolve_clip(s, p.clip)
        s.caps.require(obj, "SetAudioMapping", since=(21, 1, 0))
        ok = obj.SetAudioMapping(payload)
        scope = "clip"
    else:
        assert p.item is not None
        obj = resolve_item(s, p.item)
        s.caps.require(obj, "SetSourceAudioChannelMapping", since=(21, 1, 0))
        ok = obj.SetSourceAudioChannelMapping(payload)
        scope = "item"
    if not ok:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Setting the audio mapping returned False.",
            hint="Read the current mapping first and edit it; the JSON structure must match.",
            retryable=False,
        )
    return AudioMapping(scope=scope, raw=payload, mapping=p.mapping)


# -- normalize ---------------------------------------------------------------------------


class TimelineOnly(_Params):
    timeline: TimelineRef | None = None


class NormalizeModes(BaseModel):
    modes: list[str]


@action(
    domain=DOMAIN,
    kind=Kind.READ,
    params=TimelineOnly,
    result=NormalizeModes,
    since=(21, 1, 0),
)
def list_normalize_modes(s: ResolveSession, p: TimelineOnly) -> NormalizeModes:
    """List valid normalizationMode names for normalize_audio_level."""
    timeline = resolve_timeline(s, p.timeline)
    s.caps.require(timeline, "GetNormalizeAudioModes", since=(21, 1, 0))
    return NormalizeModes(modes=_names(timeline.GetNormalizeAudioModes()))


class NormalizeParams(_Params):
    items: list[ItemRef] = Field(min_length=1, description="Audio timeline items of the timeline.")
    options: NormalizeAudioOptions | None = None


class NormalizeResult(BaseModel):
    normalized: int
    options: dict[str, Any]


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=NormalizeParams,
    result=NormalizeResult,
    since=(21, 1, 0),
    verify_reason="Resolve has no getter for the applied gain; compare get_audio_properties "
    "(AudioVolume) before and after.",
)
def normalize_audio_level(s: ResolveSession, p: NormalizeParams) -> NormalizeResult:
    """Normalize the audio level of timeline items (21.1, Studio)."""
    timeline = s.timeline
    s.caps.require(timeline, "NormalizeAudioLevel", since=(21, 1, 0))
    options: dict[str, Any] = p.options.model_dump(exclude_none=True) if p.options else {}
    mode = options.get("normalizationMode")
    if mode is not None:
        valid = _names(timeline.GetNormalizeAudioModes())
        if mode not in valid:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS,
                f"Unknown normalizationMode {mode!r}.",
                hint=f"Valid modes: {valid}",
                retryable=False,
            )
    if "setLevelMode" in options:
        options["setLevelMode"] = const(s, options["setLevelMode"])
    items = [resolve_item(s, ref) for ref in p.items]
    ok = (
        timeline.NormalizeAudioLevel(items, options)
        if options
        else timeline.NormalizeAudioLevel(items)
    )
    if not ok:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Timeline.NormalizeAudioLevel returned False.",
            hint="Items must belong to the current timeline and be audio items.",
            retryable=False,
        )
    return NormalizeResult(
        normalized=len(items), options=p.options.model_dump(exclude_none=True) if p.options else {}
    )


# -- Fairlight presets ---------------------------------------------------------------------


class PresetList(BaseModel):
    presets: list[str]


class ApplyPresetParams(_Params):
    name: str


class PresetApplied(BaseModel):
    name: str
    timeline: str


@action(domain=DOMAIN, kind=Kind.READ, result=PresetList)
def list_fairlight_presets(s: ResolveSession, _: BaseModel) -> PresetList:
    """List Fairlight preset names."""
    s.caps.require(s.resolve, "GetFairlightPresets")
    return PresetList(presets=_names(s.resolve.GetFairlightPresets()))


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ApplyPresetParams,
    result=PresetApplied,
    verify_reason="Resolve has no getter for the applied Fairlight preset.",
)
def apply_fairlight_preset(s: ResolveSession, p: ApplyPresetParams) -> PresetApplied:
    """Apply a Fairlight preset to the current timeline."""
    s.caps.require(s.resolve, "GetFairlightPresets")
    known = _names(s.resolve.GetFairlightPresets())
    if p.name not in known:
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"No Fairlight preset named {p.name!r}.",
            hint=f"Available: {known}",
            next_call="audio_read(op={'action': 'list_fairlight_presets'})",
            retryable=False,
        )
    project = s.project
    s.caps.require(project, "ApplyFairlightPresetToCurrentTimeline")
    timeline = s.timeline
    if not project.ApplyFairlightPresetToCurrentTimeline(p.name):
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "ApplyFairlightPresetToCurrentTimeline returned False.",
            retryable=False,
        )
    return PresetApplied(name=p.name, timeline=str(timeline.GetName()))


# -- item audio properties (dialogue leveler, volume, pan...) ------------------------------


class AudioProps(_Params):
    """Subset of TimelineItem properties related to audio. Dialogue leveler applies to the
    active timeline only."""

    AudioVolume: float | None = Field(default=None, ge=-100, le=30, description="dB")
    AudioPan: float | None = Field(default=None, ge=-100, le=100)
    AudioPitchSemiTones: float | None = Field(default=None, ge=-24, le=24)
    AudioPitchCents: float | None = Field(default=None, ge=-100, le=100)
    AudioVoiceIsolationEnabled: bool | None = None
    AudioVoiceIsolationAmount: int | None = Field(default=None, ge=0, le=100)
    AudioDialogueLevelerEnabled: bool | None = None
    AudioDialogueLevelerMode: DialogueLevelerMode | None = Field(
        default=None, description="resolve.DIALOGUE_LEVELER_MODE_* constant name."
    )
    AudioDialogueLevelerReduceLoudDialogue: bool | None = None
    AudioDialogueLevelerLiftSoftDialogue: bool | None = None
    AudioDialogueLevelerBackgroundReduction: bool | None = None
    AudioDialogueLevelerOutputGain: float | None = None


class ItemOnly(_Params):
    item: ItemRef


class SetAudioPropsParams(_Params):
    item: ItemRef
    properties: AudioProps


class AudioPropsResult(BaseModel):
    item_id: str
    properties: dict[str, Any]


def _read_props(s: ResolveSession, item: ResolveObject) -> dict[str, Any]:
    if s.caps.has(item, "GetProperties"):
        raw: Any = item.GetProperties()
    else:
        raw = item.GetProperty()
    props = _as_dict(raw)
    return {k: v for k, v in props.items() if str(k).startswith("Audio")}


@action(domain=DOMAIN, kind=Kind.READ, params=ItemOnly, result=AudioPropsResult)
def get_audio_properties(s: ResolveSession, p: ItemOnly) -> AudioPropsResult:
    """Read audio properties of a timeline item (volume, pan, voice isolation, leveler)."""
    item = resolve_item(s, p.item)
    return AudioPropsResult(item_id=item.GetUniqueId(), properties=_read_props(s, item))


def _wanted(s: ResolveSession, p: SetAudioPropsParams) -> dict[str, Any]:
    wanted: dict[str, Any] = p.properties.model_dump(exclude_none=True)
    if "AudioDialogueLevelerMode" in wanted:
        wanted["AudioDialogueLevelerMode"] = const(s, wanted["AudioDialogueLevelerMode"])
    return wanted


def _verify_props(s: ResolveSession, p: SetAudioPropsParams, _: AudioPropsResult) -> Verification:
    now = _read_props(s, resolve_item(s, p.item))
    wanted = _wanted(s, p)
    diff = {k: {"wanted": v, "observed": now.get(k)} for k, v in wanted.items() if now.get(k) != v}
    return Verification(ok=not diff, observed={"mismatch": diff} if diff else now)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetAudioPropsParams,
    result=AudioPropsResult,
    verify=_verify_props,
    idempotent=True,
)
def set_audio_properties(s: ResolveSession, p: SetAudioPropsParams) -> AudioPropsResult:
    """Set audio properties of a timeline item (volume, pan, voice isolation, leveler)."""
    item = resolve_item(s, p.item)
    wanted = _wanted(s, p)
    if not wanted:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "No property given.", retryable=False)
    if s.caps.has(item, "SetProperties"):
        ok = item.SetProperties(wanted)
    else:
        ok = all(item.SetProperty(k, v) for k, v in wanted.items())
    if not ok:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Setting the audio properties returned False.",
            hint="Dialogue leveler and voice isolation only apply to the active timeline.",
            retryable=False,
        )
    return AudioPropsResult(item_id=item.GetUniqueId(), properties=wanted)


# -- ffmpeg helpers ---------------------------------------------------------------------


class MediaSource(_Params):
    path: str | None = Field(default=None, description="Media file inside the allowed roots.")
    clip: ClipRef | None = Field(default=None, description="Media Pool clip (its File Path).")

    @model_validator(mode="after")
    def _one(self) -> MediaSource:
        if (self.path is None) == (self.clip is None):
            raise ValueError("use exactly one of path or clip")
        return self


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found is None:
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "ffmpeg is not installed.",
            hint="brew install ffmpeg",
            retryable=False,
        )
    return found


def _source_path(s: ResolveSession, src: MediaSource) -> Path:
    raw = src.path
    if src.clip is not None:
        raw = resolve_clip(s, src.clip).GetClipProperty("File Path")
        if not raw:
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                "The clip has no File Path (generator or compound).",
                retryable=False,
            )
    assert raw is not None
    return check_path(raw, runtime().allowed_roots, must_exist=True)


def _run_ffmpeg(args: list[str], timeout_s: float = _FFMPEG_TIMEOUT_S) -> str:
    """Run ffmpeg, return stderr text. Never reads stdin (it would eat the stdio transport)."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [_ffmpeg(), "-hide_banner", "-nostdin", "-nostats", *args],
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise DavinciError(
            ErrorCode.TIMEOUT, f"ffmpeg exceeded {timeout_s:.0f}s.", retryable=False
        ) from None
    text = done.stderr.decode(errors="replace")
    if done.returncode != 0:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            f"ffmpeg failed (exit {done.returncode}).",
            details={"stderr_tail": text[-600:]},
            retryable=False,
        )
    return text


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _loudnorm_json(text: str) -> dict[str, Any]:
    blocks = re.findall(r"\{[^{}]*\}", text)
    for block in reversed(blocks):
        parsed = _parse_json(block)
        if isinstance(parsed, dict) and "input_i" in parsed:
            return parsed  # pyright: ignore[reportUnknownVariableType]
    raise DavinciError(
        ErrorCode.API_CALL_FAILED,
        "ffmpeg loudnorm produced no measurement (does the file have an audio stream?).",
        retryable=False,
    )


class LoudnessParams(MediaSource):
    pass


class Loudness(BaseModel):
    path: str
    integrated_lufs: float | None = Field(description="None when the file is silent.")
    true_peak_dbtp: float | None
    lra: float | None
    threshold_lufs: float | None


@action(
    domain=DOMAIN,
    kind=Kind.READ,
    params=LoudnessParams,
    result=Loudness,
    timeout_s=_FFMPEG_TIMEOUT_S + 30,
)
def analyze_loudness(s: ResolveSession, p: LoudnessParams) -> Loudness:
    """Measure integrated LUFS, true peak and LRA of a file with ffmpeg (no Resolve needed)."""
    path = _source_path(s, p)
    text = _run_ffmpeg(
        ["-i", str(path), "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"]
    )
    data = _loudnorm_json(text)
    return Loudness(
        path=str(path),
        integrated_lufs=_finite(data.get("input_i")),
        true_peak_dbtp=_finite(data.get("input_tp")),
        lra=_finite(data.get("input_lra")),
        threshold_lufs=_finite(data.get("input_thresh")),
    )


class SilenceParams(MediaSource):
    noise_db: float = Field(default=-40.0, le=0, ge=-90, description="Silence threshold in dB.")
    min_duration_s: float = Field(default=0.5, gt=0, le=3600)


class SilenceInterval(BaseModel):
    start_s: float
    end_s: float | None = Field(description="None when silence runs to the end of the file.")
    duration_s: float | None


class SilenceReport(BaseModel):
    path: str
    intervals: list[SilenceInterval]
    truncated: bool = False


_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")


@action(
    domain=DOMAIN,
    kind=Kind.READ,
    params=SilenceParams,
    result=SilenceReport,
    timeout_s=_FFMPEG_TIMEOUT_S + 30,
)
def detect_silence(s: ResolveSession, p: SilenceParams) -> SilenceReport:
    """Find silent intervals of a file with ffmpeg silencedetect (no Resolve needed)."""
    path = _source_path(s, p)
    text = _run_ffmpeg(
        [
            "-i", str(path), "-vn", "-af",
            f"silencedetect=noise={p.noise_db}dB:d={p.min_duration_s}", "-f", "null", "-",
        ]
    )  # fmt: skip
    intervals: list[SilenceInterval] = []
    start: float | None = None
    for line in text.splitlines():
        if (m := _START.search(line)) is not None:
            start = max(0.0, float(m.group(1)))
        elif (m := _END.search(line)) is not None and start is not None:
            intervals.append(
                SilenceInterval(
                    start_s=start, end_s=float(m.group(1)), duration_s=float(m.group(2))
                )
            )
            start = None
    if start is not None:
        intervals.append(SilenceInterval(start_s=start, end_s=None, duration_s=None))
    truncated = len(intervals) > _MAX_SILENCES
    return SilenceReport(path=str(path), intervals=intervals[:_MAX_SILENCES], truncated=truncated)


# -- normalize_file (two-pass loudnorm job) --------------------------------------------------


class NormalizeFileParams(MediaSource):
    target_lufs: float = Field(default=-16.0, ge=-70, le=-5, description="Integrated loudness.")
    true_peak_dbtp: float = Field(default=-1.5, ge=-9, le=0)
    lra: float = Field(default=11.0, ge=1, le=50)
    sample_rate: int = Field(default=48000, ge=8000, le=192000)
    output: str | None = Field(
        default=None,
        description="New file inside the allowed roots (never overwritten). "
        "Default: '<name>.norm<ext>' next to the source.",
    )
    import_to_pool: bool = Field(default=False, description="Import the result in the Media Pool.")


def _output_path(src: Path, requested: str | None) -> Path:
    roots = runtime().allowed_roots
    if requested is not None:
        dst = check_path(requested, roots)
        if dst.exists():
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                f"Output already exists: {dst}",
                retryable=False,
                hint="normalize_file never overwrites; choose another name.",
            )
    else:
        dst = check_path(src.with_name(f"{src.stem}.norm{src.suffix}"), roots)
        n = 2
        while dst.exists():
            dst = check_path(src.with_name(f"{src.stem}.norm{n}{src.suffix}"), roots)
            n += 1
    if dst == src:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "Output equals source.", retryable=False)
    if not dst.parent.is_dir():
        raise DavinciError(
            ErrorCode.NOT_FOUND, f"Output folder does not exist: {dst.parent}", retryable=False
        )
    return dst


class _NormalizeJob:
    """Two-pass loudnorm run as background ffmpeg processes advanced by ``poll``."""

    def __init__(self, src: Path, dst: Path, p: NormalizeFileParams) -> None:
        self.src, self.dst, self.p = src, dst, p
        self.stage = "analyze"
        self.proc: subprocess.Popen[bytes] | None = None
        self.log: IO[bytes] | None = None
        self.error: str | None = None
        self.imported: list[str] = []

    def _spawn(self, args: list[str]) -> None:
        self.log = tempfile.TemporaryFile()  # noqa: SIM115 - closed in _close
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [_ffmpeg(), "-hide_banner", "-nostdin", "-nostats", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self.log,
        )

    def _text(self) -> str:
        if self.log is None:
            return ""
        self.log.seek(0)
        return self.log.read().decode(errors="replace")

    def _close(self) -> None:
        if self.log is not None:
            self.log.close()
            self.log = None

    def start(self) -> bool:
        p = self.p
        flt = f"loudnorm=I={p.target_lufs}:TP={p.true_peak_dbtp}:LRA={p.lra}:print_format=json"
        self._spawn(["-i", str(self.src), "-vn", "-af", flt, "-f", "null", "-"])
        return True

    def _start_encode(self, m: dict[str, Any]) -> None:
        p = self.p
        flt = (
            f"loudnorm=I={p.target_lufs}:TP={p.true_peak_dbtp}:LRA={p.lra}"
            f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
            f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
            f":offset={m['target_offset']}:linear=true"
        )
        codec = ["-c:a", "pcm_s24le"] if self.dst.suffix.lower() in _PCM_SUFFIXES else []
        self._spawn(
            [
                "-n",
                "-i",
                str(self.src),
                "-map",
                "0:v?",
                "-map",
                "0:a:0",
                "-c:v",
                "copy",
                "-af",
                flt,
                "-ar",
                str(p.sample_rate),
                *codec,
                str(self.dst),
            ]
        )

    def _fail(self, message: str) -> dict[str, Any]:
        self.stage = "failed"
        self.error = message
        self._close()
        return {"Error": message, "stage": self.stage}

    def poll(self, s: ResolveSession) -> Mapping[str, Any]:  # noqa: PLR0911
        if self.stage == "failed":
            return {"Error": self.error, "stage": self.stage}
        if self.stage == "done":
            return {"CompletionPercentage": 100, "done": True, "output": str(self.dst)}
        assert self.proc is not None
        code = self.proc.poll()
        if code is None:
            return {"CompletionPercentage": 25 if self.stage == "analyze" else 75}
        if code != 0:
            tail = self._text()[-400:]
            return self._fail(f"ffmpeg {self.stage} pass failed (exit {code}): {tail}")
        if self.stage == "analyze":
            try:
                measured = _loudnorm_json(self._text())
            except DavinciError as exc:
                return self._fail(exc.message)
            self._close()
            if any(_finite(measured.get(k)) is None for k in ("input_i", "input_tp", "input_lra")):
                return self._fail("Source is silent or unmeasurable; nothing to normalize.")
            self.stage = "encode"
            self._start_encode(measured)
            return {"CompletionPercentage": 50}
        self._close()
        self.stage = "done"
        if self.p.import_to_pool:
            pool = s.project.GetMediaPool()
            imported: list[Any] = proxies(pool.ImportMedia([str(self.dst)]))
            self.imported = [str(i.GetUniqueId()) for i in imported]
        return {
            "CompletionPercentage": 100,
            "done": True,
            "output": str(self.dst),
            "imported_clip_ids": self.imported,
        }

    def cancel(self, _: ResolveSession) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        self._close()
        if self.stage != "done" and self.dst.exists():
            self.dst.unlink()  # our own partial output (ffmpeg -n never opens an existing file)
        self.stage = "failed"
        return True


@action(domain=DOMAIN, kind=Kind.WRITE, params=NormalizeFileParams, long_running=True)
def normalize_file(s: ResolveSession, p: NormalizeFileParams) -> JobRequest:
    """Two-pass loudnorm of a file into a new file (job); optional Media Pool import."""
    src = _source_path(s, p)
    _ffmpeg()
    job = _NormalizeJob(src, _output_path(src, p.output), p)
    return JobRequest(
        kind="audio.normalize_file",
        label=f"Normalize {src.name} to {p.target_lufs} LUFS",
        launch=lambda _s: job.start(),
        poll=job.poll,
        cancel=job.cancel,
    )


ACTIONS = [
    get_voice_isolation,
    set_voice_isolation,
    get_audio_mapping,
    set_audio_mapping,
    list_normalize_modes,
    normalize_audio_level,
    list_fairlight_presets,
    apply_fairlight_preset,
    get_audio_properties,
    set_audio_properties,
    analyze_loudness,
    detect_silence,
    normalize_file,
]
