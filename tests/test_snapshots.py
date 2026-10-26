# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for snapshots."""

# ruff: noqa: N802
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.safety.snapshots import SnapshotManager


class FakeTimeline:
    def __init__(self, project: FakeProject, name: str, dup_ok: bool = True) -> None:
        self.project = project
        self.name = name
        self.dup_ok = dup_ok
        self.dups: list[str] = []

    def GetName(self) -> str:
        return self.name

    def DuplicateTimeline(self, name: str) -> object | None:
        if not self.dup_ok:
            return None
        self.dups.append(name)
        self.project.current = "dup"  # duplication switches current timeline
        return object()


class FakeProject:
    def __init__(self, name: str = "Proj") -> None:
        self.name = name
        self.orig: Any = object()
        self.current: Any = self.orig

    def GetName(self) -> str:
        return self.name

    def GetCurrentTimeline(self) -> Any:
        return self.current

    def SetCurrentTimeline(self, tl: Any) -> bool:
        self.current = tl
        return True


class FakePM:
    def __init__(self) -> None:
        self.saved = 0
        self.exports: list[str] = []

    def SaveProject(self) -> bool:
        self.saved += 1
        return True

    def ExportProject(self, name: str, path: str, with_stills: bool = True) -> bool:
        Path(path).write_bytes(b"drp")
        self.exports.append(path)
        return True


class FakeSession:
    def __init__(self, project: FakeProject) -> None:
        self.resolve = object()
        self.project = project
        self.project_manager = FakePM()


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        self.now += 1
        return self.now


def test_timeline_snapshot_restores_current(tmp_path: Path) -> None:
    p = FakeProject()
    s = FakeSession(p)
    tl = FakeTimeline(p, "Main")
    m = SnapshotManager(tmp_path, 5, Clock())
    info = m.snapshot_timeline(s, tl, "before delete")  # type: ignore[arg-type]
    assert info.kind == "timeline"
    assert info.timeline_name is not None
    assert info.timeline_name.startswith("Main [dmcp-snap ")
    assert p.current is p.orig
    assert m.get(info.id) == info
    assert m.list("Proj") == [info]
    assert m.list("Other") == []


def test_timeline_dup_failure(tmp_path: Path) -> None:
    p = FakeProject()
    m = SnapshotManager(tmp_path, 5, Clock())
    with pytest.raises(DavinciError) as ei:
        m.snapshot_timeline(FakeSession(p), FakeTimeline(p, "Main", False), "x")  # type: ignore[arg-type]
    assert ei.value.code == ErrorCode.API_CALL_FAILED


def test_untitled_refused(tmp_path: Path) -> None:
    s = FakeSession(FakeProject("Untitled Project"))
    m = SnapshotManager(tmp_path, 5, Clock())
    with pytest.raises(DavinciError) as ei:
        m.snapshot_project(s, "x")  # type: ignore[arg-type]
    assert ei.value.code == ErrorCode.WRONG_STATE
    assert s.project_manager.saved == 0
    assert s.project_manager.exports == []


def test_project_snapshot_and_retention(tmp_path: Path) -> None:
    s = FakeSession(FakeProject())
    m = SnapshotManager(tmp_path, 2, Clock())
    infos = [m.snapshot_project(s, f"n{i}") for i in range(4)]  # type: ignore[arg-type]
    assert s.project_manager.saved == 4
    remaining = m.list()
    assert [i.id for i in remaining] == [infos[2].id, infos[3].id]
    assert not Path(infos[0].path or "").exists()
    assert not Path(infos[1].path or "").exists()
    assert Path(infos[3].path or "").exists()
    # index persisted across instances
    assert [i.id for i in SnapshotManager(tmp_path, 2).list()] == [infos[2].id, infos[3].id]


def test_get_unknown(tmp_path: Path) -> None:
    with pytest.raises(DavinciError) as ei:
        SnapshotManager(tmp_path, 2).get("nope")
    assert ei.value.code == ErrorCode.NOT_FOUND
