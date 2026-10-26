# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for object references, the read cache and the audit log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.__main__ import main
from davinci_mcp.audit import AuditLog
from davinci_mcp.cache import ReadCache
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.refs import TimelineRef, resolve_timeline, summarize_timeline
from tests.fakes.fake_resolve import make_resolve


class _Session:
    def __init__(self, resolve: Any) -> None:
        self.project = resolve.GetProjectManager().GetCurrentProject()
        self.timeline = self.project.GetCurrentTimeline()


@pytest.fixture
def session() -> _Session:
    return _Session(make_resolve(timelines=3))


def test_resolve_timeline_selectors(session: _Session) -> None:
    s: Any = session
    assert resolve_timeline(s, None) is session.timeline
    second = session.project.GetTimelineByIndex(2)
    assert resolve_timeline(s, TimelineRef(index=2)) is second
    assert resolve_timeline(s, TimelineRef(id=second.GetUniqueId())) is second
    assert resolve_timeline(s, TimelineRef(name=second.GetName())) is second
    assert summarize_timeline(second, 2).index == 2


def test_resolve_timeline_errors(session: _Session) -> None:
    s: Any = session
    with pytest.raises(DavinciError) as exc:
        resolve_timeline(s, TimelineRef(index=9))
    assert exc.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(DavinciError) as exc:
        resolve_timeline(s, TimelineRef(name="nope"))
    assert exc.value.code is ErrorCode.NOT_FOUND
    tl = session.project.GetTimelineByIndex(1)
    session.project.GetTimelineByIndex(2)._name = tl.GetName()
    with pytest.raises(DavinciError) as exc:
        resolve_timeline(s, TimelineRef(name=tl.GetName()))
    assert exc.value.code is ErrorCode.AMBIGUOUS_REFERENCE


def test_timeline_ref_single_selector() -> None:
    with pytest.raises(ValueError, match="only one"):
        TimelineRef(id="a", index=1)


def test_cache_generation_and_ttl() -> None:
    now = [0.0]
    cache = ReadCache(ttl_s=1.0, max_entries=2, clock=lambda: now[0])
    calls: list[int] = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    assert cache.get_or_compute("k", compute) == 1
    assert cache.get_or_compute("k", compute) == 1
    cache.bump()
    assert cache.generation == 1
    assert cache.get_or_compute("k", compute) == 2
    now[0] = 5.0
    assert cache.get_or_compute("k", compute) == 3
    cache.get_or_compute("a", compute)
    cache.get_or_compute("b", compute)  # evicts the oldest entry
    assert len(cache._entries) == 2


def test_audit_log_rotation_and_tail(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=200)
    assert log.tail() == []
    for i in range(20):
        log.record({"action": "x", "i": i})
    assert (tmp_path / "audit.1.jsonl").exists()
    tail = log.tail(3)
    assert [e["i"] for e in tail] == [17, 18, 19]
    assert "ts" in tail[0]
    assert log.path == tmp_path / "audit.jsonl"


def test_cli_doctor_reports_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("DAVINCI_MCP_CONNECTION__AUTO_LAUNCH", "0")
    monkeypatch.setattr("davinci_mcp.connection.manager.find_resolve_process", lambda: None)
    monkeypatch.setattr("davinci_mcp.server.find_resolve_process", lambda: None)
    assert main(["doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["error"]["code"] == "RESOLVE_NOT_RUNNING"
