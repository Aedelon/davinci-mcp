# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Single-thread executor that owns every call into the Resolve scripting API.

The Resolve API is not thread-safe. Every call, including job polling, goes
through ``ResolveExecutor.run``; nothing touches Resolve objects from the
asyncio loop or from FastMCP worker threads.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from davinci_mcp.errors import DavinciError, ErrorCode

THREAD_NAME = "resolve-api"


def assert_resolve_thread() -> None:
    """Guard used by Resolve accessors; raises when called off the executor thread."""
    if not threading.current_thread().name.startswith(THREAD_NAME):
        raise DavinciError(
            ErrorCode.INTERNAL,
            "Resolve API accessed outside the resolve-api executor thread.",
            retryable=False,
        )


class ResolveExecutor:
    def __init__(self, default_timeout_s: float = 30.0) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_NAME)
        self._default_timeout = default_timeout_s
        self._suspect = False
        self._closed = False

    @property
    def suspect(self) -> bool:
        """True after a timeout: the last call may still be running inside Resolve."""
        return self._suspect

    def clear_suspect(self) -> None:
        self._suspect = False

    async def run[T](self, fn: Callable[[], T], *, timeout: float | None = None) -> T:
        if self._closed:
            raise DavinciError(ErrorCode.INTERNAL, "Executor is shut down.", retryable=False)
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, fn)
        limit = self._default_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(future, timeout=limit)
        except TimeoutError:
            # The call cannot be interrupted; flag the instance until a health check clears it.
            self._suspect = True
            raise DavinciError(
                ErrorCode.TIMEOUT,
                f"Resolve did not answer within {limit:.0f}s.",
                hint="Resolve may be blocked by a modal dialog or a long operation.",
                next_call="resolve_status",
            ) from None

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
