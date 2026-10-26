# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Short-lived read cache invalidated by a global write generation.

Any write action bumps the generation, so cached reads never outlive a mutation
made through this server. The TTL bounds staleness from edits made in the UI.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Entry:
    generation: int
    expires_at: float
    value: Any


@dataclass
class ReadCache:
    ttl_s: float = 3.0
    max_entries: int = 512
    clock: Callable[[], float] = time.monotonic
    _generation: int = 0
    _entries: dict[Hashable, _Entry] = field(default_factory=dict[Hashable, _Entry])

    @property
    def generation(self) -> int:
        return self._generation

    def bump(self) -> None:
        self._generation += 1
        self._entries.clear()

    def get_or_compute[T](self, key: Hashable, compute: Callable[[], T]) -> T:
        now = self.clock()
        entry = self._entries.get(key)
        if entry is not None and entry.generation == self._generation and entry.expires_at > now:
            return entry.value
        value = compute()
        if len(self._entries) >= self.max_entries:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = _Entry(self._generation, now + self.ttl_s, value)
        return value
