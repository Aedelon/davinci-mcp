# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Background jobs (renders, long operations) polled on the Resolve executor and persisted."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from davinci_mcp.connection.backend import ResolveObject
from davinci_mcp.connection.manager import ResolveSession
from davinci_mcp.errors import DavinciError, ErrorCode

JobState = Literal["running", "succeeded", "failed", "cancelled", "lost"]
Interpreter = Callable[[Mapping[str, Any]], tuple[JobState, float | None, str | None]]
_MAX_POLL_FAILURES = 3
_COMPLETE = 100


class JobInfo(BaseModel):
    id: str
    kind: str
    label: str
    state: JobState
    progress: float | None = None
    started_at: str
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class _Connection(Protocol):
    async def call(
        self,
        fn: Callable[[ResolveSession], Any],
        *,
        retry_safe: bool,
        timeout: float | None = None,
    ) -> ResolveObject: ...


def default_interpret(data: Mapping[str, Any]) -> tuple[JobState, float | None, str | None]:
    """Interpret a render job status dict. ``JobStatus`` is localized, so it is not compared."""
    error = data.get("Error")
    if isinstance(error, str) and error.strip():
        return "failed", _progress(data), error.strip()
    progress = _progress(data)
    if data.get("done") is True or (progress is not None and progress >= _COMPLETE):
        return "succeeded", 100.0, None
    return "running", progress, None


def _progress(data: Mapping[str, Any]) -> float | None:
    raw = data.get("CompletionPercentage")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0.0, min(100.0, float(raw)))


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobManager:
    def __init__(
        self,
        connection: _Connection,
        state_file: Path,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._conn = connection
        self._state_file = state_file
        self._poll_interval_s = poll_interval_s
        self._jobs: dict[str, JobInfo] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_fns: dict[str, Callable[[ResolveSession], Any]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data: list[dict[str, Any]] = json.loads(self._state_file.read_text())
        except (OSError, ValueError):
            return
        changed = False
        for raw in data:
            job = JobInfo.model_validate(raw)
            if job.state == "running":
                job.state = "lost"
                job.finished_at = _now()
                job.error = "Server restarted while the job was running."
                changed = True
            self._jobs[job.id] = job
        if changed:
            self._persist()

    def _persist(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([j.model_dump() for j in self._jobs.values()], indent=2)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(payload)
        tmp.replace(self._state_file)

    def _finish(
        self,
        job: JobInfo,
        state: JobState,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        job.state = state
        job.finished_at = _now()
        if error is not None:
            job.error = error
        if result is not None:
            job.result = result
        self._persist()
        event = self._done.get(job.id)
        if event is not None:
            event.set()

    # -- public API --------------------------------------------------------
    async def start(
        self,
        kind: str,
        label: str,
        launch: Callable[[ResolveSession], Any],
        poll: Callable[[ResolveSession], Mapping[str, Any]],
        *,
        cancel: Callable[[ResolveSession], Any] | None = None,
        interpret: Interpreter = default_interpret,
    ) -> JobInfo:
        job = JobInfo(
            id=f"job-{uuid.uuid4().hex[:10]}",
            kind=kind,
            label=label,
            state="running",
            started_at=_now(),
        )
        self._jobs[job.id] = job
        self._done[job.id] = asyncio.Event()
        try:
            launched = await self._conn.call(launch, retry_safe=False)
        except DavinciError as exc:
            self._finish(job, "failed", error=exc.message)
            raise
        if not launched:
            self._finish(job, "failed", error="Resolve refused to start the job.")
            raise DavinciError(
                ErrorCode.API_CALL_FAILED,
                f"Resolve refused to start job '{label}'.",
                details={"job_id": job.id},
            )
        if cancel is not None:
            self._cancel_fns[job.id] = cancel
        self._persist()
        self._tasks[job.id] = asyncio.create_task(self._poll_loop(job, poll, interpret))
        return job.model_copy()

    async def _poll_loop(
        self,
        job: JobInfo,
        poll: Callable[[ResolveSession], Mapping[str, Any]],
        interpret: Interpreter,
    ) -> None:
        failures = 0
        while job.state == "running":
            try:
                data = await self._conn.call(poll, retry_safe=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if failures >= _MAX_POLL_FAILURES:
                    self._finish(job, "failed", error=f"Polling failed: {exc}")
                    return
                await asyncio.sleep(self._poll_interval_s)
                continue
            failures = 0
            state, progress, error = interpret(data)
            if job.state != "running":  # cancelled while polling
                return
            if progress is not None:
                job.progress = progress
            if state != "running":
                self._finish(job, state, error=error, result=dict(data))
                return
            self._persist()
            await asyncio.sleep(self._poll_interval_s)

    def _get(self, job_id: str) -> JobInfo:
        job = self._jobs.get(job_id)
        if job is None:
            raise DavinciError(ErrorCode.NOT_FOUND, f"Unknown job id: {job_id}")
        return job

    def status(self, job_id: str) -> JobInfo:
        return self._get(job_id).model_copy()

    def list(self) -> list[JobInfo]:
        return [j.model_copy() for j in self._jobs.values()]

    async def wait(
        self,
        job_id: str,
        timeout_s: float,
        on_progress: Callable[[float | None, str], Awaitable[None]] | None = None,
    ) -> JobInfo:
        """Wait until the job leaves ``running`` or ``timeout_s`` elapses (then return as-is)."""
        job = self._get(job_id)
        deadline = time.monotonic() + timeout_s
        event = self._done.get(job_id)
        while job.state == "running" and event is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if on_progress is not None:
                await on_progress(job.progress, job.label)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=min(remaining, self._poll_interval_s))
        return job.model_copy()

    async def cancel(self, job_id: str) -> JobInfo:
        job = self._get(job_id)
        if job.state != "running":
            return job.model_copy()
        cancel_fn = self._cancel_fns.get(job_id)
        if cancel_fn is None:
            raise DavinciError(
                ErrorCode.WRONG_STATE, f"Job '{job_id}' cannot be cancelled.", retryable=False
            )
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._conn.call(cancel_fn, retry_safe=False)
        self._finish(job, "cancelled")
        return job.model_copy()

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
