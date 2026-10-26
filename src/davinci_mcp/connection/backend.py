# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Backend protocol shared by the external scripting and in-app bridge channels."""

from __future__ import annotations

from typing import Any, Literal, Protocol

Channel = Literal["external", "bridge-python", "bridge-lua"]

# Resolve proxy objects are untyped remote handles (PyRemoteObject or bridge proxies).
type ResolveObject = Any


class Backend(Protocol):
    channel: Channel

    def connect(self) -> ResolveObject:
        """Return the root ``resolve`` object, or ``None`` when unreachable.

        Called on the resolve-api executor thread only.
        """
        ...

    def close(self) -> None: ...
