# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Opt-in test against a real running DaVinci Resolve Studio (pytest -m live)."""

from __future__ import annotations

import pytest

from davinci_mcp.connection.external import ExternalBackend, resolve_paths
from davinci_mcp.executor import ResolveExecutor


@pytest.mark.live
async def test_live_version() -> None:
    executor = ResolveExecutor(default_timeout_s=30.0)
    backend = ExternalBackend(resolve_paths())
    try:
        resolve = await executor.run(backend.connect)
        assert resolve is not None
        raw = await executor.run(resolve.GetVersion)
        assert tuple(int(x) for x in raw[:3]) >= (20, 0, 0)
    finally:
        backend.close()
        executor.shutdown()
