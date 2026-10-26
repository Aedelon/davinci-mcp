# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Typed, actionable errors returned to MCP clients as ``isError`` results."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    RESOLVE_NOT_RUNNING = "RESOLVE_NOT_RUNNING"
    NOT_CONNECTED = "NOT_CONNECTED"
    CONNECTION_LOST = "CONNECTION_LOST"
    INSTANCE_STUCK = "INSTANCE_STUCK"
    STUDIO_REQUIRED = "STUDIO_REQUIRED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    EXTRAS_MISSING = "EXTRAS_MISSING"
    INVALID_PARAMS = "INVALID_PARAMS"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    WRONG_STATE = "WRONG_STATE"
    API_CALL_FAILED = "API_CALL_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_STALE = "CONFIRMATION_STALE"
    CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    TIMEOUT = "TIMEOUT"
    INTERNAL = "INTERNAL"


_RETRYABLE_BY_DEFAULT: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RESOLVE_NOT_RUNNING,
        ErrorCode.NOT_CONNECTED,
        ErrorCode.CONNECTION_LOST,
        ErrorCode.WRONG_STATE,
        ErrorCode.CONFIRMATION_STALE,
        ErrorCode.TIMEOUT,
    }
)


class ErrorPayload(BaseModel):
    """Structured content of an error result."""

    code: ErrorCode
    message: str
    hint: str | None = None
    retryable: bool
    next_call: str | None = Field(
        default=None, description="Suggested tool call to recover, e.g. 'resolve_status'."
    )
    details: dict[str, Any] = Field(default_factory=dict)


class DavinciError(Exception):
    """Raised anywhere in the server; converted to an ``isError`` tool result."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool | None = None,
        next_call: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = code in _RETRYABLE_BY_DEFAULT if retryable is None else retryable
        self.next_call = next_call
        self.details = details or {}

    def to_payload(self) -> ErrorPayload:
        return ErrorPayload(
            code=self.code,
            message=self.message,
            hint=self.hint,
            retryable=self.retryable,
            next_call=self.next_call,
            details=self.details,
        )

    def __repr__(self) -> str:
        return f"DavinciError({self.code}, {self.message!r})"
