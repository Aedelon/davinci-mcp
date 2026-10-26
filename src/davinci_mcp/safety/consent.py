# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Stateless HMAC consent tokens binding a dry-run plan to a later confirmed call."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from davinci_mcp.errors import DavinciError, ErrorCode

_IGNORED_PARAMS = frozenset({"confirm_token", "dry_run"})
_FP_LEN = 16
_MAC_LEN = 32
_EXP_LEN = 8
_SECRET_LEN = 32


class Plan(BaseModel):
    """Dry-run result describing what a destructive action would do."""

    action: str
    summary: str
    effects: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    fingerprint: str
    token: str
    expires_at: str


def _canonical(action: str, params: Mapping[str, Any], fp_hash: bytes, expiry_ms: int) -> bytes:
    clean = {k: v for k, v in params.items() if k not in _IGNORED_PARAMS}
    doc = {
        "action": action,
        "params": clean,
        "fp": fp_hash.hex(),
        "expiry": expiry_ms,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()


def _fp_hash(fingerprint: str) -> bytes:
    return hashlib.sha256(fingerprint.encode()).digest()[:_FP_LEN]


class ConsentManager:
    """Issues and verifies tokens. Token = b64(expiry_ms || sha256(fingerprint)[:16] || HMAC)."""

    def __init__(self, secret: bytes, ttl_s: float, clock: Callable[[], float] = time.time) -> None:
        self._secret = secret
        self._ttl_s = ttl_s
        self._clock = clock

    @classmethod
    def from_state_dir(
        cls, path: Path, ttl_s: float = 300.0, clock: Callable[[], float] = time.time
    ) -> ConsentManager:
        """Create or read a 32-byte secret (mode 0600) in ``path``."""
        path.mkdir(parents=True, exist_ok=True)
        secret_file = path / "consent.key"
        if secret_file.exists() and secret_file.stat().st_size == _SECRET_LEN:
            secret = secret_file.read_bytes()
        else:
            secret = os.urandom(_SECRET_LEN)
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(secret)
        secret_file.chmod(0o600)
        return cls(secret, ttl_s, clock)

    def _mac(self, action: str, params: Mapping[str, Any], fp_hash: bytes, expiry_ms: int) -> bytes:
        return hmac.new(
            self._secret, _canonical(action, params, fp_hash, expiry_ms), hashlib.sha256
        ).digest()

    def issue(  # noqa: PLR0917
        self,
        action: str,
        params: Mapping[str, Any],
        fingerprint: str,
        summary: str,
        effects: Sequence[str] = (),
        targets: Sequence[str] = (),
    ) -> Plan:
        expiry_ms = int((self._clock() + self._ttl_s) * 1000)
        fp = _fp_hash(fingerprint)
        mac = self._mac(action, params, fp, expiry_ms)
        raw = struct.pack(">Q", expiry_ms) + fp + mac
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return Plan(
            action=action,
            summary=summary,
            effects=list(effects),
            targets=list(targets),
            fingerprint=fingerprint,
            token=token,
            expires_at=datetime.fromtimestamp(expiry_ms / 1000, UTC).isoformat(),
        )

    def verify(
        self,
        token: str,
        action: str,
        params: Mapping[str, Any],
        current_fingerprint: str,
    ) -> None:
        invalid = DavinciError(
            ErrorCode.CONFIRMATION_INVALID,
            "Confirmation token is invalid, expired, or does not match these parameters.",
            hint="Call again with dry_run=true to get a fresh plan and token.",
        )
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (binascii.Error, ValueError):
            raise invalid from None
        if len(raw) != _EXP_LEN + _FP_LEN + _MAC_LEN:
            raise invalid
        (expiry_ms,) = struct.unpack(">Q", raw[:_EXP_LEN])
        fp = raw[_EXP_LEN : _EXP_LEN + _FP_LEN]
        mac = raw[_EXP_LEN + _FP_LEN :]
        expected = self._mac(action, params, fp, expiry_ms)
        if not hmac.compare_digest(mac, expected):
            raise invalid
        if self._clock() * 1000 > expiry_ms:
            raise invalid
        if not hmac.compare_digest(fp, _fp_hash(current_fingerprint)):
            raise DavinciError(
                ErrorCode.CONFIRMATION_STALE,
                "Resolve state changed since the plan was issued.",
                hint="Call again with dry_run=true to review the new plan.",
            )
