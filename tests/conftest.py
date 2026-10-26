# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Shared fixtures: fake Resolve, settings, executor and connection manager."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.config import Settings, load_settings
from davinci_mcp.connection.manager import ConnectionManager
from davinci_mcp.executor import ResolveExecutor
from tests.fakes.fake_resolve import FakeResolve, make_resolve

RESOLVE_ARGV = ["/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"]


class FakeBackend:
    channel = "external"

    def __init__(self, resolve: Any) -> None:
        self.resolve = resolve
        self.connects = 0
        self.closed = False

    def connect(self) -> Any:
        self.connects += 1
        return self.resolve

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_resolve() -> FakeResolve:
    return make_resolve()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    state = tmp_path / "state"
    # state_dir has no env override; it derives from XDG_STATE_HOME at construction time.
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return load_settings(
        path=tmp_path / "none.toml",
        environ={
            "DAVINCI_MCP_SAFETY__SNAPSHOT_DIR": str(state / "snapshots"),
            "DAVINCI_MCP_SAFETY__ALLOWED_ROOTS": f"[{tmp_path}]",
        },
    )


@pytest.fixture
def executor() -> Iterator[ResolveExecutor]:
    ex = ResolveExecutor(default_timeout_s=5.0)
    yield ex
    ex.shutdown()


@pytest.fixture
def backend(fake_resolve: FakeResolve) -> FakeBackend:
    return FakeBackend(fake_resolve)


@pytest.fixture
def connection(
    backend: FakeBackend, executor: ResolveExecutor, settings: Settings
) -> ConnectionManager:
    return ConnectionManager(
        [backend],  # type: ignore[list-item]
        executor,
        settings.connection,
        process_probe=lambda: list(RESOLVE_ARGV),
    )


@pytest.fixture
def run_on_executor(executor: ResolveExecutor) -> Callable[..., Awaitable[Any]]:
    async def _run(fn: Callable[[], Any], *, timeout: float | None = None) -> Any:
        return await executor.run(fn, timeout=timeout)

    return _run
