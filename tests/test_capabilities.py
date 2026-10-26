# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Tests for davinci_mcp.capabilities."""

from __future__ import annotations

from typing import Any

import pytest

from davinci_mcp.capabilities import Capabilities, proxy_class
from davinci_mcp.errors import DavinciError, ErrorCode
from tests.fakes.fake_resolve import LIVE_DIR, FakeResolve


class NoStudioResolve(FakeResolve):
    """Resolve whose dir() lacks IsStudio, like older builds."""

    product = "DaVinci Resolve"

    def __dir__(self) -> list[str]:
        return [n for n in super().__dir__() if n != "IsStudio"]

    def GetProductName(self) -> str:
        return self.product


def test_detect_studio_via_is_studio(fake_resolve: FakeResolve) -> None:
    caps = Capabilities.detect(fake_resolve, "external", headless=False)
    assert caps.edition == "studio"
    assert caps.version == (21, 1, 1)
    assert caps.version_string == "21.1.1.10"
    assert caps.at_least((20, 0, 0))
    assert not caps.at_least((22, 0, 0))


def test_detect_free_via_is_studio(fake_resolve: FakeResolve) -> None:
    fake_resolve.IsStudio = lambda: False  # type: ignore[method-assign]
    assert Capabilities.detect(fake_resolve, "external", headless=False).edition == "free"


@pytest.mark.parametrize(
    ("product", "edition"), [("DaVinci Resolve Studio", "studio"), ("DaVinci Resolve", "free")]
)
def test_detect_fallback_on_product_name(product: str, edition: str) -> None:
    r = NoStudioResolve()
    r.product = product
    assert Capabilities.detect(r, "external", headless=False).edition == edition


def test_detect_defaults_when_api_returns_none() -> None:
    class Bare:
        def GetProductName(self) -> None: ...
        def GetVersion(self) -> None: ...
        def GetVersionString(self) -> None: ...

    caps = Capabilities.detect(Bare(), "bridge-python", headless=True)
    assert caps.version == (0, 0, 0)
    assert caps.version_string == "0.0.0"
    assert caps.edition == "free"
    assert caps.headless is True


def test_has_uses_dir_not_hasattr(fake_resolve: FakeResolve) -> None:
    caps = Capabilities.detect(fake_resolve, "external", headless=False)
    assert hasattr(fake_resolve, "NotARealMethod") is True
    assert caps.has(fake_resolve, "NotARealMethod") is False
    assert caps.has(fake_resolve, "GetProjectManager") is True


def test_methods_cached_per_class(fake_resolve: FakeResolve) -> None:
    caps = Capabilities.detect(fake_resolve, "external", headless=False)
    project = fake_resolve.GetProjectManager().GetCurrentProject()
    first = caps.methods(project)
    assert first == frozenset(LIVE_DIR["Project"])
    assert caps.methods(project) is first


def test_methods_unknown_class_not_cached(fake_resolve: FakeResolve) -> None:
    caps = Capabilities.detect(fake_resolve, "external", headless=False)

    class Odd:
        def __str__(self) -> str:
            return "<odd>"

        def foo(self) -> None: ...

    assert "foo" in caps.methods(Odd())
    assert "Unknown" not in caps._dir_cache


def test_require(fake_resolve: FakeResolve) -> None:
    caps = Capabilities.detect(fake_resolve, "external", headless=False)
    caps.require(fake_resolve, "GetVersion")
    with pytest.raises(DavinciError) as info:
        caps.require(fake_resolve, "Nope", since=(22, 0, 0))
    assert info.value.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert "22.0.0+" in info.value.message
    assert info.value.details["method"] == "Resolve.Nope"
    with pytest.raises(DavinciError):
        caps.require(fake_resolve, "Nope")


def test_require_studio(fake_resolve: FakeResolve) -> None:
    caps = Capabilities.detect(fake_resolve, "external", headless=False)
    caps.require_studio("Feature")
    caps.edition = "free"
    with pytest.raises(DavinciError) as info:
        caps.require_studio("Feature")
    assert info.value.code is ErrorCode.STUDIO_REQUIRED


def test_proxy_class(fake_resolve: FakeResolve) -> None:
    assert proxy_class(fake_resolve) == "Resolve"
    assert proxy_class(fake_resolve.GetProjectManager()) == "ProjectManager"
    assert proxy_class(object()) == "Unknown" or proxy_class(object()).startswith("<") is False
    assert proxy_class("<x>") == "Unknown"
    assert proxy_class("") == "Unknown"


def test_info(fake_resolve: FakeResolve) -> None:
    caps = Capabilities.detect(fake_resolve, "external", headless=False)
    caps.missing_extras.update({"b", "a"})
    info = caps.info()
    assert info.known_missing_extras == ["a", "b"]
    assert info.edition == "studio"
    assert info.version_tuple == (21, 1, 1)


def test_fake_proxy_semantics(fake_resolve: FakeResolve) -> None:
    attr: Any = fake_resolve.NotImplementedThing
    assert attr is None
    with pytest.raises(TypeError):
        fake_resolve.NotImplementedThing()
    assert str(fake_resolve).startswith("Resolve (0x")
