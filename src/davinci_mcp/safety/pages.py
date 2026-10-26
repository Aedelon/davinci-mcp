# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Restore Resolve's current page after operations that need to switch pages."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from types import TracebackType
from typing import Literal

from davinci_mcp.connection.backend import ResolveObject
from davinci_mcp.errors import DavinciError, ErrorCode

Page = Literal["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"]


def _restore(resolve: ResolveObject, original: str | None) -> None:
    if not original:
        return
    with contextlib.suppress(Exception):
        if resolve.GetCurrentPage() != original:
            resolve.OpenPage(original)


class PageGuard:
    """Switch to ``target`` (if given) and restore the original page on exit."""

    def __init__(self, resolve: ResolveObject, target: Page | None) -> None:
        self._resolve = resolve
        self._target = target
        self._original: str | None = None

    def __enter__(self) -> PageGuard:
        self._original = self._resolve.GetCurrentPage()
        if (
            self._target is not None
            and self._target != self._original
            and not self._resolve.OpenPage(self._target)
        ):
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                f"Could not switch Resolve to the '{self._target}' page.",
                details={"current_page": self._original},
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _restore(self._resolve, self._original)


@contextlib.contextmanager
def restore_page_after(resolve: ResolveObject) -> Generator[None]:
    """Only restore the page on exit (for getters with hidden page side effects)."""
    original: str | None = None
    with contextlib.suppress(Exception):
        original = resolve.GetCurrentPage()
    try:
        yield
    finally:
        _restore(resolve, original)
