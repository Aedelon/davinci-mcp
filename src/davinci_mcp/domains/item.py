# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Timeline item domain: details, properties, speed, fades, takes, markers, multicam.

Color grading, Fusion, AI/reframe and audio routing live in their own domains.
Batch actions take ``items: list[ItemRef]`` and report one outcome per item; every
outcome is read back, so a ``True`` returned by Resolve without effect is flagged.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.api._generated.resolve_21_1 import (
    ClipColor,
    CompositeMode,
    DynamicZoomEase,
    FadeInfo,
    FlagColor,
    FlattenMulticamGrade,
    MarkerColor,
    MotionEstimation,
    OutputBlanking,
    ResizeFilter,
    RetimeProcess,
    Scaling,
    SmartSwitchSettings,
    SpeedOptions,
    TimelineItemProperties,
    TransitionOptions,
)
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import (
    ClipRef,
    ItemRef,
    const,
    iter_items,
    proxies,
    resolve_clip,
    resolve_item,
    resolve_timeline,
)

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "item"
V21_1 = (21, 1, 0)

# Properties whose value is a Resolve constant: clients use the constant NAME.
_CONST_PROPS: dict[str, tuple[str, ...]] = {
    "DynamicZoomEase": get_args(DynamicZoomEase),
    "CompositeMode": get_args(CompositeMode),
    "RetimeProcess": get_args(RetimeProcess),
    "MotionEstimation": get_args(MotionEstimation),
    "Scaling": get_args(Scaling),
    "ResizeFilter": get_args(ResizeFilter),
}
_SMART_SWITCH_CONSTS = ("analysisMode", "wideAngleFrequency", "quality")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Batch(_Params):
    items: list[ItemRef] = Field(min_length=1, description="Target timeline items.")


class _One(_Params):
    item: ItemRef


# -- result models ------------------------------------------------------------------


class ItemOutcome(BaseModel):
    id: str
    name: str
    ok: bool
    observed: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class BatchResult(BaseModel):
    results: list[ItemOutcome]
    ok_count: int
    failed_count: int


class LinkedItem(BaseModel):
    id: str
    name: str


class ItemDetails(BaseModel):
    id: str
    name: str
    type: str | None = Field(default=None, description="video|audio|generator|transition (21.1).")
    track_type: str | None = None
    track_index: int | None = None
    start: float
    end: float
    duration: float
    left_offset: float | None
    right_offset: float | None
    source_start_frame: int | None
    source_end_frame: int | None
    source_start_time: float | None
    source_end_time: float | None
    enabled: bool | None
    clip_color: str
    flags: list[str]
    linked_items: list[LinkedItem]
    media_pool_item_id: str | None


class PropertiesResult(BaseModel):
    id: str
    properties: dict[str, Any] = Field(description="Constant-valued props use constant NAMES.")


class SpeedResult(BaseModel):
    id: str
    speed: dict[str, Any]


class FadesResult(BaseModel):
    id: str
    fades: dict[str, Any]


class TakeEntry(BaseModel):
    index: int = Field(description="1-based take index.")
    start_frame: int | None
    end_frame: int | None
    media_pool_item_id: str | None
    media_pool_item_name: str | None


class TakesResult(BaseModel):
    id: str
    count: int
    selected_index: int = Field(description="0 when the item is not a take selector.")
    takes: list[TakeEntry]
    takes_before: int | None = None


class ItemMarker(BaseModel):
    frame: int
    color: str
    name: str
    note: str
    duration: int
    custom_data: str


class ItemMarkers(BaseModel):
    id: str
    markers: list[ItemMarker]


class BlankingResult(BaseModel):
    id: str
    use_timeline_blanking: bool | None
    blanking: dict[str, Any] = Field(description="Empty when the timeline blanking is used.")


class TransitionResult(BaseModel):
    host_id: str
    transition_id: str
    name: str


class NameResult(BaseModel):
    id: str
    name: str


class FlattenResult(BaseModel):
    id: str
    grade: str


class SmartSwitchResult(BaseModel):
    id: str
    settings: dict[str, Any]


# -- helpers --------------------------------------------------------------------------


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _label(item: ResolveObject) -> tuple[str, str]:
    return str(item.GetUniqueId()), str(item.GetName())


def _same(a: object, b: object) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), abs_tol=1e-4)
    return a == b


def _failed(what: str) -> DavinciError:
    return DavinciError(ErrorCode.API_CALL_FAILED, f"{what} returned False.", retryable=False)


def _resolve_all(s: ResolveSession, refs: list[ItemRef]) -> list[ResolveObject]:
    return [resolve_item(s, r) for r in refs]


type BatchFn = Callable[[ResolveObject], tuple[bool, dict[str, Any], str | None]]


def _run_batch(items: list[ResolveObject], fn: BatchFn) -> BatchResult:
    outcomes: list[ItemOutcome] = []
    for item in items:
        uid, name = _label(item)
        try:
            ok, observed, error = fn(item)
        except DavinciError as exc:
            ok, observed, error = False, {}, exc.message
        outcomes.append(ItemOutcome(id=uid, name=name, ok=ok, observed=observed, error=error))
    good = sum(o.ok for o in outcomes)
    return BatchResult(results=outcomes, ok_count=good, failed_count=len(outcomes) - good)


def _verify_batch(_: ResolveSession, __: object, result: BatchResult) -> Verification:
    bad = [o.id for o in result.results if not o.ok]
    return Verification(
        ok=not bad,
        observed={o.id: o.observed for o in result.results},
        note=f"Read-back mismatch or failure on items: {bad}" if bad else None,
    )


def _require_any_success(result: BatchResult, what: str) -> BatchResult:
    if result.ok_count == 0:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            f"{what} failed on every item.",
            details={"results": [o.model_dump() for o in result.results]},
            retryable=False,
        )
    return result


def _const_name(s: ResolveSession, prop: str, value: object) -> object:
    for name in _CONST_PROPS[prop]:
        if getattr(s.resolve, name, None) == value:
            return name
    return value


def _read_props(s: ResolveSession, item: ResolveObject) -> dict[str, Any]:
    raw = item.GetProperties() if s.caps.has(item, "GetProperties") else item.GetProperty()
    props: dict[str, Any] = dict(raw or {})
    for key in _CONST_PROPS:
        if key in props:
            props[key] = _const_name(s, key, props[key])
    return props


def _track_info(item: ResolveObject) -> tuple[str | None, int | None]:
    info = item.GetTrackTypeAndIndex()
    if info and len(info) == 2:  # noqa: PLR2004
        return str(info[0]), int(info[1])
    return None, None


def _item_markers(item: ResolveObject) -> list[ItemMarker]:
    raw: dict[Any, Any] = item.GetMarkers() or {}
    return sorted(
        (
            ItemMarker(
                frame=int(frame),
                color=str(info.get("color", "")),
                name=str(info.get("name", "")),
                note=str(info.get("note", "")),
                duration=int(info.get("duration", 1)),
                custom_data=str(info.get("customData", "")),
            )
            for frame, info in raw.items()
        ),
        key=lambda m: m.frame,
    )


def _takes(item: ResolveObject) -> TakesResult:
    count = int(item.GetTakesCount() or 0)
    entries: list[TakeEntry] = []
    for idx in range(1, count + 1):
        info: dict[str, Any] = item.GetTakeByIndex(idx) or {}
        mpi = info.get("mediaPoolItem")
        entries.append(
            TakeEntry(
                index=idx,
                start_frame=info.get("startFrame"),
                end_frame=info.get("endFrame"),
                media_pool_item_id=str(mpi.GetUniqueId()) if mpi is not None else None,
                media_pool_item_name=str(mpi.GetName()) if mpi is not None else None,
            )
        )
    return TakesResult(
        id=str(item.GetUniqueId()),
        count=count,
        selected_index=int(item.GetSelectedTakeIndex() or 0),
        takes=entries,
    )


def _opt(item: ResolveObject, method: str, *args: object) -> Any:  # noqa: ANN401
    """Call an optional getter, None when the method is missing."""
    fn = getattr(item, method, None)
    return fn(*args) if callable(fn) else None


# -- reads ----------------------------------------------------------------------------


@action(domain=DOMAIN, kind=Kind.READ, params=_One, result=ItemDetails)
def get_item(s: ResolveSession, p: _One) -> ItemDetails:
    """Full details of a timeline item (timing, source range, track, flags, links)."""
    item = resolve_item(s, p.item)
    uid, name = _label(item)
    track_type, track_index = _track_info(item)
    mpi = item.GetMediaPoolItem()
    enabled = _opt(item, "GetClipEnabled")
    return ItemDetails(
        id=uid,
        name=name,
        type=_opt(item, "GetType") if s.caps.has(item, "GetType") else None,
        track_type=track_type,
        track_index=track_index,
        start=float(item.GetStart()),
        end=float(item.GetEnd()),
        duration=float(item.GetDuration()),
        left_offset=_opt(item, "GetLeftOffset"),
        right_offset=_opt(item, "GetRightOffset"),
        source_start_frame=_opt(item, "GetSourceStartFrame"),
        source_end_frame=_opt(item, "GetSourceEndFrame"),
        source_start_time=_opt(item, "GetSourceStartTime"),
        source_end_time=_opt(item, "GetSourceEndTime"),
        enabled=None if enabled is None else bool(enabled),
        clip_color=str(item.GetClipColor() or ""),
        flags=[str(f) for f in proxies(item.GetFlagList())],
        linked_items=[
            LinkedItem(id=str(li.GetUniqueId()), name=str(li.GetName()))
            for li in proxies(item.GetLinkedItems())
        ],
        media_pool_item_id=str(mpi.GetUniqueId()) if mpi is not None else None,
    )


@action(domain=DOMAIN, kind=Kind.READ, params=_One, result=PropertiesResult)
def get_properties(s: ResolveSession, p: _One) -> PropertiesResult:
    """Read all item properties (transform, crop, composite, retime, audio...)."""
    item = resolve_item(s, p.item)
    return PropertiesResult(id=str(item.GetUniqueId()), properties=_read_props(s, item))


@action(domain=DOMAIN, kind=Kind.READ, params=_One, result=SpeedResult, since=V21_1)
def get_speed(s: ResolveSession, p: _One) -> SpeedResult:
    """Read clip speed options (Percentage, PitchCorrection...)."""
    item = resolve_item(s, p.item)
    return SpeedResult(id=str(item.GetUniqueId()), speed=dict(item.GetSpeed() or {}))


@action(domain=DOMAIN, kind=Kind.READ, params=_One, result=FadesResult, since=V21_1)
def get_fades(s: ResolveSession, p: _One) -> FadesResult:
    """Read fade-in / fade-out durations (frames)."""
    item = resolve_item(s, p.item)
    return FadesResult(id=str(item.GetUniqueId()), fades=dict(item.GetFades() or {}))


@action(domain=DOMAIN, kind=Kind.READ, params=_One, result=TakesResult)
def get_takes(s: ResolveSession, p: _One) -> TakesResult:
    """List the takes of an item (count, selected index, take info)."""
    return _takes(resolve_item(s, p.item))


@action(domain=DOMAIN, kind=Kind.READ, params=_One, result=ItemMarkers)
def list_item_markers(s: ResolveSession, p: _One) -> ItemMarkers:
    """List the markers attached to a timeline item."""
    item = resolve_item(s, p.item)
    return ItemMarkers(id=str(item.GetUniqueId()), markers=_item_markers(item))


def _blanking(item: ResolveObject) -> BlankingResult:
    return BlankingResult(
        id=str(item.GetUniqueId()),
        use_timeline_blanking=_opt(item, "GetUseTimelineForOutputBlanking"),
        blanking=dict(item.GetOutputBlanking() or {}),
    )


@action(domain=DOMAIN, kind=Kind.READ, params=_One, result=BlankingResult, since=V21_1)
def get_output_blanking(s: ResolveSession, p: _One) -> BlankingResult:
    """Read the output blanking of an item and whether the timeline one is used."""
    return _blanking(resolve_item(s, p.item))


# -- properties -----------------------------------------------------------------------


class SetPropertiesParams(_Batch):
    properties: TimelineItemProperties = Field(
        description="Properties to set; constant-valued ones (CompositeMode, Scaling, "
        "RetimeProcess...) are given as constant names, e.g. 'COMPOSITE_ADD'."
    )


def _encode_props(s: ResolveSession, props: TimelineItemProperties) -> dict[str, Any]:
    data: dict[str, Any] = props.model_dump(by_alias=True, exclude_none=True)
    if not data:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "No property given.", retryable=False)
    for key in _CONST_PROPS:
        if key in data:
            data[key] = const(s, str(data[key]))
    return data


def _apply_props(
    s: ResolveSession, item: ResolveObject, data: dict[str, Any]
) -> tuple[bool, dict[str, Any], str | None]:
    if s.caps.has(item, "SetProperties"):
        called = bool(item.SetProperties(data))
    else:
        called = all(bool(item.SetProperty(k, v)) for k, v in data.items())
    raw: dict[str, Any] = dict(
        (item.GetProperties() if s.caps.has(item, "GetProperties") else item.GetProperty()) or {}
    )
    mismatched = [k for k, v in data.items() if not _same(raw.get(k), v)]
    observed = {k: _const_name(s, k, raw.get(k)) if k in _CONST_PROPS else raw.get(k) for k in data}
    if mismatched:
        return False, observed, f"Read-back differs for {mismatched} (call returned {called})."
    return called, observed, None if called else "Resolve returned False."


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetPropertiesParams,
    result=BatchResult,
    verify=_verify_batch,
)
def set_properties(s: ResolveSession, p: SetPropertiesParams) -> BatchResult:
    """Set properties on one or many items; each key is read back per item."""
    data = _encode_props(s, p.properties)
    return _run_batch(_resolve_all(s, p.items), lambda it: _apply_props(s, it, data))


class CopyPropertiesParams(_Params):
    source: ItemRef
    targets: list[ItemRef] = Field(min_length=1)
    keys: list[str] | None = Field(
        default=None, description="Property names to copy (default: all readable properties)."
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CopyPropertiesParams,
    result=BatchResult,
    verify=_verify_batch,
    snapshot="timeline",
)
def copy_properties(s: ResolveSession, p: CopyPropertiesParams) -> BatchResult:
    """Copy properties from one item to many (timeline snapshot taken first)."""
    source = resolve_item(s, p.source)
    props: dict[str, Any] = dict(
        (source.GetProperties() if s.caps.has(source, "GetProperties") else source.GetProperty())
        or {}
    )
    if p.keys is not None:
        unknown = [k for k in p.keys if k not in props]
        if unknown:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS,
                f"Source item has no readable property {unknown}.",
                retryable=False,
            )
        props = {k: props[k] for k in p.keys}
    if not props:
        raise DavinciError(ErrorCode.NOT_FOUND, "Source item exposes no property.", retryable=False)
    return _run_batch(_resolve_all(s, p.targets), lambda it: _apply_props(s, it, props))


# -- speed / fades ---------------------------------------------------------------------


class SetSpeedParams(_Batch):
    speed: SpeedOptions


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetSpeedParams,
    result=BatchResult,
    verify=_verify_batch,
    since=V21_1,
)
def set_speed(s: ResolveSession, p: SetSpeedParams) -> BatchResult:
    """Set clip speed (Percentage, PitchCorrection...) on one or many items."""
    data: dict[str, Any] = p.speed.model_dump(exclude_none=True)
    if not data:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "No speed option given.", retryable=False)

    def apply(item: ResolveObject) -> tuple[bool, dict[str, Any], str | None]:
        called = bool(item.SetSpeed(data))
        observed: dict[str, Any] = dict(item.GetSpeed() or {})
        bad = [k for k, v in data.items() if k in observed and not _same(observed[k], v)]
        if "Percentage" in data and "Percentage" not in observed:
            bad.append("Percentage")
        if bad:
            return False, observed, f"Read-back differs for {bad}."
        return called, observed, None if called else "Resolve returned False."

    return _run_batch(_resolve_all(s, p.items), apply)


class SetFadesParams(_Batch):
    fades: FadeInfo


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetFadesParams,
    result=BatchResult,
    verify=_verify_batch,
    since=V21_1,
)
def set_fades(s: ResolveSession, p: SetFadesParams) -> BatchResult:
    """Set fade-in / fade-out durations (frames) on one or many items."""
    data: dict[str, Any] = p.fades.model_dump(exclude_none=True)
    if not data:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "No fade value given.", retryable=False)

    def apply(item: ResolveObject) -> tuple[bool, dict[str, Any], str | None]:
        called = bool(item.SetFades(data))
        observed: dict[str, Any] = dict(item.GetFades() or {})
        bad = [k for k, v in data.items() if not _same(observed.get(k), v)]
        if bad:
            return False, observed, f"Read-back differs for {bad}."
        return called, observed, None if called else "Resolve returned False."

    return _run_batch(_resolve_all(s, p.items), apply)


# -- transition ------------------------------------------------------------------------


class AddTransitionParams(_One):
    options: TransitionOptions


def _ids_in_timeline(s: ResolveSession, ref: ItemRef) -> set[str]:
    timeline = resolve_timeline(s, ref.timeline)
    return {str(it.GetUniqueId()) for _, _, _, it in iter_items(timeline)}


def _verify_transition(
    s: ResolveSession, p: AddTransitionParams, r: TransitionResult
) -> Verification:
    found = bool(r.transition_id) and r.transition_id in _ids_in_timeline(s, p.item)
    return Verification(
        ok=found,
        observed={"transition_id": r.transition_id, "listed_in_timeline": found},
        note=None if found else "Returned transition is not listed in the timeline tracks.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AddTransitionParams,
    result=TransitionResult,
    verify=_verify_transition,
    since=V21_1,
)
def add_transition(s: ResolveSession, p: AddTransitionParams) -> TransitionResult:
    """Add a transition at the start or end of an item (needs source handles)."""
    item = resolve_item(s, p.item)
    created = item.AddTransition(p.options.model_dump(exclude_none=True))
    if created is None:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "TimelineItem.AddTransition returned None.",
            hint="The item likely has no handles on that side, or the type/category is invalid.",
            retryable=False,
        )
    return TransitionResult(
        host_id=str(item.GetUniqueId()),
        transition_id=str(created.GetUniqueId() or ""),
        name=str(created.GetName() or ""),
    )


# -- simple per-item state ---------------------------------------------------------------


class SetEnabledParams(_Batch):
    enabled: bool


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetEnabledParams,
    result=BatchResult,
    verify=_verify_batch,
    idempotent=True,
)
def set_enabled(s: ResolveSession, p: SetEnabledParams) -> BatchResult:
    """Enable or disable clips."""

    def apply(item: ResolveObject) -> tuple[bool, dict[str, Any], str | None]:
        called = bool(item.SetClipEnabled(p.enabled))
        now = item.GetClipEnabled()
        return called and bool(now) == p.enabled, {"enabled": now}, None

    return _run_batch(_resolve_all(s, p.items), apply)


class SetClipColorParams(_Batch):
    color: ClipColor | None = Field(default=None, description="Clip color; omit to clear it.")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetClipColorParams,
    result=BatchResult,
    verify=_verify_batch,
    idempotent=True,
)
def set_clip_color(s: ResolveSession, p: SetClipColorParams) -> BatchResult:
    """Set the clip color on items, or clear it when no color is given."""

    def apply(item: ResolveObject) -> tuple[bool, dict[str, Any], str | None]:
        called = bool(item.ClearClipColor() if p.color is None else item.SetClipColor(p.color))
        now = str(item.GetClipColor() or "")
        return called and now == (p.color or ""), {"clip_color": now}, None

    return _run_batch(_resolve_all(s, p.items), apply)


class AddFlagParams(_Batch):
    color: FlagColor


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AddFlagParams,
    result=BatchResult,
    verify=_verify_batch,
    idempotent=True,
)
def add_flag(s: ResolveSession, p: AddFlagParams) -> BatchResult:
    """Add a colored flag to items."""

    def apply(item: ResolveObject) -> tuple[bool, dict[str, Any], str | None]:
        called = bool(item.AddFlag(p.color))
        flags = [str(f) for f in proxies(item.GetFlagList())]
        return called and p.color in flags, {"flags": flags}, None

    return _run_batch(_resolve_all(s, p.items), apply)


class ClearFlagsParams(_Batch):
    color: FlagColor | Literal["All"] = "All"


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ClearFlagsParams,
    result=BatchResult,
    verify=_verify_batch,
    idempotent=True,
)
def clear_flags(s: ResolveSession, p: ClearFlagsParams) -> BatchResult:
    """Clear flags of one color, or all flags, on items."""

    def apply(item: ResolveObject) -> tuple[bool, dict[str, Any], str | None]:
        called = bool(item.ClearFlags(p.color))
        flags = [str(f) for f in proxies(item.GetFlagList())]
        gone = not flags if p.color == "All" else p.color not in flags
        return called and gone, {"flags": flags}, None

    return _run_batch(_resolve_all(s, p.items), apply)


class SetNameParams(_One):
    name: str = Field(min_length=1)


def _verify_name(s: ResolveSession, p: SetNameParams, r: NameResult) -> Verification:
    # Re-resolve by id: a name selector no longer matches after the rename.
    got = str(resolve_item(s, _item_by_id(p.item, r.id)).GetName())
    return Verification(ok=got == p.name, observed={"name": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetNameParams,
    result=NameResult,
    verify=_verify_name,
    idempotent=True,
)
def set_name(s: ResolveSession, p: SetNameParams) -> NameResult:
    """Rename a timeline item."""
    item = resolve_item(s, p.item)
    if not item.SetName(p.name):
        raise _failed("TimelineItem.SetName")
    return NameResult(id=str(item.GetUniqueId()), name=p.name)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=_Batch,
    result=BatchResult,
    verify_reason="Resolve exposes no stabilization state getter; outcomes report the call result.",
)
def stabilize(s: ResolveSession, p: _Batch) -> BatchResult:
    """Run the stabilizer on items (can be slow; no read-back exists)."""
    result = _run_batch(_resolve_all(s, p.items), lambda it: (bool(it.Stabilize()), {}, None))
    return _require_any_success(result, "TimelineItem.Stabilize")


# -- item markers ----------------------------------------------------------------------------


class AddItemMarkerParams(_One):
    frame: int = Field(ge=0, description="Marker frame as Resolve expects it (offset in the clip).")
    color: MarkerColor = "Blue"
    name: str = ""
    note: str = ""
    duration: int = Field(default=1, ge=1)
    custom_data: str = ""


def _verify_item_marker(s: ResolveSession, p: AddItemMarkerParams, _: ItemMarker) -> Verification:
    found = {m.frame: m for m in _item_markers(resolve_item(s, p.item))}.get(p.frame)
    ok = found is not None and found.color == p.color and found.name == p.name
    return Verification(ok=ok, observed=found.model_dump() if found else {})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AddItemMarkerParams,
    result=ItemMarker,
    verify=_verify_item_marker,
)
def add_item_marker(s: ResolveSession, p: AddItemMarkerParams) -> ItemMarker:
    """Add a marker on a timeline item."""
    item = resolve_item(s, p.item)
    if any(m.frame == p.frame for m in _item_markers(item)):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"A marker already exists at frame {p.frame}.", retryable=False
        )
    if not item.AddMarker(p.frame, p.color, p.name, p.note, p.duration, p.custom_data):
        raise _failed("TimelineItem.AddMarker")
    return ItemMarker(
        frame=p.frame,
        color=p.color,
        name=p.name,
        note=p.note,
        duration=p.duration,
        custom_data=p.custom_data,
    )


class UpdateMarkerDataParams(_One):
    frame: int = Field(ge=0)
    custom_data: str


def _verify_marker_data(
    s: ResolveSession, p: UpdateMarkerDataParams, _: ItemMarker
) -> Verification:
    got = str(resolve_item(s, p.item).GetMarkerCustomData(p.frame) or "")
    return Verification(ok=got == p.custom_data, observed={"custom_data": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=UpdateMarkerDataParams,
    result=ItemMarker,
    verify=_verify_marker_data,
    idempotent=True,
)
def update_marker_custom_data(s: ResolveSession, p: UpdateMarkerDataParams) -> ItemMarker:
    """Update the custom data string of an item marker."""
    item = resolve_item(s, p.item)
    marker = {m.frame: m for m in _item_markers(item)}.get(p.frame)
    if marker is None:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No marker at frame {p.frame}.", retryable=False)
    if not item.UpdateMarkerCustomData(p.frame, p.custom_data):
        raise _failed("TimelineItem.UpdateMarkerCustomData")
    return marker.model_copy(update={"custom_data": p.custom_data})


class DeleteItemMarkersParams(_One):
    frame: int | None = Field(default=None, ge=0)
    color: MarkerColor | Literal["All"] | None = None
    custom_data: str | None = None

    @model_validator(mode="after")
    def _one_selector(self) -> DeleteItemMarkersParams:
        if sum(v is not None for v in (self.frame, self.color, self.custom_data)) != 1:
            raise ValueError("use exactly one of frame, color or custom_data")
        return self


def _matching_markers(markers: list[ItemMarker], p: DeleteItemMarkersParams) -> list[ItemMarker]:
    if p.frame is not None:
        return [m for m in markers if m.frame == p.frame]
    if p.color is not None:
        return [m for m in markers if p.color in ("All", m.color)]
    # DeleteMarkerByCustomData removes only the first match.
    return [m for m in markers if m.custom_data == p.custom_data][:1]


def _plan_delete_markers(s: ResolveSession, p: DeleteItemMarkersParams) -> PlanDraft:
    item = resolve_item(s, p.item)
    hits = _matching_markers(_item_markers(item), p)
    if not hits:
        raise DavinciError(ErrorCode.NOT_FOUND, "No marker matches.", retryable=False)
    uid, name = _label(item)
    return PlanDraft(
        summary=f"Delete {len(hits)} marker(s) of item {name!r}.",
        effects=[f"Removes marker at frame {m.frame} ({m.color} {m.name!r})." for m in hits],
        targets=[f"item:{uid}@{m.frame}" for m in hits],
        fingerprint=_fingerprint([m.model_dump() for m in hits]),
    )


def _verify_markers_deleted(
    s: ResolveSession, p: DeleteItemMarkersParams, r: ItemMarkers
) -> Verification:
    left = {m.frame for m in _item_markers(resolve_item(s, p.item))}
    deleted = {m.frame for m in r.markers}
    return Verification(ok=not (left & deleted), observed={"remaining_frames": sorted(left)})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteItemMarkersParams,
    result=ItemMarkers,
    plan=_plan_delete_markers,
    verify=_verify_markers_deleted,
)
def delete_item_markers(s: ResolveSession, p: DeleteItemMarkersParams) -> ItemMarkers:
    """Delete item markers by frame, by color ('All' allowed) or by custom data."""
    item = resolve_item(s, p.item)
    hits = _matching_markers(_item_markers(item), p)
    if not hits:
        raise DavinciError(ErrorCode.NOT_FOUND, "No marker matches.", retryable=False)
    if p.frame is not None:
        ok = item.DeleteMarkerAtFrame(p.frame)
    elif p.color is not None:
        ok = item.DeleteMarkersByColor(p.color)
    else:
        ok = item.DeleteMarkerByCustomData(p.custom_data)
    if not ok:
        raise _failed("TimelineItem marker deletion")
    return ItemMarkers(id=str(item.GetUniqueId()), markers=hits)


# -- takes -------------------------------------------------------------------------------------


class AddTakeParams(_One):
    clip: ClipRef
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _range(self) -> AddTakeParams:
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("give both start_frame and end_frame, or neither")
        return self


def _verify_take_added(s: ResolveSession, p: AddTakeParams, r: TakesResult) -> Verification:
    now = _takes(resolve_item(s, _item_by_id(p.item, r.id))).count
    return Verification(
        ok=now == (r.takes_before or 0) + 1,
        observed={"count_before": r.takes_before, "count_after": now},
    )


def _item_by_id(ref: ItemRef, uid: str) -> ItemRef:
    return ItemRef(timeline=ref.timeline, id=uid)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AddTakeParams,
    result=TakesResult,
    verify=_verify_take_added,
)
def add_take(s: ResolveSession, p: AddTakeParams) -> TakesResult:
    """Add a Media Pool clip as a new take (creates the take selector if needed)."""
    item = resolve_item(s, p.item)
    clip = resolve_clip(s, p.clip)
    before = int(item.GetTakesCount() or 0)
    if p.start_frame is None:
        ok = item.AddTake(clip)
    else:
        ok = item.AddTake(clip, p.start_frame, p.end_frame)
    if not ok:
        raise _failed("TimelineItem.AddTake")
    result = _takes(item)
    result.takes_before = before
    return result


class SelectTakeParams(_One):
    index: int = Field(ge=1, description="1-based take index.")


def _verify_take_selected(s: ResolveSession, p: SelectTakeParams, r: TakesResult) -> Verification:
    got = int(resolve_item(s, _item_by_id(p.item, r.id)).GetSelectedTakeIndex() or 0)
    return Verification(ok=got == p.index, observed={"selected_index": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SelectTakeParams,
    result=TakesResult,
    verify=_verify_take_selected,
    idempotent=True,
)
def select_take(s: ResolveSession, p: SelectTakeParams) -> TakesResult:
    """Select a take by 1-based index."""
    item = resolve_item(s, p.item)
    count = int(item.GetTakesCount() or 0)
    if not 1 <= p.index <= count:
        raise DavinciError(
            ErrorCode.NOT_FOUND, f"Item has {count} take(s); index {p.index} is out of range."
        )
    if not item.SelectTakeByIndex(p.index):
        raise _failed("TimelineItem.SelectTakeByIndex")
    return _takes(item)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=_One,
    result=NameResult,
    verify_reason="Resolve exposes no finalized-state getter for take selectors.",
)
def finalize_takes(s: ResolveSession, p: _One) -> NameResult:
    """Finalize the take selection (the selected take replaces the selector)."""
    item = resolve_item(s, p.item)
    if int(item.GetTakesCount() or 0) == 0:
        raise DavinciError(ErrorCode.WRONG_STATE, "Item is not a take selector.", retryable=False)
    if not item.FinalizeTake():
        raise _failed("TimelineItem.FinalizeTake")
    uid, name = _label(item)
    return NameResult(id=uid, name=name)


class DeleteTakeParams(_One):
    index: int = Field(ge=1, description="1-based take index.")


def _plan_delete_take(s: ResolveSession, p: DeleteTakeParams) -> PlanDraft:
    item = resolve_item(s, p.item)
    takes = _takes(item)
    if not 1 <= p.index <= takes.count:
        raise DavinciError(
            ErrorCode.NOT_FOUND, f"Item has {takes.count} take(s); no take {p.index}."
        )
    take = takes.takes[p.index - 1]
    return PlanDraft(
        summary=f"Delete take {p.index} ({take.media_pool_item_name!r}) of {item.GetName()!r}.",
        effects=["Removes the take from the take selector; cannot be undone through the API."],
        targets=[f"item:{item.GetUniqueId()}#take{p.index}"],
        fingerprint=_fingerprint([takes.count, takes.selected_index, take.model_dump()]),
    )


def _verify_take_deleted(s: ResolveSession, p: DeleteTakeParams, r: TakesResult) -> Verification:
    now = _takes(resolve_item(s, _item_by_id(p.item, r.id))).count
    return Verification(
        ok=now == (r.takes_before or 0) - 1,
        observed={"count_before": r.takes_before, "count_after": now},
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteTakeParams,
    result=TakesResult,
    plan=_plan_delete_take,
    verify=_verify_take_deleted,
)
def delete_take(s: ResolveSession, p: DeleteTakeParams) -> TakesResult:
    """Delete a take by 1-based index."""
    item = resolve_item(s, p.item)
    before = int(item.GetTakesCount() or 0)
    if not 1 <= p.index <= before:
        raise DavinciError(ErrorCode.NOT_FOUND, f"Item has {before} take(s); no take {p.index}.")
    if not item.DeleteTakeByIndex(p.index):
        raise _failed("TimelineItem.DeleteTakeByIndex")
    result = _takes(item)
    result.takes_before = before
    return result


# -- output blanking -------------------------------------------------------------------------------


class SetOutputBlankingParams(_One):
    blanking: OutputBlanking


def _verify_blanking(
    s: ResolveSession, p: SetOutputBlankingParams, _: BlankingResult
) -> Verification:
    want: dict[str, Any] = p.blanking.model_dump(exclude_none=True)
    got = _blanking(resolve_item(s, p.item))
    ok = all(_same(got.blanking.get(k), v) for k, v in want.items())
    return Verification(
        ok=ok,
        observed=got.model_dump(),
        note=None
        if ok
        else "Blanking reads back empty while the timeline blanking is used: "
        "call set_use_timeline_blanking with use_timeline=false.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetOutputBlankingParams,
    result=BlankingResult,
    verify=_verify_blanking,
    since=V21_1,
    idempotent=True,
)
def set_output_blanking(s: ResolveSession, p: SetOutputBlankingParams) -> BlankingResult:
    """Set per-item output blanking in pixels (Top/Bottom/Left/Right)."""
    data: dict[str, Any] = p.blanking.model_dump(exclude_none=True)
    if not data:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "No blanking value given.", retryable=False)
    item = resolve_item(s, p.item)
    if not item.SetOutputBlanking(data):
        raise _failed("TimelineItem.SetOutputBlanking")
    return _blanking(item)


class UseTimelineBlankingParams(_One):
    use_timeline: bool


def _verify_use_blanking(
    s: ResolveSession, p: UseTimelineBlankingParams, _: BlankingResult
) -> Verification:
    got = resolve_item(s, p.item).GetUseTimelineForOutputBlanking()
    return Verification(ok=bool(got) == p.use_timeline, observed={"use_timeline": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=UseTimelineBlankingParams,
    result=BlankingResult,
    verify=_verify_use_blanking,
    since=V21_1,
    idempotent=True,
)
def set_use_timeline_blanking(s: ResolveSession, p: UseTimelineBlankingParams) -> BlankingResult:
    """Choose between the timeline output blanking and the item's own."""
    item = resolve_item(s, p.item)
    if not item.SetUseTimelineForOutputBlanking(p.use_timeline):
        raise _failed("TimelineItem.SetUseTimelineForOutputBlanking")
    return _blanking(item)


# -- multicam ----------------------------------------------------------------


class FlattenMulticamParams(_One):
    grade: FlattenMulticamGrade = "FLATTEN_MULTICAM_COPY_GRADE"


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=FlattenMulticamParams,
    result=FlattenResult,
    verify_reason="Flattening replaces the item; Resolve gives no multicam-state getter.",
    since=V21_1,
    snapshot="timeline",
)
def flatten_multicam(s: ResolveSession, p: FlattenMulticamParams) -> FlattenResult:
    """Flatten a multicam item into its active angle (timeline snapshot taken first)."""
    item = resolve_item(s, p.item)
    uid = str(item.GetUniqueId())
    if not item.FlattenMulticam(const(s, p.grade)):
        raise _failed("TimelineItem.FlattenMulticam")
    return FlattenResult(id=uid, grade=p.grade)


class SmartSwitchParams(_One):
    settings: SmartSwitchSettings = Field(default_factory=SmartSwitchSettings)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SmartSwitchParams,
    result=SmartSwitchResult,
    verify_reason="Smart Switch runs synchronously; Resolve has no status or result getter.",
    since=V21_1,
    snapshot="timeline",
    timeout_s=900.0,
)
def smart_switch(s: ResolveSession, p: SmartSwitchParams) -> SmartSwitchResult:
    """Run Multicam Smart Switch on a multicam item (synchronous, may take minutes)."""
    item = resolve_item(s, p.item)
    data: dict[str, Any] = p.settings.model_dump(by_alias=True, exclude_none=True)
    sent = dict(data)
    for key in _SMART_SWITCH_CONSTS:
        if key in sent:
            sent[key] = const(s, str(sent[key]))
    if not item.PerformMulticamSmartSwitch(sent):
        raise _failed("TimelineItem.PerformMulticamSmartSwitch")
    return SmartSwitchResult(id=str(item.GetUniqueId()), settings=data)


ACTIONS = [
    get_item,
    get_properties,
    get_speed,
    get_fades,
    get_takes,
    list_item_markers,
    get_output_blanking,
    set_properties,
    copy_properties,
    set_speed,
    set_fades,
    add_transition,
    set_enabled,
    set_clip_color,
    add_flag,
    clear_flags,
    set_name,
    stabilize,
    add_item_marker,
    update_marker_custom_data,
    add_take,
    select_take,
    finalize_takes,
    set_output_blanking,
    set_use_timeline_blanking,
    flatten_multicam,
    smart_switch,
    delete_item_markers,
    delete_take,
]
