# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Snapshot domain: explicit safety copies before risky work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, action
from davinci_mcp.refs import TimelineRef, resolve_timeline
from davinci_mcp.safety.snapshots import SnapshotInfo, SnapshotManager

if TYPE_CHECKING:
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "snapshot"

# Set by the server at startup; actions run on the executor thread and need the manager.
_manager: SnapshotManager | None = None


def bind(manager: SnapshotManager) -> None:
    global _manager  # noqa: PLW0603 - single process-wide manager
    _manager = manager


def _get_manager() -> SnapshotManager:
    if _manager is None:
        raise RuntimeError("snapshot manager not bound")
    return _manager


class TimelineSnapshotParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeline: TimelineRef | None = None
    label: str = Field(default="manual", max_length=80)


class ProjectSnapshotParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="manual", max_length=80)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TimelineSnapshotParams,
    result=SnapshotInfo,
    verify_reason="SnapshotManager raises when DuplicateTimeline returns None.",
)
def snapshot_timeline(s: ResolveSession, p: TimelineSnapshotParams) -> SnapshotInfo:
    """Duplicate a timeline as a safety copy (current timeline by default)."""
    return _get_manager().snapshot_timeline(s, resolve_timeline(s, p.timeline), p.label)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ProjectSnapshotParams,
    result=SnapshotInfo,
    verify_reason="SnapshotManager raises when SaveProject or ExportProject fails.",
)
def snapshot_project(s: ResolveSession, p: ProjectSnapshotParams) -> SnapshotInfo:
    """Save the project and export it as a .drp file in the snapshot directory."""
    return _get_manager().snapshot_project(s, p.label)


ACTIONS = [snapshot_project, snapshot_timeline]
