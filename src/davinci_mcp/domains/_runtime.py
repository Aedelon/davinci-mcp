# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Process-wide services available to domain handlers.

Handlers are plain functions running on the executor thread; they reach
settings, the read cache and the snapshot manager through ``runtime()``.
The server binds it once at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from davinci_mcp.cache import ReadCache
    from davinci_mcp.config import Settings
    from davinci_mcp.safety.snapshots import SnapshotManager


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    cache: ReadCache
    snapshots: SnapshotManager

    @property
    def allowed_roots(self) -> list[Path]:
        return self.settings.safety.allowed_roots


_runtime: Runtime | None = None


def bind(runtime_: Runtime) -> None:
    global _runtime  # noqa: PLW0603 - single process-wide runtime
    _runtime = runtime_


def runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("domain runtime not bound; build the server first")
    return _runtime
