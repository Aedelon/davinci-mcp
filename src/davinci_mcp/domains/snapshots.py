# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Snapshot domain: explicit safety copies before risky work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.refs import TimelineRef, resolve_timeline
from davinci_mcp.safety.snapshots import SnapshotInfo

if TYPE_CHECKING:
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "snapshot"


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
    return runtime().snapshots.snapshot_timeline(s, resolve_timeline(s, p.timeline), p.label)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ProjectSnapshotParams,
    result=SnapshotInfo,
    verify_reason="SnapshotManager raises when SaveProject or ExportProject fails.",
)
def snapshot_project(s: ResolveSession, p: ProjectSnapshotParams) -> SnapshotInfo:
    """Save the project and export it as a .drp file in the snapshot directory."""
    return runtime().snapshots.snapshot_project(s, p.label)


ACTIONS = [snapshot_project, snapshot_timeline]
