# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Timeline duplicates and project exports used as undo points before risky actions."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from davinci_mcp.connection.backend import ResolveObject
from davinci_mcp.connection.manager import ResolveSession
from davinci_mcp.errors import DavinciError, ErrorCode

UNTITLED = "Untitled Project"


class SnapshotInfo(BaseModel):
    id: str
    kind: Literal["timeline", "project"]
    created_at: str
    label: str
    project: str
    timeline_name: str | None = None
    path: str | None = None


def _safe_dir(name: str) -> str:
    return re.sub(r"[^\w .-]", "_", name).strip() or "project"


class SnapshotManager:
    def __init__(
        self,
        snapshot_dir: Path,
        retention: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = snapshot_dir
        self._retention = retention
        self._clock = clock
        self._index = snapshot_dir / "index.json"

    # -- index -------------------------------------------------------------
    def _load(self) -> list[SnapshotInfo]:
        if not self._index.exists():
            return []
        try:
            data: list[dict[str, Any]] = json.loads(self._index.read_text())
        except (OSError, ValueError):
            return []
        return [SnapshotInfo.model_validate(d) for d in data]

    def _save(self, items: list[SnapshotInfo]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([i.model_dump() for i in items], indent=2)
        tmp = self._index.with_suffix(".tmp")
        tmp.write_text(payload)
        tmp.replace(self._index)

    def _record(self, info: SnapshotInfo) -> None:
        items = self._load()
        items.append(info)
        keep: list[SnapshotInfo] = []
        same = [i for i in items if i.project == info.project]
        excess = max(0, len(same) - self._retention)
        drop_ids = {i.id for i in same[:excess]}
        for item in items:
            if item.id in drop_ids:
                if item.path:
                    Path(item.path).unlink(missing_ok=True)
            else:
                keep.append(item)
        self._save(keep)

    def _now(self) -> tuple[str, str]:
        ts = self._clock()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        return stamp, datetime.fromtimestamp(ts, UTC).isoformat()

    # -- public ------------------------------------------------------------
    def snapshot_timeline(
        self, session: ResolveSession, timeline: ResolveObject, label: str
    ) -> SnapshotInfo:
        project = session.project
        original = project.GetCurrentTimeline()
        stamp, created = self._now()
        orig_name = str(timeline.GetName())
        snap_name = f"{orig_name} [dmcp-snap {stamp}]"
        dup = timeline.DuplicateTimeline(snap_name)
        if original is not None:
            project.SetCurrentTimeline(original)
        if dup is None:
            raise DavinciError(
                ErrorCode.API_CALL_FAILED,
                f"Could not duplicate timeline '{orig_name}' for a snapshot.",
            )
        info = SnapshotInfo(
            id=f"snap-{stamp}-{uuid.uuid4().hex[:6]}",
            kind="timeline",
            created_at=created,
            label=label,
            project=str(project.GetName()),
            timeline_name=snap_name,
        )
        self._record(info)
        return info

    def snapshot_project(self, session: ResolveSession, label: str) -> SnapshotInfo:
        pm = session.project_manager
        project = session.project
        name = str(project.GetName())
        if name == UNTITLED:
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                "Cannot snapshot the transient 'Untitled Project'.",
                hint="Save the project under a name first.",
            )
        if not pm.SaveProject():
            raise DavinciError(ErrorCode.API_CALL_FAILED, "SaveProject failed before snapshot.")
        stamp, created = self._now()
        target_dir = self._dir / _safe_dir(name)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{stamp}.drp"
        if not pm.ExportProject(name, str(path)):
            raise DavinciError(ErrorCode.API_CALL_FAILED, f"ExportProject failed for '{name}'.")
        info = SnapshotInfo(
            id=f"snap-{stamp}-{uuid.uuid4().hex[:6]}",
            kind="project",
            created_at=created,
            label=label,
            project=name,
            path=str(path),
        )
        self._record(info)
        return info

    def list(self, project: str | None = None) -> list[SnapshotInfo]:
        items = self._load()
        return [i for i in items if project is None or i.project == project]

    def get(self, snapshot_id: str) -> SnapshotInfo:
        for item in self._load():
            if item.id == snapshot_id:
                return item
        raise DavinciError(ErrorCode.NOT_FOUND, f"Unknown snapshot id: {snapshot_id}")
