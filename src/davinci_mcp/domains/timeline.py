# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Timeline domain (socle subset: listing and markers).

This module is the reference template for domain authors: typed params and
results, 1-based indices, read-back verification on writes, a plan with a
state fingerprint on destructive actions.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.api._generated.resolve_21_1 import MarkerColor
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import TimelineRef, TimelineSummary, iter_timelines, resolve_timeline

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "timeline"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class ListMarkersParams(_Params):
    timeline: TimelineRef | None = None
    color: MarkerColor | None = Field(default=None, description="Only markers of this color.")


class AddMarkerParams(_Params):
    timeline: TimelineRef | None = None
    frame: int = Field(ge=0, description="Frame offset from the timeline start (0-based).")
    color: MarkerColor = "Blue"
    name: str = ""
    note: str = ""
    duration: int = Field(default=1, ge=1)
    custom_data: str = ""


class DeleteMarkerParams(_Params):
    timeline: TimelineRef | None = None
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
        timelines=[
            TimelineSummary(id=tl.GetUniqueId(), name=tl.GetName(), index=i)
            for i, tl in iter_timelines(project)
        ],
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ListMarkersParams, result=MarkerList)
def list_markers(s: ResolveSession, p: ListMarkersParams) -> MarkerList:
    """List timeline markers, optionally filtered by color."""
    timeline = resolve_timeline(s, p.timeline)
    markers = [m for m in _markers(timeline).values() if p.color is None or m.color == p.color]
    return MarkerList(
        timeline_id=timeline.GetUniqueId(), markers=sorted(markers, key=lambda m: m.frame)
    )


def _verify_added(s: ResolveSession, p: AddMarkerParams, result: Marker) -> Verification:
    found = _markers(resolve_timeline(s, p.timeline)).get(p.frame)
    ok = found is not None and found.color == p.color and found.name == p.name
    return Verification(ok=ok, observed=found.model_dump() if found else {})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AddMarkerParams,
    result=Marker,
    verify=_verify_added,
)
def add_marker(s: ResolveSession, p: AddMarkerParams) -> Marker:
    """Add a marker at a frame offset of a timeline."""
    timeline = resolve_timeline(s, p.timeline)
    if p.frame in _markers(timeline):
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"A marker already exists at frame {p.frame}.",
            retryable=False,
        )
    ok = timeline.AddMarker(p.frame, p.color, p.name, p.note, p.duration, p.custom_data)
    if not ok:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Timeline.AddMarker returned False.",
            hint="The frame may be beyond the timeline end.",
            retryable=False,
        )
    return Marker(
        frame=p.frame,
        color=p.color,
        name=p.name,
        note=p.note,
        duration=p.duration,
        custom_data=p.custom_data,
    )


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


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
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "Timeline.DeleteMarkerAtFrame returned False.",
            retryable=False,
        )
    return marker


ACTIONS = [list_timelines, list_markers, add_marker, delete_marker]
