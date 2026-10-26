# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""UI domain tests: fake backend and fake runner only, never the real UI."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from davinci_mcp.config import Settings
from davinci_mcp.domains import ui
from davinci_mcp.domains.ui import ACTIONS, MacUiBackend, UnsupportedUiBackend
from davinci_mcp.errors import DavinciError, ErrorCode
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, confirm, mcp_client


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.menus: dict[tuple[str, ...], list[str]] = {
            (): ["File", "Edit"],
            ("Edit",): ["Undo", "Redo"],
        }
        self.ok = True

    def available(self) -> bool:
        return self.ok

    def front_app(self) -> str:
        self.calls.append(("front_app", None))
        return "Terminal"

    def activate(self) -> None:
        self.calls.append(("activate", None))

    def list_menu(self, path: list[str]) -> list[str]:
        self.calls.append(("list_menu", path))
        return self.menus.get(tuple(path), [])

    def menu(self, path: list[str]) -> None:
        self.calls.append(("menu", path))

    def keystroke(self, key: str, modifiers: list[str]) -> None:
        self.calls.append(("keystroke", (key, modifiers)))


@pytest.fixture
def backend() -> Iterator[FakeBackend]:
    b = FakeBackend()
    ui.set_backend(b)
    yield b
    ui.set_backend(None)


def _runner(
    code: int = 0, out: str = "", err: str = ""
) -> tuple[list[list[str]], Callable[[list[str]], tuple[int, str, str]]]:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return code, out, err

    return seen, run


def test_osascript_strings() -> None:
    seen, run = _runner(out="Edit\nTrim")
    mac = MacUiBackend(run)
    mac.menu(["Edit", "Undo"])
    mac.keystroke("b", ["command"])
    mac.keystroke("delete", ["shift"])
    assert mac.list_menu(["Edit"]) == ["Edit", "Trim"]
    assert mac.list_menu([]) == ["Edit", "Trim"]
    assert all(a[:2] == ["osascript", "-e"] for a in seen)
    assert 'click menu item "Undo" of menu 1 of menu bar item "Edit" of menu bar 1' in seen[0][2]
    assert 'tell process "Resolve"' in seen[0][2]
    assert 'keystroke "b" using {command down}' in seen[1][2]
    assert "key code 51 using {shift down}" in seen[2][2]
    assert "menu items of menu 1 of menu bar item" in seen[3][2]
    assert "menu bar items of menu bar 1" in seen[4][2]


def test_quoting_and_nested_menu() -> None:
    seen, run = _runner()
    MacUiBackend(run).menu(["Workspace", "Video Scopes", 'On "x"'])
    script = seen[0][2]
    assert 'menu item "Video Scopes" of menu 1 of menu bar item "Workspace"' in script
    assert 'menu item "On \\"x\\""' in script


def test_accessibility_denied_hint() -> None:
    _, run = _runner(code=1, err="osascript is not allowed assistive access. (-1719)")
    with pytest.raises(DavinciError) as exc:
        MacUiBackend(run).activate()
    assert exc.value.code is ErrorCode.WRONG_STATE
    assert "Accessibility" in (exc.value.hint or "")


def test_other_osascript_error() -> None:
    _, run = _runner(code=1, err="boom")
    with pytest.raises(DavinciError) as exc:
        MacUiBackend(run).front_app()
    assert exc.value.code is ErrorCode.API_CALL_FAILED


def test_stub_backends() -> None:
    stub = UnsupportedUiBackend("linux")
    assert not stub.available()
    with pytest.raises(DavinciError) as exc:
        stub.menu(["a", "b"])
    assert exc.value.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert isinstance(ui.default_backend("win32"), UnsupportedUiBackend)
    assert isinstance(ui.default_backend("darwin"), MacUiBackend)


async def test_list_menu_items(settings: Settings, backend: FakeBackend) -> None:
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, out = await call(
            client, "ui_read", {"op": {"action": "list_menu_items", "path": ["Edit"]}}
        )
    assert not err
    assert "Undo" in str(out)


async def test_invoke_menu_validates(settings: Settings, backend: FakeBackend) -> None:
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, out = await call(
            client, "ui_write", {"op": {"action": "invoke_menu", "path": ["Edit", "Undo"]}}
        )
        assert not err
        assert ("menu", ["Edit", "Undo"]) in backend.calls
        assert [c[0] for c in backend.calls][:2] == ["front_app", "activate"]
        assert "no API read-back" in str(out)
        backend.calls.clear()
        err, out = await call(
            client, "ui_write", {"op": {"action": "invoke_menu", "path": ["Edit", "Nope"]}}
        )
        assert err
        assert not any(c[0] == "menu" for c in backend.calls)


async def test_send_shortcut_and_preset_warning(settings: Settings, backend: FakeBackend) -> None:
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, out = await call(
            client,
            "ui_write",
            {"op": {"action": "send_shortcut", "command": "blade", "keyboard_preset": "Mine"}},
        )
    assert not err
    assert ("keystroke", ("b", ["command"])) in backend.calls
    assert "Mine" in str(out)


async def test_unavailable_platform(settings: Settings, backend: FakeBackend) -> None:
    backend.ok = False
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, _ = await call(
            client, "ui_write", {"op": {"action": "send_shortcut", "command": "undo"}}
        )
    assert err
    assert not backend.calls


async def test_open_gallery_panel(settings: Settings, backend: FakeBackend) -> None:
    resolve = make_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "ui_write", {"op": {"action": "open_gallery_panel"}})
    assert not err
    assert "'page': 'color'" in str(out)
    assert ("menu", ["Workspace", "Gallery"]) in backend.calls


async def test_blade_reports_item_counts(settings: Settings, backend: FakeBackend) -> None:
    resolve = make_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        _, out = await call(client, "ui_write", {"op": {"action": "blade_at_playhead"}})
    assert ("keystroke", ("b", ["command"])) in backend.calls
    assert "items_after" in str(out)
    assert "page" in str(out)


async def test_ripple_delete_needs_confirmation(settings: Settings, backend: FakeBackend) -> None:
    resolve = make_resolve()
    args = {"op": {"action": "ripple_delete_selection"}}
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, planned = await call(client, "ui_delete", args)
        assert not err
        assert planned["status"] == "planned"
        assert not any(c[0] == "keystroke" for c in backend.calls)
        await confirm(client, "ui_delete", args)
    assert ("keystroke", ("delete", ["shift"])) in backend.calls
