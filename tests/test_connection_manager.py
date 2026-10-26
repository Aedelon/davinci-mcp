# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for davinci_mcp.connection.manager."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from davinci_mcp.config import ConnectionSettings, Settings
from davinci_mcp.connection.external import ResolvePaths
from davinci_mcp.connection.manager import ConnectionManager, ResolveSession, check_alive
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.executor import ResolveExecutor
from tests.conftest import RESOLVE_ARGV, FakeBackend
from tests.fakes.fake_resolve import FakeResolve, make_resolve


class FailingBackend:
    channel = "bridge-python"

    def __init__(self) -> None:
        self.connects = 0

    def connect(self) -> Any:
        self.connects += 1
        raise DavinciError(ErrorCode.NOT_CONNECTED, "boom")

    def close(self) -> None: ...


def _manager(
    backends: list[Any],
    executor: ResolveExecutor,
    conn: ConnectionSettings,
    *args: Any,
    **kwargs: Any,
) -> ConnectionManager:
    return ConnectionManager(backends, executor, conn, *args, **kwargs)


def test_requires_backend(executor: ResolveExecutor, settings: Settings) -> None:
    with pytest.raises(ValueError, match="backend"):
        ConnectionManager([], executor, settings.connection)


async def test_connect_and_capabilities(
    connection: ConnectionManager, backend: FakeBackend
) -> None:
    assert connection.connected is False
    session = await connection.session()
    assert connection.connected is True
    assert session.caps.edition == "studio"
    assert session.caps.database == {"DbType": "Disk", "DbName": "Local Database"}
    assert session.caps.channel == "external"
    assert session.caps.headless is False
    assert await connection.session() is session
    assert backend.connects == 1


async def test_session_properties(connection: ConnectionManager, executor: ResolveExecutor) -> None:
    session = await connection.session()

    def read() -> tuple[str, str]:
        return session.project.GetName(), session.timeline.GetName()

    assert await executor.run(read) == ("Test Project", "Timeline 1")


async def test_session_properties_off_thread(connection: ConnectionManager) -> None:
    session = await connection.session()
    with pytest.raises(DavinciError) as info:
        _ = session.project_manager
    assert info.value.code is ErrorCode.INTERNAL


async def test_session_no_project_or_timeline(
    executor: ResolveExecutor, settings: Settings
) -> None:
    resolve = make_resolve(with_project=False)
    mgr = _manager(
        [FakeBackend(resolve)], executor, settings.connection, process_probe=lambda: RESOLVE_ARGV
    )
    session = await mgr.session()
    with pytest.raises(DavinciError) as info:
        await executor.run(lambda: session.project)
    assert info.value.code is ErrorCode.WRONG_STATE

    resolve2 = make_resolve(timelines=0)
    mgr2 = _manager(
        [FakeBackend(resolve2)], executor, settings.connection, process_probe=lambda: RESOLVE_ARGV
    )
    session2 = await mgr2.session()
    with pytest.raises(DavinciError) as info2:
        await executor.run(lambda: session2.timeline)
    assert info2.value.code is ErrorCode.WRONG_STATE


async def test_headless_detected(executor: ResolveExecutor, settings: Settings) -> None:
    mgr = _manager(
        [FakeBackend(make_resolve())],
        executor,
        settings.connection,
        process_probe=lambda: [*RESOLVE_ARGV, "-nogui"],
    )
    assert (await mgr.session()).caps.headless is True


async def test_stuck_database(connection: ConnectionManager, fake_resolve: FakeResolve) -> None:
    fake_resolve.project_manager.stuck = True
    with pytest.raises(DavinciError) as info:
        await connection.session()
    assert info.value.code is ErrorCode.INSTANCE_STUCK
    assert info.value.retryable is False


async def test_backend_returns_none(executor: ResolveExecutor, settings: Settings) -> None:
    mgr = _manager(
        [FakeBackend(None)], executor, settings.connection, process_probe=lambda: RESOLVE_ARGV
    )
    with pytest.raises(DavinciError) as info:
        await mgr.session()
    assert info.value.code is ErrorCode.NOT_CONNECTED


async def test_auto_launch_disabled_no_process(executor: ResolveExecutor) -> None:
    mgr = _manager(
        [FakeBackend(make_resolve())],
        executor,
        ConnectionSettings(auto_launch=False),
        process_probe=lambda: None,
    )
    with pytest.raises(DavinciError) as info:
        await mgr.session()
    assert info.value.code is ErrorCode.RESOLVE_NOT_RUNNING


async def test_no_launcher_configured(executor: ResolveExecutor) -> None:
    mgr = _manager(
        [FakeBackend(make_resolve())], executor, ConnectionSettings(), process_probe=lambda: None
    )
    with pytest.raises(DavinciError) as info:
        await mgr.session()
    assert info.value.code is ErrorCode.RESOLVE_NOT_RUNNING


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep

    async def fast(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("davinci_mcp.connection.manager.asyncio.sleep", fast)


async def test_launcher_called_then_probe_finds_process(
    executor: ResolveExecutor, fast_sleep: None
) -> None:
    probes = iter([None, None, RESOLVE_ARGV, RESOLVE_ARGV])
    launched: list[bool] = []
    mgr = _manager(
        [FakeBackend(make_resolve())],
        executor,
        ConnectionSettings(headless=True),
        process_probe=lambda: next(probes),
        launcher=launched.append,
    )
    session = await mgr.session()
    assert launched == [True]
    assert session.caps.edition == "studio"


async def test_launch_timeout(executor: ResolveExecutor, fast_sleep: None) -> None:
    mgr = _manager(
        [FakeBackend(make_resolve())],
        executor,
        ConnectionSettings(launch_timeout_s=0.05),
        process_probe=lambda: None,
        launcher=lambda _h: None,
    )
    with pytest.raises(DavinciError) as info:
        await mgr.session()
    assert info.value.code is ErrorCode.RESOLVE_NOT_RUNNING


async def test_launch_via_paths(
    executor: ResolveExecutor, fast_sleep: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        "davinci_mcp.connection.manager.launch_resolve",
        lambda paths, *, headless: calls.append((paths, headless)),
    )
    paths = ResolvePaths.__new__(ResolvePaths)
    probes = iter([None, RESOLVE_ARGV, RESOLVE_ARGV])
    mgr = _manager(
        [FakeBackend(make_resolve())],
        executor,
        ConnectionSettings(),
        paths,
        process_probe=lambda: next(probes),
    )
    await mgr.session()
    assert calls == [(paths, False)]


async def test_fallback_to_second_backend(executor: ResolveExecutor, settings: Settings) -> None:
    first = FailingBackend()
    second = FakeBackend(make_resolve())
    mgr = _manager(
        [first, second], executor, settings.connection, process_probe=lambda: RESOLVE_ARGV
    )
    session = await mgr.session()
    assert first.connects == 1
    assert session.caps.channel == "external"


async def test_all_backends_fail(executor: ResolveExecutor, settings: Settings) -> None:
    mgr = _manager(
        [FailingBackend()], executor, settings.connection, process_probe=lambda: RESOLVE_ARGV
    )
    with pytest.raises(DavinciError) as info:
        await mgr.session()
    assert info.value.code is ErrorCode.NOT_CONNECTED
    assert info.value.details["process_running"] is True
    assert info.value.details["attempts"] == ["bridge-python: boom"]


async def test_call_success(connection: ConnectionManager) -> None:
    result = await connection.call(lambda s: s.project.GetName(), retry_safe=True)
    assert result == "Test Project"


async def test_call_ordinary_exception_while_alive(connection: ConnectionManager) -> None:
    def fn(_s: ResolveSession) -> None:
        raise KeyError("x")

    with pytest.raises(DavinciError) as info:
        await connection.call(fn, retry_safe=True)
    assert info.value.code is ErrorCode.API_CALL_FAILED
    assert connection.connected is True


async def test_call_davinci_error_passthrough(connection: ConnectionManager) -> None:
    def fn(s: ResolveSession) -> None:
        s.caps.require_studio("x") if False else s.caps.require(s.resolve, "Nope")

    with pytest.raises(DavinciError) as info:
        await connection.call(fn, retry_safe=True)
    assert info.value.code is ErrorCode.CAPABILITY_UNAVAILABLE


async def test_lost_connection_read_reconnects_and_retries(
    connection: ConnectionManager, backend: FakeBackend, fake_resolve: FakeResolve
) -> None:
    attempts: list[int] = []
    fake_resolve.dead_mode = "none"

    def fn(s: ResolveSession) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            fake_resolve.dead = True
            raise RuntimeError("connection lost")
        return "ok"

    def revive() -> None:
        fake_resolve.dead = False

    # Resolve "restarts" as soon as the manager tries to reconnect.
    original_connect = backend.connect

    def connect() -> Any:
        revive()
        return original_connect()

    backend.connect = connect  # type: ignore[method-assign]
    assert await connection.call(fn, retry_safe=True) == "ok"
    assert len(attempts) == 2
    assert backend.connects == 2


async def test_lost_connection_write_not_replayed(
    connection: ConnectionManager, fake_resolve: FakeResolve
) -> None:
    attempts: list[int] = []
    fake_resolve.dead_mode = "none"

    def fn(s: ResolveSession) -> None:
        attempts.append(1)
        fake_resolve.dead = True
        raise RuntimeError("connection lost")

    with pytest.raises(DavinciError) as info:
        await connection.call(fn, retry_safe=False)
    assert info.value.code is ErrorCode.CONNECTION_LOST
    assert attempts == [1]
    assert connection.connected is False


async def test_check_alive_none_resolve() -> None:
    with pytest.raises(DavinciError) as info:
        check_alive(None)
    assert info.value.code is ErrorCode.CONNECTION_LOST


async def test_suspect_recheck_clears_when_alive(
    connection: ConnectionManager, executor: ResolveExecutor
) -> None:
    session = await connection.session()
    executor._suspect = True
    assert await connection.session() is session
    assert executor.suspect is False


async def test_suspect_recheck_reconnects_when_dead(
    connection: ConnectionManager,
    executor: ResolveExecutor,
    fake_resolve: FakeResolve,
    backend: FakeBackend,
) -> None:
    first = await connection.session()
    fake_resolve.dead = True
    fake_resolve.dead_mode = "none"
    executor._suspect = True
    original = backend.connect

    def connect() -> Any:
        fake_resolve.dead = False
        return original()

    backend.connect = connect  # type: ignore[method-assign]
    second = await connection.session()
    assert second is not first
    assert backend.connects == 2
