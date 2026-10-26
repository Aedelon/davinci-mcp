# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Write receipts: every mutation is read back and reported as verified or not.

Resolve frequently answers ``True`` without applying a change; a receipt whose
read-back disagrees is reported with ``verified=False`` instead of trusting it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Verification(BaseModel):
    ok: bool
    observed: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class Receipt(BaseModel):
    action: str
    kind: str
    result: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    verified: bool | None = Field(
        default=None, description="None when the action has no read-back (see verify_note)."
    )
    observed: dict[str, Any] = Field(default_factory=dict)
    verify_note: str | None = None
    snapshot_id: str | None = None
    channel: str
    duration_ms: int
    warnings: list[str] = Field(default_factory=list)
