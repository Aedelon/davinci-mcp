# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for consent tokens."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.safety.consent import ConsentManager


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _mgr(clock: Clock) -> ConsentManager:
    return ConsentManager(b"s" * 32, 60.0, clock)


def _issue(m: ConsentManager) -> str:
    return m.issue("delete", {"id": 1, "dry_run": True}, "fp1", "sum", ["e"], ["t"]).token


def test_roundtrip_ignores_confirm_and_dry_run() -> None:
    m = _mgr(Clock())
    token = _issue(m)
    m.verify(token, "delete", {"id": 1, "confirm_token": token}, "fp1")


def test_forged_token() -> None:
    m = _mgr(Clock())
    other = ConsentManager(b"x" * 32, 60.0, Clock())
    for bad in ("garbage", "", _issue(other)):
        with pytest.raises(DavinciError) as ei:
            m.verify(bad, "delete", {"id": 1}, "fp1")
        assert ei.value.code == ErrorCode.CONFIRMATION_INVALID


def test_expired() -> None:
    c = Clock()
    m = _mgr(c)
    token = _issue(m)
    c.now += 61
    with pytest.raises(DavinciError) as ei:
        m.verify(token, "delete", {"id": 1}, "fp1")
    assert ei.value.code == ErrorCode.CONFIRMATION_INVALID


def test_params_or_action_changed() -> None:
    m = _mgr(Clock())
    token = _issue(m)
    with pytest.raises(DavinciError) as ei:
        m.verify(token, "delete", {"id": 2}, "fp1")
    assert ei.value.code == ErrorCode.CONFIRMATION_INVALID
    with pytest.raises(DavinciError) as ei:
        m.verify(token, "other", {"id": 1}, "fp1")
    assert ei.value.code == ErrorCode.CONFIRMATION_INVALID


def test_stale_fingerprint() -> None:
    m = _mgr(Clock())
    token = _issue(m)
    with pytest.raises(DavinciError) as ei:
        m.verify(token, "delete", {"id": 1}, "fp2")
    assert ei.value.code == ErrorCode.CONFIRMATION_STALE


def test_from_state_dir_secret_persisted(tmp_path: Path) -> None:
    m1 = ConsentManager.from_state_dir(tmp_path / "state")
    m2 = ConsentManager.from_state_dir(tmp_path / "state")
    token = m1.issue("a", {}, "f", "s", [], []).token
    m2.verify(token, "a", {}, "f")
    key = tmp_path / "state" / "consent.key"
    if sys.platform != "win32":  # Windows uses ACLs; chmod only toggles read-only
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.stat().st_size == 32
