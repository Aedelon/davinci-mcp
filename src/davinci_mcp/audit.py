# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Append-only JSONL audit log of every write and destructive action."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MAX_BYTES = 10 * 1024 * 1024


class AuditLog:
    def __init__(self, path: Path, max_bytes: int = _MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: dict[str, Any]) -> None:
        line = json.dumps(
            {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **event},
            default=str,
            ensure_ascii=False,
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists() and self._path.stat().st_size > self._max_bytes:
                self._path.replace(self._path.with_suffix(".1.jsonl"))
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines if line.strip()]
