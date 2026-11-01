# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Timeline domain: listing, tracks, items, markers, editing, import/export.

Traps encoded here (see research/audit-existant.md section 6.2 B and E):
``AppendToTimeline`` end frames are exclusive and ``recordFrame`` is absolute
(this module takes timeline-relative offsets and adds ``GetStartFrame``);
``DuplicateTimeline`` silently moves the current timeline; ``Insert*`` calls land
on the source-track target; ``DeleteClips`` needs the Edit page and leaves linked
audio untouched; ``Timeline.Export`` needs live ``EXPORT_*`` constants.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.api._generated.resolve_21_1 import MarkerColor, TimelineSettings
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import (
    ClipRef,
    ItemRef,
    ItemSummary,
    TimelineRef,
    TimelineSummary,
    TrackSel,
    TrackType,
    const,
    iter_items,
    iter_timelines,
    proxies,
    resolve_clip,
    resolve_item,
    resolve_timeline,
    summarize_item,
)
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from pathlib import Path

    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "timeline"
_EPS = 1e-6
_TRACK_TYPES: tuple[TrackType, ...] = ("video", "audio", "subtitle")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TimelineParams(_Params):
    timeline: TimelineRef | None = None


# -- results ---------------------------------------------------------------------


class TimelineList(BaseModel):
    current_id: str | None
    timelines: list[TimelineSummary]


class Marker(BaseModel):
    frame: int = Field(description="Frame offset from the timeline start (0-based).")
    color: str
    name: str
    note: str
    duration: int
    custom_data: str


class MarkerList(BaseModel):
    timeline_id: str
    markers: list[Marker]


class TimelineInfo(BaseModel):
    id: str
    name: str
    start_frame: int
    end_frame: int = Field(description="Exclusive end frame (absolute).")
    start_timecode: str
    frame_rate: str | None
    resolution: str | None
    use_custom_settings: str | None
    track_counts: dict[str, int]


class SettingsResult(BaseModel):
    timeline_id: str
    settings: dict[str, Any]


class TrackInfo(BaseModel):
    type: TrackType
    index: int
    name: str
    enabled: bool
    locked: bool
    item_count: int
    sub_type: str | None = None


class TrackList(BaseModel):
    timeline_id: str
    tracks: list[TrackInfo]


class ItemPage(BaseModel):
    timeline_id: str
    total: int
    offset: int
    limit: int
    items: list[ItemSummary]


class ItemBrief(BaseModel):
    id: str
    name: str
    start: int | None = None
    end: int | None = None


class ItemBriefList(BaseModel):
    timeline_id: str
    items: list[ItemBrief]


class OptionalItem(BaseModel):
    timeline_id: str
    item: ItemBrief | None


class TimecodeResult(BaseModel):
    timeline_id: str
    timecode: str


class MarkInOutResult(BaseModel):
    timeline_id: str
    marks: dict[str, Any]


class BlankingResult(BaseModel):
    timeline_id: str
    top: int
    bottom: int
    left: int
    right: int


class Ack(BaseModel):
    """Generic write result: the timeline plus whatever was intended/observed."""

    timeline_id: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AppendResult(BaseModel):
    timeline_id: str
    requested: int
    appended: list[ItemBrief]


class ExportResult(BaseModel):
    timeline_id: str
    path: str
    format: str


class BulkMarkerResult(BaseModel):
    timeline_id: str
    added: list[int]
    skipped: list[int] = Field(description="Frames that already hold a marker.")


class DeleteResult(BaseModel):
    deleted: list[str]
    detail: dict[str, Any] = Field(default_factory=dict)


# -- shared helpers ----------------------------------------------------------------


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _fail(what: str, hint: str | None = None) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, f"{what} returned False/None.", hint=hint, retryable=False
    )


def _summary(timeline: ResolveObject, index: int) -> TimelineSummary:
    return TimelineSummary(id=timeline.GetUniqueId(), name=timeline.GetName(), index=index)


def _summary_of(s: ResolveSession, timeline: ResolveObject) -> TimelineSummary:
    uid = timeline.GetUniqueId()
    for i, tl in iter_timelines(s.project):
        if tl.GetUniqueId() == uid:
            return _summary(tl, i)
    return TimelineSummary(id=uid, name=timeline.GetName(), index=0)


def _names(s: ResolveSession) -> list[str]:
    return [tl.GetName() for _, tl in iter_timelines(s.project)]


def _item_ids(timeline: ResolveObject) -> set[str]:
    return {it.GetUniqueId() for _, _, _, it in iter_items(timeline)}


def _brief(item: ResolveObject) -> ItemBrief:
    return ItemBrief(
        id=item.GetUniqueId() or "",
        name=item.GetName() or "",
        start=int(item.GetStart()) if item.GetStart() is not None else None,
        end=int(item.GetEnd()) if item.GetEnd() is not None else None,
    )


def _settings(s: ResolveSession, timeline: ResolveObject) -> dict[str, Any]:
    if s.caps.has(timeline, "GetSettings"):
        return dict(timeline.GetSettings() or {})
    return dict(timeline.GetSetting() or {})


@contextmanager
def _as_current(s: ResolveSession, timeline: ResolveObject) -> Generator[None]:
    """Make ``timeline`` current for calls that act on the current one, then restore."""
    project = s.project
    previous = project.GetCurrentTimeline()
    if previous is not None and previous.GetUniqueId() == timeline.GetUniqueId():
        yield
        return
    if not project.SetCurrentTimeline(timeline):
        raise _fail("Project.SetCurrentTimeline")
    try:
        yield
    finally:
        if previous is not None:
            project.SetCurrentTimeline(previous)


def _resolve_items(s: ResolveSession, refs: list[ItemRef]) -> list[ResolveObject]:
    return [resolve_item(s, r) for r in refs]


def _same(a: object, b: object) -> bool:
    if str(a) == str(b):
        return True
    try:
        return abs(float(str(a)) - float(str(b))) < _EPS
    except ValueError:
        return False


# -- markers (existing) -------------------------------------------------------------


class ListMarkersParams(_TimelineParams):
    color: MarkerColor | None = Field(default=None, description="Only markers of this color.")


class AddMarkerParams(_TimelineParams):
    frame: int = Field(ge=0, description="Frame offset from the timeline start (0-based).")
    color: MarkerColor = "Blue"
    name: str = ""
    note: str = ""
    duration: int = Field(default=1, ge=1)
    custom_data: str = ""


class DeleteMarkerParams(_TimelineParams):
    frame: int = Field(ge=0, description="Frame offset of the marker to delete.")


def _markers(timeline: ResolveObject) -> dict[int, Marker]:
    raw: dict[Any, Any] = timeline.GetMarkers() or {}
    return {
        int(frame): Marker(
            frame=int(frame),
            color=str(info.get("color", "")),
            name=str(info.get("name", "")),
            note=str(info.get("note", "")),
            duration=int(info.get("duration", 1)),
            custom_data=str(info.get("customData", "")),
        )
        for frame, info in raw.items()
    }


@action(domain=DOMAIN, kind=Kind.READ, result=TimelineList)
def list_timelines(s: ResolveSession, _: BaseModel) -> TimelineList:
    """List the timelines of the current project with their ids."""
    project = s.project
    current = project.GetCurrentTimeline()
    return TimelineList(
        current_id=current.GetUniqueId() if current is not None else None,
        timelines=[_summary(tl, i) for i, tl in iter_timelines(project)],
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ListMarkersParams, result=MarkerList)
def list_markers(s: ResolveSession, p: ListMarkersParams) -> MarkerList:
    """List timeline markers, optionally filtered by color."""
    timeline = resolve_timeline(s, p.timeline)
    markers = [m for m in _markers(timeline).values() if p.color is None or m.color == p.color]
    return MarkerList(
        timeline_id=timeline.GetUniqueId(), markers=sorted(markers, key=lambda m: m.frame)
    )


class MarkerByDataParams(_TimelineParams):
    custom_data: str = Field(min_length=1)


@action(domain=DOMAIN, kind=Kind.READ, params=MarkerByDataParams, result=Marker)
def get_marker_by_custom_data(s: ResolveSession, p: MarkerByDataParams) -> Marker:
    """Find the first marker whose custom data matches."""
    timeline = resolve_timeline(s, p.timeline)
    for frame in sorted(_markers(timeline)):
        marker = _markers(timeline)[frame]
        if marker.custom_data == p.custom_data:
            return marker
    raise DavinciError(
        ErrorCode.NOT_FOUND, f"No marker has custom data {p.custom_data!r}.", retryable=False
    )


def _verify_added(s: ResolveSession, p: AddMarkerParams, result: Marker) -> Verification:
    found = _markers(resolve_timeline(s, p.timeline)).get(p.frame)
    ok = found is not None and found.color == p.color and found.name == p.name
    return Verification(ok=ok, observed=found.model_dump() if found else {})


@action(domain=DOMAIN, kind=Kind.WRITE, params=AddMarkerParams, result=Marker, verify=_verify_added)
def add_marker(s: ResolveSession, p: AddMarkerParams) -> Marker:
    """Add a marker at a frame offset of a timeline."""
    timeline = resolve_timeline(s, p.timeline)
    if p.frame in _markers(timeline):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"A marker already exists at frame {p.frame}.", retryable=False
        )
    ok = timeline.AddMarker(p.frame, p.color, p.name, p.note, p.duration, p.custom_data)
    if not ok:
        raise _fail("Timeline.AddMarker", "The frame may be beyond the timeline end.")
    return Marker(
        frame=p.frame,
        color=p.color,
        name=p.name,
        note=p.note,
        duration=p.duration,
        custom_data=p.custom_data,
    )


class BulkMarkersParams(_TimelineParams):
    markers: list[AddMarkerParams] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _no_nested_timeline(self) -> BulkMarkersParams:
        if any(m.timeline is not None for m in self.markers):
            raise ValueError("set the timeline once at the top level, not per marker")
        return self


def _verify_bulk(s: ResolveSession, p: BulkMarkersParams, result: BulkMarkerResult) -> Verification:
    present = _markers(resolve_timeline(s, p.timeline))
    missing = [f for f in result.added if f not in present]
    return Verification(ok=not missing, observed={"missing": missing})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=BulkMarkersParams,
    result=BulkMarkerResult,
    verify=_verify_bulk,
)
def add_markers(s: ResolveSession, p: BulkMarkersParams) -> BulkMarkerResult:
    """Add several markers in one call; frames already occupied are skipped."""
    timeline = resolve_timeline(s, p.timeline)
    taken = set(_markers(timeline))
    added: list[int] = []
    skipped: list[int] = []
    for m in p.markers:
        if m.frame in taken:
            skipped.append(m.frame)
            continue
        if not timeline.AddMarker(m.frame, m.color, m.name, m.note, m.duration, m.custom_data):
            raise _fail(
                f"Timeline.AddMarker (frame {m.frame})", f"Markers added before it: {added}."
            )
        taken.add(m.frame)
        added.append(m.frame)
    return BulkMarkerResult(timeline_id=timeline.GetUniqueId(), added=added, skipped=skipped)


class UpdateMarkerDataParams(_TimelineParams):
    frame: int = Field(ge=0)
    custom_data: str


def _verify_marker_data(s: ResolveSession, p: UpdateMarkerDataParams, _: Ack) -> Verification:
    found = _markers(resolve_timeline(s, p.timeline)).get(p.frame)
    return Verification(
        ok=found is not None and found.custom_data == p.custom_data,
        observed={"custom_data": found.custom_data if found else None},
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=UpdateMarkerDataParams,
    result=Ack,
    verify=_verify_marker_data,
)
def update_marker_custom_data(s: ResolveSession, p: UpdateMarkerDataParams) -> Ack:
    """Replace the custom data of the marker at a frame offset."""
    timeline = resolve_timeline(s, p.timeline)
    if p.frame not in _markers(timeline):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No marker at frame {p.frame}.", retryable=False)
    if not timeline.UpdateMarkerCustomData(p.frame, p.custom_data):
        raise _fail("Timeline.UpdateMarkerCustomData")
    return Ack(timeline_id=timeline.GetUniqueId(), detail={"frame": p.frame})


def _plan_delete(s: ResolveSession, p: DeleteMarkerParams) -> PlanDraft:
    timeline = resolve_timeline(s, p.timeline)
    marker = _markers(timeline).get(p.frame)
    if marker is None:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No marker at frame {p.frame}.", retryable=False)
    return PlanDraft(
        summary=f"Delete marker {marker.name!r} at frame {p.frame} of {timeline.GetName()!r}.",
        effects=[f"Removes the {marker.color} marker; this cannot be undone through the API."],
        targets=[f"timeline:{timeline.GetUniqueId()}@{p.frame}"],
        fingerprint=_fingerprint(marker.model_dump()),
    )


def _verify_deleted(s: ResolveSession, p: DeleteMarkerParams, _: Marker) -> Verification:
    still = _markers(resolve_timeline(s, p.timeline)).get(p.frame)
    return Verification(ok=still is None, observed={"still_present": still is not None})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteMarkerParams,
    result=Marker,
    plan=_plan_delete,
    verify=_verify_deleted,
)
def delete_marker(s: ResolveSession, p: DeleteMarkerParams) -> Marker:
    """Delete the timeline marker at a frame offset."""
    timeline = resolve_timeline(s, p.timeline)
    marker = _markers(timeline).get(p.frame)
    if marker is None:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No marker at frame {p.frame}.", retryable=False)
    if not timeline.DeleteMarkerAtFrame(p.frame):
        raise _fail("Timeline.DeleteMarkerAtFrame")
    return marker


class DeleteMarkersByColorParams(_TimelineParams):
    color: MarkerColor | Literal["All"] = Field(description="Marker color, or 'All'.")


def _plan_delete_markers(s: ResolveSession, p: DeleteMarkersByColorParams) -> PlanDraft:
    timeline = resolve_timeline(s, p.timeline)
    hit = [m for m in _markers(timeline).values() if p.color in ("All", m.color)]
    if not hit:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.color} marker to delete.", retryable=False)
    return PlanDraft(
        summary=f"Delete {len(hit)} {p.color} marker(s) of {timeline.GetName()!r}.",
        effects=["Markers cannot be restored through the API."],
        targets=[f"timeline:{timeline.GetUniqueId()}"],
        fingerprint=_fingerprint(sorted(m.model_dump_json() for m in hit)),
    )


def _verify_markers_gone(
    s: ResolveSession, p: DeleteMarkersByColorParams, _: DeleteResult
) -> Verification:
    left = [
        m.frame
        for m in _markers(resolve_timeline(s, p.timeline)).values()
        if p.color in ("All", m.color)
    ]
    return Verification(ok=not left, observed={"remaining": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteMarkersByColorParams,
    result=DeleteResult,
    plan=_plan_delete_markers,
    verify=_verify_markers_gone,
)
def delete_markers_by_color(s: ResolveSession, p: DeleteMarkersByColorParams) -> DeleteResult:
    """Delete all timeline markers of a color, or every marker with 'All'."""
    timeline = resolve_timeline(s, p.timeline)
    frames = [str(m.frame) for m in _markers(timeline).values() if p.color in ("All", m.color)]
    if not timeline.DeleteMarkersByColor(p.color):
        raise _fail("Timeline.DeleteMarkersByColor")
    return DeleteResult(deleted=frames)


# -- read: info, tracks, items ---------------------------------------------------------


@action(domain=DOMAIN, kind=Kind.READ, params=_TimelineParams, result=TimelineInfo)
def timeline_info(s: ResolveSession, p: _TimelineParams) -> TimelineInfo:
    """Name, id, frame range, start timecode, fps/resolution and track counts."""
    tl = resolve_timeline(s, p.timeline)
    cfg = _settings(s, tl)
    width, height = cfg.get("timelineResolutionWidth"), cfg.get("timelineResolutionHeight")
    fps = cfg.get("timelineFrameRate")
    return TimelineInfo(
        id=tl.GetUniqueId(),
        name=tl.GetName(),
        start_frame=int(tl.GetStartFrame()),
        end_frame=int(tl.GetEndFrame()),
        start_timecode=str(tl.GetStartTimecode() or ""),
        frame_rate=str(fps) if fps is not None else None,
        resolution=f"{width}x{height}" if width and height else None,
        use_custom_settings=cfg.get("useCustomSettings"),
        track_counts={t: int(tl.GetTrackCount(t) or 0) for t in _TRACK_TYPES},
    )


class GetSettingsParams(_TimelineParams):
    keys: list[str] | None = Field(default=None, description="Only these setting keys.")


@action(domain=DOMAIN, kind=Kind.READ, params=GetSettingsParams, result=SettingsResult)
def get_settings(s: ResolveSession, p: GetSettingsParams) -> SettingsResult:
    """Read the timeline settings (project settings when custom settings are off)."""
    tl = resolve_timeline(s, p.timeline)
    cfg = _settings(s, tl)
    if p.keys is not None:
        cfg = {k: cfg.get(k) for k in p.keys}
    return SettingsResult(timeline_id=tl.GetUniqueId(), settings=cfg)


@action(domain=DOMAIN, kind=Kind.READ, params=_TimelineParams, result=TrackList)
def list_tracks(s: ResolveSession, p: _TimelineParams) -> TrackList:
    """List tracks of every type with name, enabled, locked and item count."""
    tl = resolve_timeline(s, p.timeline)
    tracks: list[TrackInfo] = []
    for kind in _TRACK_TYPES:
        for i in range(1, int(tl.GetTrackCount(kind) or 0) + 1):
            tracks.append(
                TrackInfo(
                    type=kind,
                    index=i,
                    name=str(tl.GetTrackName(kind, i) or ""),
                    enabled=bool(tl.GetIsTrackEnabled(kind, i)),
                    locked=bool(tl.GetIsTrackLocked(kind, i)),
                    item_count=len(proxies(tl.GetItemListInTrack(kind, i))),
                    sub_type=(tl.GetTrackSubType(kind, i) or None) if kind == "audio" else None,
                )
            )
    return TrackList(timeline_id=tl.GetUniqueId(), tracks=tracks)


class ListItemsParams(_TimelineParams):
    track: TrackSel | None = Field(default=None, description="Only this track (default: all).")
    name_contains: str | None = Field(default=None, description="Case-insensitive substring.")
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


@action(domain=DOMAIN, kind=Kind.READ, params=ListItemsParams, result=ItemPage)
def list_items(s: ResolveSession, p: ListItemsParams) -> ItemPage:
    """List timeline items of a track or of the whole timeline (paginated)."""
    tl = resolve_timeline(s, p.timeline)
    if p.track is not None:
        items = proxies(tl.GetItemListInTrack(p.track.type, p.track.index))
        rows = [
            summarize_item(it, p.track.type, p.track.index, pos)
            for pos, it in enumerate(items, start=1)
        ]
    else:
        rows = [summarize_item(it, t, i, pos) for t, i, pos, it in iter_items(tl)]
    if p.name_contains:
        needle = p.name_contains.lower()
        rows = [r for r in rows if needle in r.name.lower()]
    return ItemPage(
        timeline_id=tl.GetUniqueId(),
        total=len(rows),
        offset=p.offset,
        limit=p.limit,
        items=rows[p.offset : p.offset + p.limit],
    )


@action(domain=DOMAIN, kind=Kind.READ, params=_TimelineParams, result=OptionalItem)
def current_video_item(s: ResolveSession, p: _TimelineParams) -> OptionalItem:
    """Return the video item under the playhead, if any."""
    tl = resolve_timeline(s, p.timeline)
    item = tl.GetCurrentVideoItem()
    return OptionalItem(timeline_id=tl.GetUniqueId(), item=_brief(item) if item else None)


@action(domain=DOMAIN, kind=Kind.READ, params=_TimelineParams, result=TimecodeResult)
def current_timecode(s: ResolveSession, p: _TimelineParams) -> TimecodeResult:
    """Return the playhead timecode."""
    tl = resolve_timeline(s, p.timeline)
    return TimecodeResult(timeline_id=tl.GetUniqueId(), timecode=str(tl.GetCurrentTimecode()))


@action(
    domain=DOMAIN,
    kind=Kind.READ,
    params=_TimelineParams,
    result=ItemBriefList,
    since=(21, 0, 4),
)
def selected_clips(s: ResolveSession, p: _TimelineParams) -> ItemBriefList:
    """List the currently selected timeline items (Resolve 21.0.4+)."""
    tl = resolve_timeline(s, p.timeline)
    s.caps.require(tl, "GetSelectedClips", since=(21, 0, 4))
    return ItemBriefList(
        timeline_id=tl.GetUniqueId(), items=[_brief(i) for i in proxies(tl.GetSelectedClips())]
    )


@action(domain=DOMAIN, kind=Kind.READ, params=_TimelineParams, result=MarkInOutResult)
def get_mark_in_out(s: ResolveSession, p: _TimelineParams) -> MarkInOutResult:
    """Return the video/audio mark in and out (frames relative to timeline start)."""
    tl = resolve_timeline(s, p.timeline)
    return MarkInOutResult(timeline_id=tl.GetUniqueId(), marks=dict(tl.GetMarkInOut() or {}))


def _blanking(tl: ResolveObject) -> BlankingResult:
    raw: dict[str, Any] = tl.GetOutputBlanking() or {}
    return BlankingResult(
        timeline_id=tl.GetUniqueId(),
        top=int(raw.get("Top", 0)),
        bottom=int(raw.get("Bottom", 0)),
        left=int(raw.get("Left", 0)),
        right=int(raw.get("Right", 0)),
    )


@action(
    domain=DOMAIN,
    kind=Kind.READ,
    params=_TimelineParams,
    result=BlankingResult,
    since=(21, 1, 0),
)
def get_output_blanking(s: ResolveSession, p: _TimelineParams) -> BlankingResult:
    """Return the timeline output blanking in pixels (Resolve 21.1+)."""
    tl = resolve_timeline(s, p.timeline)
    s.caps.require(tl, "GetOutputBlanking", since=(21, 1, 0))
    return _blanking(tl)


# -- write: timelines ------------------------------------------------------------------


class CreateTimelineParams(_Params):
    name: str = Field(min_length=1)


class ImportedTimeline(TimelineSummary):
    items: int = Field(description="Video and audio items in the imported timeline.")
    linked: int = Field(description="Items linked to a Media Pool clip.")
    offline: int = Field(description="Items with no linked media (check reels / Start TC).")
    warning: str | None = None


def _imported(s: ResolveSession, timeline: ResolveObject) -> ImportedTimeline:
    items = [it for _, _, _, it in iter_items(timeline, ("video", "audio"))]
    linked = sum(1 for it in items if it.GetMediaPoolItem() is not None)
    offline = len(items) - linked
    warning = None
    if offline:
        warning = (
            f"{offline} of {len(items)} items are offline. EDL/XML events link by reel or clip "
            "name AND source timecode: set the clips' 'Start TC' to match the list (or import "
            "with the right source clips path), delete this timeline and import again."
        )
    return ImportedTimeline(
        **_summary_of(s, timeline).model_dump(),
        items=len(items),
        linked=linked,
        offline=offline,
        warning=warning,
    )


def _verify_created(s: ResolveSession, _: object, result: TimelineSummary) -> Verification:
    ids = [tl.GetUniqueId() for _, tl in iter_timelines(s.project)]
    return Verification(ok=result.id in ids, observed={"timelines": len(ids)})


def _require_free_name(s: ResolveSession, name: str) -> None:
    if name in _names(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"A timeline named {name!r} already exists.",
            hint="Timeline names must be unique.",
            retryable=False,
        )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CreateTimelineParams,
    result=TimelineSummary,
    verify=_verify_created,
)
def create_timeline(s: ResolveSession, p: CreateTimelineParams) -> TimelineSummary:
    """Create an empty timeline in the current Media Pool folder."""
    _require_free_name(s, p.name)
    tl = s.project.GetMediaPool().CreateEmptyTimeline(p.name)
    if tl is None:
        raise _fail("MediaPool.CreateEmptyTimeline")
    return _summary_of(s, tl)


class SourceRange(_Params):
    clip: ClipRef

    @model_validator(mode="before")
    @classmethod
    def _from_ref(cls, value: object) -> object:
        """Accept a bare clip ref ("<uuid>" or {"id": ...}) for a whole clip."""
        if isinstance(value, str):
            return {"clip": value}
        if isinstance(value, dict) and "clip" not in value:
            return {"clip": cast("dict[str, object]", value)}
        return cast("object", value)

    start_frame: int | None = Field(default=None, ge=0, description="Source start frame.")
    end_frame: int | None = Field(
        default=None, ge=1, description="Source end frame, EXCLUSIVE (duration = end - start)."
    )

    @model_validator(mode="after")
    def _range(self) -> SourceRange:
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("give both start_frame and end_frame, or neither")
        if (
            self.start_frame is not None
            and self.end_frame is not None
            and self.end_frame <= self.start_frame
        ):
            raise ValueError("end_frame must be greater than start_frame")
        return self


class CreateFromClipsParams(_Params):
    name: str = Field(min_length=1)
    clips: list[SourceRange] = Field(min_length=1, max_length=500)


def _range_info(r: SourceRange, pool_item: ResolveObject) -> dict[str, Any]:
    info: dict[str, Any] = {"mediaPoolItem": pool_item}
    if r.start_frame is not None and r.end_frame is not None:
        info["startFrame"] = r.start_frame
        info["endFrame"] = r.end_frame
    return info


def _verify_from_clips(
    s: ResolveSession, p: CreateFromClipsParams, result: TimelineSummary
) -> Verification:
    for _, tl in iter_timelines(s.project):
        if tl.GetUniqueId() == result.id:
            count = len(_item_ids(tl))
            return Verification(ok=count > 0, observed={"items": count})
    return Verification(ok=False, observed={"items": 0})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CreateFromClipsParams,
    result=TimelineSummary,
    verify=_verify_from_clips,
)
def create_timeline_from_clips(s: ResolveSession, p: CreateFromClipsParams) -> TimelineSummary:
    """Create a timeline from Media Pool clips (optional source in/out per clip)."""
    _require_free_name(s, p.name)
    infos = [_range_info(r, resolve_clip(s, r.clip)) for r in p.clips]
    tl = s.project.GetMediaPool().CreateTimelineFromClips(p.name, infos)
    if tl is None:
        raise _fail(
            "MediaPool.CreateTimelineFromClips",
            "The Media Pool current folder must be the folder holding the clips.",
        )
    return _summary_of(s, tl)


class DuplicateParams(_TimelineParams):
    name: str = Field(min_length=1)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=DuplicateParams,
    result=TimelineSummary,
    verify=_verify_created,
)
def duplicate_timeline(s: ResolveSession, p: DuplicateParams) -> TimelineSummary:
    """Duplicate a timeline under a new name; the current timeline is left unchanged."""
    tl = resolve_timeline(s, p.timeline)
    _require_free_name(s, p.name)
    previous = s.project.GetCurrentTimeline()
    copy = tl.DuplicateTimeline(p.name)
    if previous is not None:
        # DuplicateTimeline silently moves the current timeline onto the copy.
        s.project.SetCurrentTimeline(previous)
    if copy is None:
        raise _fail("Timeline.DuplicateTimeline")
    return _summary_of(s, copy)


class SetCurrentParams(_Params):
    timeline: TimelineRef


def _verify_current(
    s: ResolveSession, p: SetCurrentParams, result: TimelineSummary
) -> Verification:
    cur = s.project.GetCurrentTimeline()
    uid = cur.GetUniqueId() if cur else None
    return Verification(ok=uid == result.id, observed={"current_id": uid})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetCurrentParams,
    result=TimelineSummary,
    idempotent=True,
    verify=_verify_current,
)
def set_current_timeline(s: ResolveSession, p: SetCurrentParams) -> TimelineSummary:
    """Make a timeline the current one."""
    tl = resolve_timeline(s, p.timeline)
    if not s.project.SetCurrentTimeline(tl):
        raise _fail("Project.SetCurrentTimeline")
    return _summary_of(s, tl)


class RenameParams(_TimelineParams):
    name: str = Field(min_length=1)


def _verify_renamed(s: ResolveSession, p: RenameParams, result: TimelineSummary) -> Verification:
    for _, tl in iter_timelines(s.project):
        if tl.GetUniqueId() == result.id:
            return Verification(ok=tl.GetName() == p.name, observed={"name": tl.GetName()})
    return Verification(ok=False)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RenameParams,
    result=TimelineSummary,
    idempotent=True,
    verify=_verify_renamed,
)
def rename_timeline(s: ResolveSession, p: RenameParams) -> TimelineSummary:
    """Rename a timeline (the new name must be unique)."""
    tl = resolve_timeline(s, p.timeline)
    if p.name != tl.GetName():
        _require_free_name(s, p.name)
    if not tl.SetName(p.name):
        raise _fail("Timeline.SetName", "The name must be unique in the project.")
    return _summary_of(s, tl)


class StartTimecodeParams(_TimelineParams):
    timecode: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}[:;]\d{2}$", description="HH:MM:SS:FF")


def _verify_start_tc(s: ResolveSession, p: StartTimecodeParams, _: Ack) -> Verification:
    got = resolve_timeline(s, p.timeline).GetStartTimecode()
    return Verification(ok=got == p.timecode, observed={"start_timecode": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=StartTimecodeParams,
    result=Ack,
    idempotent=True,
    verify=_verify_start_tc,
)
def set_start_timecode(s: ResolveSession, p: StartTimecodeParams) -> Ack:
    """Set the timeline start timecode."""
    tl = resolve_timeline(s, p.timeline)
    if not tl.SetStartTimecode(p.timecode):
        raise _fail("Timeline.SetStartTimecode")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"timecode": p.timecode})


class SetSettingsParams(_TimelineParams):
    settings: TimelineSettings = Field(description="Setting names and values (strings).")


def _verify_settings(s: ResolveSession, p: SetSettingsParams, _: Ack) -> Verification:
    wanted = p.settings.model_dump(exclude_none=True, by_alias=True)
    got = _settings(s, resolve_timeline(s, p.timeline))
    bad = {k: got.get(k) for k, v in wanted.items() if not _same(got.get(k), v)}
    return Verification(ok=not bad, observed={"mismatched": bad})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetSettingsParams,
    result=Ack,
    idempotent=True,
    verify=_verify_settings,
)
def set_settings(s: ResolveSession, p: SetSettingsParams) -> Ack:
    """Set timeline settings (verified key by key; some keys are silently refused)."""
    tl = resolve_timeline(s, p.timeline)
    wanted = {k: str(v) for k, v in p.settings.model_dump(exclude_none=True, by_alias=True).items()}
    if not wanted:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "No setting given.", retryable=False)
    if s.caps.has(tl, "SetSettings"):
        if not tl.SetSettings(wanted):
            raise _fail("Timeline.SetSettings", "Resolve may refuse keys such as frame rate.")
    else:
        refused = [k for k, v in wanted.items() if not tl.SetSetting(k, v)]
        if refused:
            raise _fail("Timeline.SetSetting", f"Refused keys: {refused}.")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"keys": sorted(wanted)})


# -- write: tracks -----------------------------------------------------------------------


class AddTrackParams(_TimelineParams):
    type: TrackType
    sub_type: str | None = Field(
        default=None,
        description="Audio layout: mono, stereo, 5.1, 5.1film, 7.1, 7.1film, adaptive1..",
    )


def _verify_track_added(s: ResolveSession, p: AddTrackParams, result: Ack) -> Verification:
    count = int(resolve_timeline(s, p.timeline).GetTrackCount(p.type) or 0)
    return Verification(ok=count == result.detail["index"], observed={"track_count": count})


@action(
    domain=DOMAIN, kind=Kind.WRITE, params=AddTrackParams, result=Ack, verify=_verify_track_added
)
def add_track(s: ResolveSession, p: AddTrackParams) -> Ack:
    """Add a video, audio or subtitle track (audio accepts a sub type)."""
    tl = resolve_timeline(s, p.timeline)
    before = int(tl.GetTrackCount(p.type) or 0)
    ok = tl.AddTrack(p.type, p.sub_type) if p.sub_type else tl.AddTrack(p.type)
    if not ok:
        raise _fail("Timeline.AddTrack", "Check the sub type for audio tracks.")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"type": p.type, "index": before + 1})


class TrackNameParams(_TimelineParams):
    track: TrackSel
    name: str


def _check_track(tl: ResolveObject, track: TrackSel) -> None:
    count = int(tl.GetTrackCount(track.type) or 0)
    if track.index > count:
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"{track.type} track {track.index} does not exist ({count} tracks).",
            retryable=False,
        )


def _verify_track_name(s: ResolveSession, p: TrackNameParams, _: Ack) -> Verification:
    got = resolve_timeline(s, p.timeline).GetTrackName(p.track.type, p.track.index)
    return Verification(ok=got == p.name, observed={"name": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TrackNameParams,
    result=Ack,
    idempotent=True,
    verify=_verify_track_name,
)
def rename_track(s: ResolveSession, p: TrackNameParams) -> Ack:
    """Rename a track."""
    tl = resolve_timeline(s, p.timeline)
    _check_track(tl, p.track)
    if not tl.SetTrackName(p.track.type, p.track.index, p.name):
        raise _fail("Timeline.SetTrackName")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"name": p.name})


class TrackEnableParams(_TimelineParams):
    track: TrackSel
    enabled: bool


def _verify_track_enabled(s: ResolveSession, p: TrackEnableParams, _: Ack) -> Verification:
    got = bool(resolve_timeline(s, p.timeline).GetIsTrackEnabled(p.track.type, p.track.index))
    return Verification(ok=got == p.enabled, observed={"enabled": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TrackEnableParams,
    result=Ack,
    idempotent=True,
    verify=_verify_track_enabled,
)
def set_track_enabled(s: ResolveSession, p: TrackEnableParams) -> Ack:
    """Enable or disable a track."""
    tl = resolve_timeline(s, p.timeline)
    _check_track(tl, p.track)
    if not tl.SetTrackEnable(p.track.type, p.track.index, p.enabled):
        raise _fail("Timeline.SetTrackEnable")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"enabled": p.enabled})


class TrackLockParams(_TimelineParams):
    track: TrackSel
    locked: bool


def _verify_track_locked(s: ResolveSession, p: TrackLockParams, _: Ack) -> Verification:
    got = bool(resolve_timeline(s, p.timeline).GetIsTrackLocked(p.track.type, p.track.index))
    return Verification(ok=got == p.locked, observed={"locked": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TrackLockParams,
    result=Ack,
    idempotent=True,
    verify=_verify_track_locked,
)
def set_track_locked(s: ResolveSession, p: TrackLockParams) -> Ack:
    """Lock or unlock a track (a locked V1 makes Insert* calls fail)."""
    tl = resolve_timeline(s, p.timeline)
    _check_track(tl, p.track)
    if not tl.SetTrackLock(p.track.type, p.track.index, p.locked):
        raise _fail("Timeline.SetTrackLock")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"locked": p.locked})


# -- write: clips on the timeline ----------------------------------------------------


class Placement(SourceRange):
    media_type: Literal["video", "audio"] | None = Field(
        default=None, description="Restrict to video-only or audio-only."
    )
    track_index: int | None = Field(default=None, ge=1)
    record_frame: int | None = Field(
        default=None,
        ge=0,
        description="Offset from the timeline start (0-based); the server adds GetStartFrame.",
    )


class AppendParams(_TimelineParams):
    clips: list[Placement] = Field(min_length=1, max_length=500)


def _verify_appended(s: ResolveSession, p: AppendParams, result: AppendResult) -> Verification:
    present = _item_ids(resolve_timeline(s, p.timeline))
    valid = [i for i in result.appended if i.id and i.id in present]
    ok = len(valid) == result.requested
    return Verification(
        ok=ok,
        observed={"requested": result.requested, "found": len(valid)},
        note=None
        if ok
        else "Some clips were not placed: overlapping items on a track win silently; "
        "failed chunks are also lost on save.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AppendParams,
    result=AppendResult,
    verify=_verify_appended,
    snapshot="timeline",
)
def append_clips(s: ResolveSession, p: AppendParams) -> AppendResult:
    """Append Media Pool clips to a timeline (end frames exclusive, record offsets relative)."""
    tl = resolve_timeline(s, p.timeline)
    base = int(tl.GetStartFrame())
    infos: list[dict[str, Any]] = []
    for c in p.clips:
        info = _range_info(c, resolve_clip(s, c.clip))
        if c.media_type is not None:
            info["mediaType"] = 1 if c.media_type == "video" else 2
        if c.track_index is not None:
            info["trackIndex"] = c.track_index
        if c.record_frame is not None:
            info["recordFrame"] = base + c.record_frame
        infos.append(info)
    with _as_current(s, tl):
        placed = proxies(s.project.GetMediaPool().AppendToTimeline(infos))
    return AppendResult(
        timeline_id=tl.GetUniqueId(),
        requested=len(infos),
        appended=[_brief(i) for i in placed],
    )


GenKind = Literal[
    "generator", "fusion_generator", "ofx_generator", "title", "fusion_title", "fusion_composition"
]
_GEN_METHODS: dict[str, str] = {
    "generator": "InsertGeneratorIntoTimeline",
    "fusion_generator": "InsertFusionGeneratorIntoTimeline",
    "ofx_generator": "InsertOFXGeneratorIntoTimeline",
    "title": "InsertTitleIntoTimeline",
    "fusion_title": "InsertFusionTitleIntoTimeline",
    "fusion_composition": "InsertFusionCompositionIntoTimeline",
}


class InsertParams(_TimelineParams):
    kind: GenKind
    name: str | None = Field(
        default=None, description="Generator/title name (not used by fusion_composition)."
    )

    @model_validator(mode="after")
    def _name(self) -> InsertParams:
        if self.kind != "fusion_composition" and not self.name:
            raise ValueError("name is required except for fusion_composition")
        return self


class InsertResult(BaseModel):
    timeline_id: str
    item: ItemBrief


def _verify_inserted(s: ResolveSession, p: InsertParams, result: InsertResult) -> Verification:
    present = _item_ids(resolve_timeline(s, p.timeline))
    return Verification(ok=result.item.id in present, observed={"item_id": result.item.id})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=InsertParams,
    result=InsertResult,
    verify=_verify_inserted,
    snapshot="timeline",
)
def insert_generated(s: ResolveSession, p: InsertParams) -> InsertResult:
    """Insert a generator, title, OFX or Fusion generator/title/comp at the playhead."""
    tl = resolve_timeline(s, p.timeline)
    method = _GEN_METHODS[p.kind]
    s.caps.require(tl, method)
    with _as_current(s, tl):
        fn = getattr(tl, method)
        item = fn() if p.kind == "fusion_composition" else fn(p.name)
    if item is None:
        raise _fail(
            f"Timeline.{method}",
            "It lands on the Source Track Selector target; an unlocked V1 is required.",
        )
    return InsertResult(timeline_id=tl.GetUniqueId(), item=_brief(item))


class CompoundParams(_TimelineParams):
    items: list[ItemRef] = Field(min_length=1, max_length=200)
    name: str | None = None
    start_timecode: str | None = None


def _verify_new_item(s: ResolveSession, p: _TimelineParams, result: InsertResult) -> Verification:
    present = _item_ids(resolve_timeline(s, p.timeline))
    return Verification(ok=result.item.id in present, observed={"item_id": result.item.id})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CompoundParams,
    result=InsertResult,
    verify=_verify_new_item,
    snapshot="timeline",
)
def create_compound_clip(s: ResolveSession, p: CompoundParams) -> InsertResult:
    """Create a compound clip from timeline items."""
    tl = resolve_timeline(s, p.timeline)
    items = _resolve_items(s, p.items)
    opts: dict[str, str] = {}
    if p.name:
        opts["name"] = p.name
    if p.start_timecode:
        opts["startTimecode"] = p.start_timecode
    item = tl.CreateCompoundClip(items, opts) if opts else tl.CreateCompoundClip(items)
    if item is None:
        raise _fail("Timeline.CreateCompoundClip")
    return InsertResult(timeline_id=tl.GetUniqueId(), item=_brief(item))


class FusionClipParams(_TimelineParams):
    items: list[ItemRef] = Field(min_length=1, max_length=200)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=FusionClipParams,
    result=InsertResult,
    verify=_verify_new_item,
    snapshot="timeline",
)
def create_fusion_clip(s: ResolveSession, p: FusionClipParams) -> InsertResult:
    """Create a Fusion clip from timeline items."""
    tl = resolve_timeline(s, p.timeline)
    item = tl.CreateFusionClip(_resolve_items(s, p.items))
    if item is None:
        raise _fail("Timeline.CreateFusionClip")
    return InsertResult(timeline_id=tl.GetUniqueId(), item=_brief(item))


class LinkParams(_TimelineParams):
    items: list[ItemRef] = Field(min_length=2, max_length=200)
    linked: bool


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=LinkParams,
    result=Ack,
    verify_reason="Resolve exposes no getter for clip links.",
)
def set_clips_linked(s: ResolveSession, p: LinkParams) -> Ack:
    """Link or unlink timeline items."""
    tl = resolve_timeline(s, p.timeline)
    if not tl.SetClipsLinked(_resolve_items(s, p.items), p.linked):
        raise _fail("Timeline.SetClipsLinked")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"linked": p.linked, "count": len(p.items)})


class AutoAlignParams(_TimelineParams):
    items: list[ItemRef] = Field(min_length=2, max_length=200)
    sync_using: Literal["timecode", "waveform"] = "timecode"
    use_track: int | Literal["mix", "automatic"] | None = Field(
        default=None, description="Waveform mode only: track number, 'mix' or 'automatic'."
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AutoAlignParams,
    result=Ack,
    since=(21, 1, 0),
    snapshot="timeline",
    verify_reason="Resolve exposes no read-back for alignment; compare item starts manually.",
)
def auto_align_clips(s: ResolveSession, p: AutoAlignParams) -> Ack:
    """Align timeline items by timecode or waveform (Resolve 21.1+)."""
    tl = resolve_timeline(s, p.timeline)
    s.caps.require(tl, "AutoAlignClips", since=(21, 1, 0))
    options: dict[str, object] = {
        "SyncUsing": const(s, f"AUTO_ALIGN_CLIPS_USING_{p.sync_using.upper()}")
    }
    if p.use_track is not None:
        options["UseTrack"] = (
            p.use_track
            if isinstance(p.use_track, int)
            else const(s, f"AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_{p.use_track.upper()}")
        )
    if not tl.AutoAlignClips(_resolve_items(s, p.items), options):
        raise _fail("Timeline.AutoAlignClips")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"sync_using": p.sync_using})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=_TimelineParams,
    result=Ack,
    verify_reason="Resolve exposes no stereo getter.",
    snapshot="timeline",
)
def convert_to_stereo(s: ResolveSession, p: _TimelineParams) -> Ack:
    """Convert the timeline to stereoscopic 3D."""
    tl = resolve_timeline(s, p.timeline)
    if not tl.ConvertTimelineToStereo():
        raise _fail("Timeline.ConvertTimelineToStereo")
    return Ack(timeline_id=tl.GetUniqueId())


# -- write: playhead, marks, blanking -------------------------------------------------


class SetTimecodeParams(_TimelineParams):
    timecode: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}[:;]\d{2}$")


_NO_PLAYHEAD_PAGES = frozenset({"media"})


def _verify_playhead(s: ResolveSession, p: SetTimecodeParams, _: Ack) -> Verification:
    got = resolve_timeline(s, p.timeline).GetCurrentTimecode()
    return Verification(ok=got == p.timecode, observed={"timecode": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetTimecodeParams,
    result=Ack,
    idempotent=True,
    verify=_verify_playhead,
)
def set_current_timecode(s: ResolveSession, p: SetTimecodeParams) -> Ack:
    """Move the playhead to a timecode (from the Edit page when Resolve is on Media)."""
    tl = resolve_timeline(s, p.timeline)
    # The Media page has no playhead: SetCurrentTimecode returns False there and
    # GetCurrentTimecode None. Set it from the Edit page (the pipeline restores the page).
    if s.resolve.GetCurrentPage() in _NO_PLAYHEAD_PAGES and not s.resolve.OpenPage("edit"):
        raise _fail("Resolve.OpenPage('edit')", "The playhead only exists on timeline pages.")
    if not tl.SetCurrentTimecode(p.timecode):
        start, end = tl.GetStartTimecode(), tl.GetEndFrame()
        raise _fail(
            "Timeline.SetCurrentTimecode",
            f"Check the timecode is inside the timeline (starts at {start}, "
            f"{int(end) - int(tl.GetStartFrame())} frames long) and is the current one.",
        )
    return Ack(timeline_id=tl.GetUniqueId(), detail={"timecode": p.timecode})


class SetMarkParams(_TimelineParams):
    mark_in: int = Field(ge=0, description="Frame relative to the timeline start.")
    mark_out: int = Field(ge=0)
    type: Literal["video", "audio", "all"] = "all"

    @model_validator(mode="after")
    def _order(self) -> SetMarkParams:
        if self.mark_out < self.mark_in:
            raise ValueError("mark_out must not precede mark_in")
        return self


def _verify_marks(s: ResolveSession, p: SetMarkParams, _: Ack) -> Verification:
    got: dict[str, Any] = resolve_timeline(s, p.timeline).GetMarkInOut() or {}
    kinds = ("video", "audio") if p.type == "all" else (p.type,)
    seen = [got[k] for k in kinds if k in got]
    ok = bool(seen) and all(r.get("in") == p.mark_in and r.get("out") == p.mark_out for r in seen)
    return Verification(ok=ok, observed=dict(got))


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetMarkParams,
    result=Ack,
    idempotent=True,
    verify=_verify_marks,
)
def set_mark_in_out(s: ResolveSession, p: SetMarkParams) -> Ack:
    """Set mark in/out (frames relative to timeline start)."""
    tl = resolve_timeline(s, p.timeline)
    if not tl.SetMarkInOut(p.mark_in, p.mark_out, p.type):
        raise _fail("Timeline.SetMarkInOut")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"in": p.mark_in, "out": p.mark_out})


class ClearMarkParams(_TimelineParams):
    type: Literal["video", "audio", "all"] = "all"


def _verify_marks_cleared(s: ResolveSession, p: ClearMarkParams, _: Ack) -> Verification:
    got: dict[str, Any] = resolve_timeline(s, p.timeline).GetMarkInOut() or {}
    kinds = ("video", "audio") if p.type == "all" else (p.type,)
    left = {k: got[k] for k in kinds if got.get(k)}
    return Verification(ok=not left, observed={"remaining": left})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ClearMarkParams,
    result=Ack,
    idempotent=True,
    verify=_verify_marks_cleared,
)
def clear_mark_in_out(s: ResolveSession, p: ClearMarkParams) -> Ack:
    """Clear mark in/out."""
    tl = resolve_timeline(s, p.timeline)
    if not tl.ClearMarkInOut(p.type):
        raise _fail("Timeline.ClearMarkInOut")
    return Ack(timeline_id=tl.GetUniqueId())


class BlankingParams(_TimelineParams):
    top: int = Field(default=0, ge=0)
    bottom: int = Field(default=0, ge=0)
    left: int = Field(default=0, ge=0)
    right: int = Field(default=0, ge=0)


def _verify_blanking(s: ResolveSession, p: BlankingParams, _: Ack) -> Verification:
    got = _blanking(resolve_timeline(s, p.timeline))
    ok = (got.top, got.bottom, got.left, got.right) == (p.top, p.bottom, p.left, p.right)
    return Verification(ok=ok, observed=got.model_dump(exclude={"timeline_id"}))


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=BlankingParams,
    result=Ack,
    since=(21, 1, 0),
    idempotent=True,
    verify=_verify_blanking,
)
def set_output_blanking(s: ResolveSession, p: BlankingParams) -> Ack:
    """Set the timeline output blanking in pixels (Resolve 21.1+)."""
    tl = resolve_timeline(s, p.timeline)
    s.caps.require(tl, "SetOutputBlanking", since=(21, 1, 0))
    if not tl.SetOutputBlanking(
        {"Top": p.top, "Bottom": p.bottom, "Left": p.left, "Right": p.right}
    ):
        raise _fail("Timeline.SetOutputBlanking")
    return Ack(timeline_id=tl.GetUniqueId())


# -- import / export ------------------------------------------------------------------


ExportFormat = Literal[
    "aaf",
    "drt",
    "edl",
    "fcp_7_xml",
    "fcpxml_1_8",
    "fcpxml_1_9",
    "fcpxml_1_10",
    "otio",
    "ale",
    "ale_cdl",
    "text_csv",
    "text_tab",
    "hdr_10_profile_a",
    "hdr_10_profile_b",
    "dolby_vision_ver_2_9",
    "dolby_vision_ver_4_0",
    "dolby_vision_ver_5_1",
]
ExportSubtype = Literal["none", "aaf_new", "aaf_existing", "cdl", "sdl", "missing_clips"]


class ExportParams(_TimelineParams):
    path: str = Field(description="Destination file; must not exist yet.")
    format: ExportFormat
    subtype: ExportSubtype = Field(
        default="none", description="aaf_new/aaf_existing for AAF; cdl/sdl/missing_clips for EDL."
    )


def _verify_export(s: ResolveSession, p: ExportParams, result: ExportResult) -> Verification:
    from pathlib import Path  # noqa: PLC0415 - runtime use only here

    exists = Path(result.path).exists()
    return Verification(ok=exists, observed={"exists": exists, "path": result.path})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportParams,
    result=ExportResult,
    verify=_verify_export,
)
def export_timeline(s: ResolveSession, p: ExportParams) -> ExportResult:
    """Export a timeline (EDL, XML, AAF, DRT, OTIO, ...) to a file."""
    tl = resolve_timeline(s, p.timeline)
    target = check_path(p.path, runtime().allowed_roots, must_exist=False)
    if target.exists():
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"{target} already exists.",
            hint="Pick a new path.",
            retryable=False,
        )
    etype = const(s, f"EXPORT_{p.format.upper()}")
    subtype = const(s, f"EXPORT_{p.subtype.upper()}")
    if not tl.Export(str(target), etype, subtype):
        raise _fail("Timeline.Export", "AAF needs aaf_new/aaf_existing; EDL is video-only.")
    return ExportResult(timeline_id=tl.GetUniqueId(), path=str(target), format=p.format)


class ImportParams(_Params):
    path: str = Field(description="EDL, XML, FCPXML, AAF, DRT, ADL or OTIO file.")
    timeline_name: str | None = Field(default=None, description="Ignored for DRT.")
    import_source_clips: bool | None = None
    source_clips_path: str | None = None
    interlace_processing: bool | None = Field(default=None, description="AAF only.")


def _import_options(p: ImportParams) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if p.timeline_name is not None:
        opts["timelineName"] = p.timeline_name
    if p.import_source_clips is not None:
        opts["importSourceClips"] = p.import_source_clips
    if p.source_clips_path is not None:
        opts["sourceClipsPath"] = str(
            check_path(p.source_clips_path, runtime().allowed_roots, must_exist=True)
        )
    if p.interlace_processing is not None:
        opts["interlaceProcessing"] = p.interlace_processing
    return opts


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportParams,
    result=ImportedTimeline,
    verify=_verify_created,
)
def import_timeline(s: ResolveSession, p: ImportParams) -> ImportedTimeline:
    """Create a timeline from an EDL/XML/AAF/DRT/OTIO file."""
    path: Path = check_path(p.path, runtime().allowed_roots, must_exist=True)
    if p.timeline_name is not None:
        _require_free_name(s, p.timeline_name)
    before = {tl.GetUniqueId() for _, tl in iter_timelines(s.project)}
    pool = s.project.GetMediaPool()
    opts = _import_options(p)
    tl = (
        pool.ImportTimelineFromFile(str(path), opts)
        if opts
        else pool.ImportTimelineFromFile(str(path))
    )
    if tl is None:
        raise _fail(
            "MediaPool.ImportTimelineFromFile",
            "Returns None when the timeline name exists or an OTIO file is not in Resolve form.",
        )
    if tl.GetUniqueId() in before:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "The import returned an EXISTING timeline; nothing new was created.",
            hint="FCP7 XML: the internal <sequence><name> wins over timeline_name; rename it.",
            retryable=False,
        )
    return _imported(s, tl)


class ImportIntoParams(_TimelineParams):
    path: str = Field(description="AAF file.")
    insert_additional_tracks: bool | None = None
    insert_with_offset: str | None = Field(default=None, description="HH:MM:SS:FF")
    source_clips_path: str | None = None
    link_to_source_camera_files: bool | None = None
    use_sizing_info: bool | None = None


def _verify_import_into(s: ResolveSession, p: ImportIntoParams, result: Ack) -> Verification:
    now = len(_item_ids(resolve_timeline(s, p.timeline)))
    return Verification(
        ok=now > result.detail["items_before"],
        observed={"items_before": result.detail["items_before"], "items_after": now},
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportIntoParams,
    result=Ack,
    verify=_verify_import_into,
    snapshot="timeline",
)
def import_into_timeline(s: ResolveSession, p: ImportIntoParams) -> Ack:
    """Import items from an AAF file into an existing timeline."""
    tl = resolve_timeline(s, p.timeline)
    path = check_path(p.path, runtime().allowed_roots, must_exist=True)
    mapping = {
        "insertAdditionalTracks": p.insert_additional_tracks,
        "insertWithOffset": p.insert_with_offset,
        "sourceClipsPath": None
        if p.source_clips_path is None
        else str(check_path(p.source_clips_path, runtime().allowed_roots, must_exist=True)),
        "linkToSourceCameraFiles": p.link_to_source_camera_files,
        "useSizingInfo": p.use_sizing_info,
    }
    opts = {k: v for k, v in mapping.items() if v is not None}
    before = len(_item_ids(tl))
    ok = tl.ImportIntoTimeline(str(path), opts) if opts else tl.ImportIntoTimeline(str(path))
    if not ok:
        raise _fail("Timeline.ImportIntoTimeline")
    return Ack(timeline_id=tl.GetUniqueId(), detail={"items_before": before})


# -- destructive ------------------------------------------------------------------------


class DeleteTimelinesParams(_Params):
    timelines: list[TimelineRef] = Field(min_length=1, max_length=50)


def _targets(s: ResolveSession, refs: list[TimelineRef]) -> list[ResolveObject]:
    out: dict[str, ResolveObject] = {}
    for ref in refs:
        if ref.id is None and ref.name is None and ref.index is None:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS,
                "Each timeline must be selected by id, name or index.",
                retryable=False,
            )
        tl = resolve_timeline(s, ref)
        out[tl.GetUniqueId()] = tl
    return list(out.values())


def _plan_delete_timelines(s: ResolveSession, p: DeleteTimelinesParams) -> PlanDraft:
    tls = _targets(s, p.timelines)
    state = [(t.GetUniqueId(), t.GetName(), len(_item_ids(t))) for t in tls]
    return PlanDraft(
        summary=f"Delete {len(tls)} timeline(s): {', '.join(repr(t.GetName()) for t in tls)}.",
        effects=[
            "Timelines are removed from the Media Pool; media stays.",
            "Deleting the current timeline changes the current selection.",
        ],
        targets=[f"timeline:{t.GetUniqueId()}" for t in tls],
        fingerprint=_fingerprint(state),
    )


def _verify_timelines_gone(
    s: ResolveSession, p: DeleteTimelinesParams, result: DeleteResult
) -> Verification:
    ids = {tl.GetUniqueId() for _, tl in iter_timelines(s.project)}
    left = [i for i in result.deleted if i in ids]
    return Verification(ok=not left, observed={"remaining": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteTimelinesParams,
    result=DeleteResult,
    plan=_plan_delete_timelines,
    verify=_verify_timelines_gone,
    snapshot="project",
)
def delete_timelines(s: ResolveSession, p: DeleteTimelinesParams) -> DeleteResult:
    """Delete timelines from the project."""
    tls = _targets(s, p.timelines)
    ids = [t.GetUniqueId() for t in tls]
    if not s.project.GetMediaPool().DeleteTimelines(tls):
        raise _fail("MediaPool.DeleteTimelines")
    return DeleteResult(deleted=ids)


class DeleteTrackParams(_TimelineParams):
    track: TrackSel


def _plan_delete_track(s: ResolveSession, p: DeleteTrackParams) -> PlanDraft:
    tl = resolve_timeline(s, p.timeline)
    _check_track(tl, p.track)
    items = proxies(tl.GetItemListInTrack(p.track.type, p.track.index))
    return PlanDraft(
        summary=f"Delete {p.track.type} track {p.track.index} with {len(items)} item(s).",
        effects=["All items on the track are removed with it.", "Higher tracks shift down."],
        targets=[f"timeline:{tl.GetUniqueId()}/{p.track.type}{p.track.index}"],
        fingerprint=_fingerprint([i.GetUniqueId() for i in items]),
    )


def _verify_track_deleted(
    s: ResolveSession, p: DeleteTrackParams, result: DeleteResult
) -> Verification:
    count = int(resolve_timeline(s, p.timeline).GetTrackCount(p.track.type) or 0)
    return Verification(ok=count == result.detail["count_after"], observed={"track_count": count})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteTrackParams,
    result=DeleteResult,
    plan=_plan_delete_track,
    verify=_verify_track_deleted,
    snapshot="timeline",
)
def delete_track(s: ResolveSession, p: DeleteTrackParams) -> DeleteResult:
    """Delete a track and the items on it."""
    tl = resolve_timeline(s, p.timeline)
    _check_track(tl, p.track)
    before = int(tl.GetTrackCount(p.track.type))
    if not tl.DeleteTrack(p.track.type, p.track.index):
        raise _fail("Timeline.DeleteTrack")
    return DeleteResult(
        deleted=[f"{p.track.type}{p.track.index}"], detail={"count_after": before - 1}
    )


class DeleteClipsParams(_TimelineParams):
    items: list[ItemRef] = Field(min_length=1, max_length=500)
    ripple: bool = False


def _plan_delete_clips(s: ResolveSession, p: DeleteClipsParams) -> PlanDraft:
    items = _resolve_items(s, p.items)
    state = [(i.GetUniqueId(), i.GetStart(), i.GetEnd()) for i in items]
    return PlanDraft(
        summary=f"Delete {len(items)} timeline item(s)" + (" with ripple." if p.ripple else "."),
        effects=[
            "Linked audio/video partners are NOT deleted: pass them explicitly.",
            "Runs on the Edit page (switched automatically).",
        ],
        targets=[f"item:{i.GetUniqueId()}" for i in items],
        fingerprint=_fingerprint(state),
    )


def _verify_clips_gone(
    s: ResolveSession, p: DeleteClipsParams, result: DeleteResult
) -> Verification:
    present = _item_ids(resolve_timeline(s, p.timeline))
    left = [i for i in result.deleted if i in present]
    return Verification(ok=not left, observed={"remaining": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteClipsParams,
    result=DeleteResult,
    page="edit",
    plan=_plan_delete_clips,
    verify=_verify_clips_gone,
    snapshot="timeline",
)
def delete_clips(s: ResolveSession, p: DeleteClipsParams) -> DeleteResult:
    """Delete timeline items (Edit page); linked audio is not removed automatically."""
    tl = resolve_timeline(s, p.timeline)
    items = _resolve_items(s, p.items)
    ids = [i.GetUniqueId() for i in items]
    if not tl.DeleteClips(items, p.ripple):
        raise _fail("Timeline.DeleteClips", "The first attempt is sometimes unstable; retry once.")
    return DeleteResult(deleted=ids, detail={"ripple": p.ripple})


ACTIONS = [
    list_timelines,
    timeline_info,
    get_settings,
    list_tracks,
    list_items,
    current_video_item,
    current_timecode,
    list_markers,
    get_marker_by_custom_data,
    get_mark_in_out,
    selected_clips,
    get_output_blanking,
    create_timeline,
    create_timeline_from_clips,
    duplicate_timeline,
    set_current_timeline,
    rename_timeline,
    set_start_timecode,
    set_settings,
    add_track,
    rename_track,
    set_track_enabled,
    set_track_locked,
    append_clips,
    insert_generated,
    create_compound_clip,
    create_fusion_clip,
    set_clips_linked,
    auto_align_clips,
    convert_to_stereo,
    set_current_timecode,
    set_mark_in_out,
    clear_mark_in_out,
    set_output_blanking,
    add_marker,
    add_markers,
    update_marker_custom_data,
    import_timeline,
    import_into_timeline,
    export_timeline,
    delete_timelines,
    delete_track,
    delete_markers_by_color,
    delete_marker,
    delete_clips,
]
