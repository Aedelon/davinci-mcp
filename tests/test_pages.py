# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for page guards."""

# ruff: noqa: N802
from __future__ import annotations

import pytest

from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.safety.pages import PageGuard, restore_page_after


class FakeResolve:
    def __init__(self, page: str = "edit", can_open: bool = True) -> None:
        self.page = page
        self.can_open = can_open
        self.opened: list[str] = []
        self.fail_restore = False

    def GetCurrentPage(self) -> str:
        return self.page

    def OpenPage(self, name: str) -> bool:
        self.opened.append(name)
        if self.fail_restore and name == "edit":
            raise RuntimeError("x")
        if not self.can_open:
            return False
        self.page = name
        return True


def test_switch_and_restore() -> None:
    r = FakeResolve("edit")
    with PageGuard(r, "color"):
        assert r.page == "color"
    assert r.page == "edit"


def test_same_page_no_switch() -> None:
    r = FakeResolve("edit")
    with PageGuard(r, "edit"):
        pass
    assert r.opened == []


def test_restore_on_exception_not_masked() -> None:
    r = FakeResolve("edit")
    with pytest.raises(ValueError, match="boom"), PageGuard(r, "deliver"):
        raise ValueError("boom")
    assert r.page == "edit"


def test_open_failure_wrong_state() -> None:
    r = FakeResolve("edit", can_open=False)
    with pytest.raises(DavinciError) as ei, PageGuard(r, "color"):
        pass
    assert ei.value.code == ErrorCode.WRONG_STATE


def test_restore_failure_is_swallowed() -> None:
    r = FakeResolve("edit")
    r.fail_restore = True
    with pytest.raises(ValueError, match="boom"), PageGuard(r, "color"):
        raise ValueError("boom")


def test_restore_page_after() -> None:
    r = FakeResolve("edit")
    with restore_page_after(r):
        r.page = "deliver"
    assert r.page == "edit"
