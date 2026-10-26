# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for davinci_mcp.executor."""

from __future__ import annotations

import threading
import time

import pytest

from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.executor import ResolveExecutor, assert_resolve_thread


async def test_all_calls_share_one_resolve_thread(executor: ResolveExecutor) -> None:
    names = {await executor.run(lambda: threading.current_thread().name) for _ in range(5)}
    assert len(names) == 1
    assert next(iter(names)).startswith("resolve-api")


async def test_timeout_marks_suspect(executor: ResolveExecutor) -> None:
    assert executor.suspect is False
    with pytest.raises(DavinciError) as info:
        await executor.run(lambda: time.sleep(0.5), timeout=0.05)
    assert info.value.code is ErrorCode.TIMEOUT
    assert executor.suspect is True
    executor.clear_suspect()
    assert executor.suspect is False


async def test_exception_propagates(executor: ResolveExecutor) -> None:
    def boom() -> None:
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        await executor.run(boom)


async def test_run_after_shutdown() -> None:
    ex = ResolveExecutor()
    ex.shutdown()
    with pytest.raises(DavinciError) as info:
        await ex.run(lambda: 1)
    assert info.value.code is ErrorCode.INTERNAL


async def test_assert_resolve_thread(executor: ResolveExecutor) -> None:
    with pytest.raises(DavinciError) as info:
        assert_resolve_thread()
    assert info.value.code is ErrorCode.INTERNAL
    await executor.run(assert_resolve_thread)
