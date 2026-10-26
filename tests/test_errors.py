# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for davinci_mcp.errors."""

from __future__ import annotations

import pytest

from davinci_mcp.errors import DavinciError, ErrorCode


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.RESOLVE_NOT_RUNNING,
        ErrorCode.NOT_CONNECTED,
        ErrorCode.CONNECTION_LOST,
        ErrorCode.WRONG_STATE,
        ErrorCode.CONFIRMATION_STALE,
        ErrorCode.TIMEOUT,
    ],
)
def test_retryable_by_default(code: ErrorCode) -> None:
    assert DavinciError(code, "x").retryable is True


@pytest.mark.parametrize("code", [ErrorCode.INSTANCE_STUCK, ErrorCode.INVALID_PARAMS])
def test_not_retryable_by_default(code: ErrorCode) -> None:
    assert DavinciError(code, "x").retryable is False


def test_retryable_override() -> None:
    assert DavinciError(ErrorCode.TIMEOUT, "x", retryable=False).retryable is False


def test_payload_roundtrip() -> None:
    err = DavinciError(ErrorCode.NOT_FOUND, "nope", hint="h", next_call="n", details={"a": 1})
    payload = err.to_payload()
    assert payload.code is ErrorCode.NOT_FOUND
    assert payload.hint == "h"
    assert payload.next_call == "n"
    assert payload.details == {"a": 1}
    assert str(err) == "nope"
    assert "NOT_FOUND" in repr(err)
