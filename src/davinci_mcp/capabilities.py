# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Runtime capability detection.

``hasattr`` is always True on Resolve proxies and ``getattr`` returns None for
unknown names, so method presence is decided by ``dir()`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from davinci_mcp.errors import DavinciError, ErrorCode

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import Channel, ResolveObject

Edition = Literal["studio", "free"]
type Version = tuple[int, int, int]


def proxy_class(obj: ResolveObject) -> str:
    """Resolve proxies stringify as 'Timeline (0x...) [App: ...]'."""
    text = str(obj)
    return text.split(" ", 1)[0] if text and not text.startswith("<") else "Unknown"


class CapabilityInfo(BaseModel):
    product: str
    version: str
    version_tuple: tuple[int, int, int]
    edition: Edition
    channel: str
    headless: bool
    database: dict[str, str] | None
    known_missing_extras: list[str]


@dataclass
class Capabilities:
    product: str
    version_string: str
    version: Version
    edition: Edition
    channel: Channel
    headless: bool
    database: dict[str, str] | None = None
    missing_extras: set[str] = field(default_factory=set[str])
    _dir_cache: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])

    @classmethod
    def detect(cls, resolve: ResolveObject, channel: Channel, *, headless: bool) -> Capabilities:
        """Run on the executor thread."""
        product: str = resolve.GetProductName() or "DaVinci Resolve"
        raw = resolve.GetVersion() or [0, 0, 0]
        version = (int(raw[0]), int(raw[1]), int(raw[2]))
        names = set(dir(resolve))
        if "IsStudio" in names:
            edition: Edition = "studio" if resolve.IsStudio() else "free"
        else:
            edition = "studio" if "Studio" in product else "free"
        caps = cls(
            product=product,
            version_string=resolve.GetVersionString() or ".".join(map(str, version)),
            version=version,
            edition=edition,
            channel=channel,
            headless=headless,
        )
        caps._dir_cache["Resolve"] = frozenset(names)
        return caps

    def methods(self, obj: ResolveObject) -> frozenset[str]:
        key = proxy_class(obj)
        cached = self._dir_cache.get(key)
        if cached is None or key == "Unknown":
            cached = frozenset(n for n in dir(obj) if not n.startswith("_"))
            if key != "Unknown":
                self._dir_cache[key] = cached
        return cached

    def has(self, obj: ResolveObject, method: str) -> bool:
        return method in self.methods(obj)

    def require(self, obj: ResolveObject, method: str, *, since: Version | None = None) -> None:
        if self.has(obj, method):
            return
        need = f" (requires Resolve {'.'.join(map(str, since))}+)" if since else ""
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            f"{proxy_class(obj)}.{method} is not available in Resolve {self.version_string}{need}.",
            retryable=False,
            details={"method": f"{proxy_class(obj)}.{method}", "version": self.version_string},
        )

    def require_studio(self, feature: str) -> None:
        if self.edition != "studio":
            raise DavinciError(
                ErrorCode.STUDIO_REQUIRED,
                f"{feature} requires DaVinci Resolve Studio.",
                hint="Studio-only calls on the free edition also open an upsell modal "
                "that breaks subsequent calls.",
                retryable=False,
            )

    def at_least(self, version: Version) -> bool:
        return self.version >= version

    def info(self) -> CapabilityInfo:
        return CapabilityInfo(
            product=self.product,
            version=self.version_string,
            version_tuple=self.version,
            edition=self.edition,
            channel=self.channel,
            headless=self.headless,
            database=self.database,
            known_missing_extras=sorted(self.missing_extras),
        )
