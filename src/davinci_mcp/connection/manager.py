# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Connection lifecycle: backend selection, liveness, reconnection, auto-launch."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from davinci_mcp.capabilities import Capabilities
from davinci_mcp.connection.external import find_resolve_process, is_headless, launch_resolve
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.executor import assert_resolve_thread

if TYPE_CHECKING:
    from davinci_mcp.config import ConnectionSettings
    from davinci_mcp.connection.backend import Backend, ResolveObject
    from davinci_mcp.connection.external import ResolvePaths
    from davinci_mcp.executor import ResolveExecutor

type ProcessProbe = Callable[[], list[str] | None]
type Launcher = Callable[[bool], None]


@dataclass
class ResolveSession:
    """Handle given to actions. Only valid on the resolve-api executor thread."""

    resolve: ResolveObject
    caps: Capabilities

    @property
    def project_manager(self) -> ResolveObject:
        assert_resolve_thread()
        return self.resolve.GetProjectManager()

    @property
    def project(self) -> ResolveObject:
        assert_resolve_thread()
        project = self.project_manager.GetCurrentProject()
        if project is None:
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                "No project is open in Resolve.",
                next_call="project_read(action='list')",
            )
        return project

    @property
    def timeline(self) -> ResolveObject:
        timeline = self.project.GetCurrentTimeline()
        if timeline is None:
            raise DavinciError(ErrorCode.WRONG_STATE, "The current project has no active timeline.")
        return timeline


def check_alive(resolve: ResolveObject) -> dict[str, str]:
    """Real liveness test: a stuck instance answers everything except GetCurrentDatabase."""
    pm = resolve.GetProjectManager() if resolve is not None else None
    if pm is None:
        raise DavinciError(ErrorCode.CONNECTION_LOST, "Resolve stopped answering.")
    database = pm.GetCurrentDatabase()
    if database is None:
        raise DavinciError(
            ErrorCode.INSTANCE_STUCK,
            "Resolve is running but its project database is unreachable.",
            hint="This happens after an unclean shutdown; only restarting Resolve fixes it.",
            retryable=False,
        )
    return {str(k): str(v) for k, v in dict(database).items()}


class ConnectionManager:
    def __init__(
        self,
        backends: Sequence[Backend],
        executor: ResolveExecutor,
        settings: ConnectionSettings,
        paths: ResolvePaths | None = None,
        *,
        process_probe: ProcessProbe = find_resolve_process,
        launcher: Launcher | None = None,
    ) -> None:
        if not backends:
            raise ValueError("at least one backend is required")
        self._backends = list(backends)
        self._executor = executor
        self._settings = settings
        self._paths = paths
        self._probe = process_probe
        self._launcher = launcher
        self._session: ResolveSession | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def session(self) -> ResolveSession:
        if self._session is not None and not self._executor.suspect:
            return self._session
        async with self._lock:
            if self._session is not None and self._executor.suspect:
                await self._recheck()
            if self._session is None:
                self._session = await self._connect()
            return self._session

    async def _recheck(self) -> None:
        assert self._session is not None
        resolve = self._session.resolve
        try:
            await self._executor.run(lambda: check_alive(resolve), timeout=5.0)
            self._executor.clear_suspect()
        except Exception:  # dead proxies raise arbitrary errors
            self._session = None

    async def invalidate(self) -> None:
        async with self._lock:
            self._session = None

    async def _connect(self) -> ResolveSession:
        argv = self._probe()
        if argv is None:
            await self._maybe_launch()
            argv = self._probe()
        headless = is_headless(argv)
        errors: list[str] = []
        for backend in self._backends:
            try:
                return await self._executor.run(
                    lambda b=backend: self._open(b, headless=headless),
                    timeout=self._settings.call_timeout_s,
                )
            except DavinciError as exc:
                if exc.code is ErrorCode.INSTANCE_STUCK:
                    raise
                errors.append(f"{backend.channel}: {exc.message}")
            except Exception as exc:  # broken module / transport: try the next channel
                errors.append(f"{backend.channel}: {type(exc).__name__}: {exc}")
        raise DavinciError(
            ErrorCode.NOT_CONNECTED,
            "Could not reach DaVinci Resolve through any channel.",
            hint="Studio: Preferences > General > External scripting using = Local. "
            "Free edition: start the davinci-mcp bridge from Workspace > Scripts.",
            details={"attempts": errors, "process_running": argv is not None},
        )

    @staticmethod
    def _open(backend: Backend, *, headless: bool) -> ResolveSession:
        resolve = backend.connect()
        if resolve is None:
            raise DavinciError(ErrorCode.NOT_CONNECTED, "scriptapp('Resolve') returned None.")
        database = check_alive(resolve)
        caps = Capabilities.detect(resolve, backend.channel, headless=headless)
        caps.database = database
        return ResolveSession(resolve=resolve, caps=caps)

    async def _maybe_launch(self) -> None:
        if not self._settings.auto_launch:
            raise DavinciError(
                ErrorCode.RESOLVE_NOT_RUNNING,
                "DaVinci Resolve is not running and auto-launch is disabled.",
                hint="Start Resolve, or set connection.auto_launch = true.",
                next_call="resolve_status",
            )
        if self._launcher is not None:
            self._launcher(self._settings.headless)
        elif self._paths is not None:
            launch_resolve(self._paths, headless=self._settings.headless)
        else:
            raise DavinciError(ErrorCode.RESOLVE_NOT_RUNNING, "No launcher configured.")
        deadline = time.monotonic() + self._settings.launch_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            if self._probe() is not None:
                # The process exists before scripting is ready; give it a moment.
                await asyncio.sleep(5.0)
                return
        raise DavinciError(
            ErrorCode.RESOLVE_NOT_RUNNING,
            f"Resolve did not start within {self._settings.launch_timeout_s:.0f}s.",
        )

    async def call[T](
        self, fn: Callable[[ResolveSession], T], *, retry_safe: bool, timeout: float | None = None
    ) -> T:
        """Run ``fn`` on the executor; on a lost connection, reconnect and retry once if safe."""
        session = await self.session()
        try:
            return await self._executor.run(lambda: fn(session), timeout=timeout)
        except DavinciError:
            raise
        except Exception as exc:
            alive = await self._is_alive(session)
            if alive:
                raise DavinciError(
                    ErrorCode.API_CALL_FAILED,
                    f"Resolve API call raised {type(exc).__name__}: {exc}",
                    retryable=False,
                ) from exc
            await self.invalidate()
            if not retry_safe:
                raise DavinciError(
                    ErrorCode.CONNECTION_LOST,
                    "Connection to Resolve was lost during a write; it was not replayed.",
                    hint="Re-read the state before retrying.",
                    next_call="resolve_status",
                ) from exc
            session = await self.session()
            return await self._executor.run(lambda: fn(session), timeout=timeout)

    async def _is_alive(self, session: ResolveSession) -> bool:
        resolve = session.resolve
        try:
            await self._executor.run(lambda: check_alive(resolve), timeout=5.0)
        except Exception:  # dead proxies raise arbitrary errors
            return False
        return True
