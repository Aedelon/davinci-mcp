# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Object references shared by all domains.

Unique ids (``GetUniqueId()``) are preferred; name and 1-based index are
fallbacks. Every index exposed by davinci-mcp is 1-based, like the Resolve API.
Resolve has no ``GetTimelineByName``: timelines are found by iterating indices.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.errors import DavinciError, ErrorCode

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _shorthand(value: object, *, text_field: str) -> object:
    """Accept a bare string for a ref: a UUID is an id, anything else goes to ``text_field``.

    Agents naturally pass ``"item": "<uuid>"``; refusing it costs a round trip.
    """
    if isinstance(value, str):
        return {"id": value} if _UUID_RE.match(value) else {text_field: value}
    return value


class TimelineRef(BaseModel):
    """Select a timeline by unique id, exact name, or 1-based index. Omit for the current one."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: object) -> object:
        return _shorthand(value, text_field="name")

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


# -- helpers -------------------------------------------------------------------


def proxies(value: ResolveObject) -> list[ResolveObject]:
    """Typed list from a Resolve list return (None or [] when empty)."""
    return list(value) if value else []


# -- constants --------------------------------------------------------------


def const(session: ResolveSession, name: str) -> object:
    """Live value of a ``resolve.*`` constant (Resolve rejects their string names silently)."""
    value = getattr(session.resolve, name, None)
    if value is None:
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            f"Constant resolve.{name} is not defined in Resolve {session.caps.version_string}.",
            retryable=False,
        )
    return value


# -- timeline items -----------------------------------------------------------

TrackType = Literal["video", "audio", "subtitle"]


class TrackSel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TrackType = "video"
    index: int = Field(default=1, ge=1, description="1-based track index")


class ItemRef(BaseModel):
    """Select a timeline item by unique id, by track + 1-based position, or by exact name."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: object) -> object:
        return _shorthand(value, text_field="name")

    timeline: TimelineRef | None = None
    id: str | None = Field(default=None, description="TimelineItem.GetUniqueId()")
    track: TrackSel | None = None
    position: int | None = Field(
        default=None, ge=1, description="1-based position in the track (requires track)"
    )
    name: str | None = None

    @model_validator(mode="after")
    def _selector(self) -> ItemRef:
        has_pos = self.track is not None and self.position is not None
        if sum((self.id is not None, has_pos, self.name is not None)) != 1:
            raise ValueError("use exactly one of id, track+position or name")
        return self


class ItemSummary(BaseModel):
    id: str
    name: str
    track_type: TrackType
    track_index: int
    position: int
    start: int
    end: int


def iter_items(
    timeline: ResolveObject, types: tuple[TrackType, ...] = ("video", "audio", "subtitle")
) -> list[tuple[TrackType, int, int, ResolveObject]]:
    """(track_type, track_index, position, item) for every item, all 1-based."""
    out: list[tuple[TrackType, int, int, ResolveObject]] = []
    for track_type in types:
        for track in range(1, int(timeline.GetTrackCount(track_type) or 0) + 1):
            items = proxies(timeline.GetItemListInTrack(track_type, track))
            out.extend((track_type, track, pos, it) for pos, it in enumerate(items, start=1))
    return out


def resolve_item(session: ResolveSession, ref: ItemRef) -> ResolveObject:
    timeline = resolve_timeline(session, ref.timeline)
    if ref.track is not None and ref.position is not None:
        items = proxies(timeline.GetItemListInTrack(ref.track.type, ref.track.index))
        if ref.position > len(items):
            raise DavinciError(
                ErrorCode.NOT_FOUND,
                f"{ref.track.type} track {ref.track.index} has {len(items)} items.",
                retryable=False,
            )
        return items[ref.position - 1]
    matches = [
        it
        for _, _, _, it in iter_items(timeline)
        if (ref.id is not None and it.GetUniqueId() == ref.id)
        or (ref.name is not None and it.GetName() == ref.name)
    ]
    return _single(matches, "timeline item", ref.model_dump(exclude_none=True))


def summarize_item(
    item: ResolveObject, track_type: TrackType, track_index: int, position: int
) -> ItemSummary:
    return ItemSummary(
        id=item.GetUniqueId(),
        name=item.GetName(),
        track_type=track_type,
        track_index=track_index,
        position=position,
        start=int(item.GetStart()),
        end=int(item.GetEnd()),
    )


# -- media pool -------------------------------------------------------------------


class FolderRef(BaseModel):
    """Select a Media Pool folder by unique id or by path from the root ('Master/Shots/Day 1')."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: object) -> object:
        return _shorthand(value, text_field="path")

    id: str | None = None
    path: str | None = Field(default=None, description="Folder names joined by '/'")

    @model_validator(mode="after")
    def _one(self) -> FolderRef:
        if self.id is not None and self.path is not None:
            raise ValueError("use only one of id or path")
        return self


class ClipRef(BaseModel):
    """Select a Media Pool clip by unique id, exact clip name, or source file path."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: object) -> object:
        return _shorthand(value, text_field="name")

    id: str | None = Field(default=None, description="MediaPoolItem.GetUniqueId()")
    name: str | None = None
    file_path: str | None = None

    @model_validator(mode="after")
    def _one(self) -> ClipRef:
        if sum(v is not None for v in (self.id, self.name, self.file_path)) != 1:
            raise ValueError("use exactly one of id, name or file_path")
        return self


def walk_folders(root: ResolveObject) -> list[tuple[str, ResolveObject]]:
    """(path, folder) for the root and every nested folder, depth first."""
    out: list[tuple[str, ResolveObject]] = []
    stack: list[tuple[str, ResolveObject]] = [(root.GetName(), root)]
    while stack:
        path, folder = stack.pop()
        out.append((path, folder))
        children = proxies(folder.GetSubFolderList())
        stack.extend((f"{path}/{child.GetName()}", child) for child in reversed(children))
    return out


def resolve_folder(session: ResolveSession, ref: FolderRef | None) -> ResolveObject:
    pool = session.project.GetMediaPool()
    root = pool.GetRootFolder()
    if ref is None or (ref.id is None and ref.path is None):
        return root
    for path, folder in walk_folders(root):
        if (ref.id is not None and folder.GetUniqueId() == ref.id) or (
            ref.path is not None and path.strip("/") == ref.path.strip("/")
        ):
            return folder
    raise DavinciError(
        ErrorCode.NOT_FOUND,
        f"No Media Pool folder matches {ref.model_dump(exclude_none=True)}.",
        retryable=False,
    )


def _clip_index(session: ResolveSession) -> dict[str, ResolveObject]:
    """uid -> clip for the whole Media Pool, cached until the next write or TTL."""

    def build() -> dict[str, ResolveObject]:
        root = session.project.GetMediaPool().GetRootFolder()
        index: dict[str, ResolveObject] = {}
        for _, folder in walk_folders(root):
            for clip in proxies(folder.GetClipList()):
                index[clip.GetUniqueId()] = clip
        return index

    try:
        from davinci_mcp.domains._runtime import runtime  # noqa: PLC0415 - optional binding

        cache = runtime().cache
    except RuntimeError:
        return build()
    return cache.get_or_compute(("clip_index", session.project.GetUniqueId()), build)


def all_clips(session: ResolveSession) -> list[ResolveObject]:
    return list(_clip_index(session).values())


def resolve_clip(session: ResolveSession, ref: ClipRef) -> ResolveObject:
    index = _clip_index(session)
    if ref.id is not None:
        clip = index.get(ref.id)
        if clip is None:
            raise DavinciError(ErrorCode.NOT_FOUND, f"No clip with id {ref.id}.", retryable=False)
        return clip
    if ref.name is not None:
        matches = [c for c in index.values() if c.GetName() == ref.name]
    else:
        matches = [c for c in index.values() if c.GetClipProperty("File Path") == ref.file_path]
    return _single(matches, "clip", ref.model_dump(exclude_none=True))


def _single(matches: list[ResolveObject], what: str, selector: dict[str, object]) -> ResolveObject:
    if not matches:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {what} matches {selector}.", retryable=False)
    if len(matches) > 1:
        raise DavinciError(
            ErrorCode.AMBIGUOUS_REFERENCE,
            f"{len(matches)} {what}s match {selector}; select by id instead.",
            details={"ids": [m.GetUniqueId() for m in matches]},
            retryable=False,
        )
    return matches[0]
