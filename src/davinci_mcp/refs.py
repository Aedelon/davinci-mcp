# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Object references shared by all domains.

Unique ids (``GetUniqueId()``) are preferred; name and 1-based index are
fallbacks. Every index exposed by davinci-mcp is 1-based, like the Resolve API.
Resolve has no ``GetTimelineByName``: timelines are found by iterating indices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.errors import DavinciError, ErrorCode

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession


class TimelineRef(BaseModel):
    """Select a timeline by unique id, exact name, or 1-based index. Omit for the current one."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="Timeline.GetUniqueId()")
    name: str | None = None
    index: int | None = Field(default=None, ge=1, description="1-based timeline index")

    @model_validator(mode="after")
    def _one_selector(self) -> TimelineRef:
        if sum(v is not None for v in (self.id, self.name, self.index)) > 1:
            raise ValueError("use only one of id, name or index")
        return self


class TimelineSummary(BaseModel):
    id: str
    name: str
    index: int


def iter_timelines(project: ResolveObject) -> list[tuple[int, ResolveObject]]:
    count = int(project.GetTimelineCount() or 0)
    return [(i, project.GetTimelineByIndex(i)) for i in range(1, count + 1)]


def resolve_timeline(session: ResolveSession, ref: TimelineRef | None) -> ResolveObject:
    """Return the Timeline proxy for ``ref`` (current timeline when ``ref`` is empty)."""
    if ref is None or (ref.id is None and ref.name is None and ref.index is None):
        return session.timeline
    project = session.project
    if ref.index is not None:
        timeline = project.GetTimelineByIndex(ref.index)
        if timeline is None:
            raise DavinciError(
                ErrorCode.NOT_FOUND,
                f"No timeline at index {ref.index} (project has {project.GetTimelineCount()}).",
                retryable=False,
            )
        return timeline
    matches = [
        tl
        for _, tl in iter_timelines(project)
        if (ref.id is not None and tl.GetUniqueId() == ref.id)
        or (ref.name is not None and tl.GetName() == ref.name)
    ]
    if not matches:
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"No timeline matches {ref.model_dump(exclude_none=True)}.",
            next_call="timeline_read(op={'action': 'list_timelines'})",
            retryable=False,
        )
    if len(matches) > 1:
        raise DavinciError(
            ErrorCode.AMBIGUOUS_REFERENCE,
            f"{len(matches)} timelines are named {ref.name!r}; select by id instead.",
            details={"ids": [tl.GetUniqueId() for tl in matches]},
            retryable=False,
        )
    return matches[0]


def summarize_timeline(timeline: ResolveObject, index: int) -> TimelineSummary:
    return TimelineSummary(id=timeline.GetUniqueId(), name=timeline.GetName(), index=index)
