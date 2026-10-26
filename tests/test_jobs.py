# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the job manager."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.jobs import JobManager, default_interpret


class FakeConn:
    async def call(
        self, fn: Callable[[Any], Any], *, retry_safe: bool, timeout: float | None = None
    ) -> Any:
        return fn(None)


class Script:
    def __init__(self, *steps: dict[str, Any]) -> None:
        self.steps = list(steps)

    def __call__(self, _s: Any) -> dict[str, Any]:
        return self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]


def _mgr(tmp_path: Path) -> JobManager:
    return JobManager(FakeConn(), tmp_path / "jobs.json", poll_interval_s=0.01)


def test_default_interpret() -> None:
    assert default_interpret({"JobStatus": "Rendu", "CompletionPercentage": 40})[:2] == (
        "running",
        40.0,
    )
    assert (
        default_interpret({"JobStatus": "Terminé", "CompletionPercentage": 100})[0] == "succeeded"
    )
    assert default_interpret({"JobStatus": "x", "Error": "disk full"}) == (
        "failed",
        None,
        "disk full",
    )
    assert default_interpret({"done": True})[0] == "succeeded"


async def test_success_and_persist(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    poll = Script({"CompletionPercentage": 10}, {"CompletionPercentage": 100, "JobStatus": "Fini"})
    job = await m.start("render", "r1", lambda _s: "id", poll)
    assert job.state == "running"
    done = await m.wait(job.id, 2.0)
    assert done.state == "succeeded"
    assert done.progress == 100.0
    assert done.result == {"CompletionPercentage": 100, "JobStatus": "Fini"}
    saved = json.loads((tmp_path / "jobs.json").read_text())
    assert saved[0]["state"] == "succeeded"
    assert [j.id for j in m.list()] == [job.id]
    await m.aclose()


async def test_failure(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    job = await m.start("render", "r", lambda _s: True, Script({"Error": "boom"}))
    done = await m.wait(job.id, 2.0)
    assert done.state == "failed"
    assert done.error == "boom"
    await m.aclose()


async def test_launch_falsy(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    with pytest.raises(DavinciError) as ei:
        await m.start("render", "r", lambda _s: None, Script({}))
    assert ei.value.code == ErrorCode.API_CALL_FAILED
    assert m.list()[0].state == "failed"


async def test_lost_on_restart(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    job = await m.start("render", "r", lambda _s: True, Script({"CompletionPercentage": 5}))
    await m.aclose()
    m2 = _mgr(tmp_path)
    assert m2.status(job.id).state == "lost"
    assert json.loads((tmp_path / "jobs.json").read_text())[0]["state"] == "lost"


async def test_cancel(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    cancelled: list[bool] = []
    job = await m.start(
        "render",
        "r",
        lambda _s: True,
        Script({"CompletionPercentage": 5}),
        cancel=lambda _s: cancelled.append(True),
    )
    res = await m.cancel(job.id)
    assert res.state == "cancelled"
    assert cancelled == [True]
    await asyncio.sleep(0.05)
    assert m.status(job.id).state == "cancelled"
    await m.aclose()


async def test_cancel_not_supported(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    job = await m.start("render", "r", lambda _s: True, Script({"CompletionPercentage": 5}))
    with pytest.raises(DavinciError) as ei:
        await m.cancel(job.id)
    assert ei.value.code == ErrorCode.WRONG_STATE
    await m.aclose()


async def test_wait_timeout_and_progress(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    job = await m.start("render", "r", lambda _s: True, Script({"CompletionPercentage": 30}))
    seen: list[float | None] = []

    async def on_progress(p: float | None, _msg: str) -> None:
        seen.append(p)

    res = await m.wait(job.id, 0.1, on_progress)
    assert res.state == "running"
    assert seen
    await m.aclose()


def test_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(DavinciError) as ei:
        _mgr(tmp_path).status("nope")
    assert ei.value.code == ErrorCode.NOT_FOUND
