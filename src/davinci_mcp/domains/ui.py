# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""UI automation domain: fill API gaps by driving Resolve's menus and shortcuts.

macOS first (``osascript`` + System Events) behind an OS-agnostic ``UiBackend``.
Every OS side effect goes through an injectable ``Runner`` so tests never touch
the real UI. Shortcuts are documented for the *default keyboard preset only*.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification

if TYPE_CHECKING:
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "ui"
PROCESS_NAME = "Resolve"
APP_NAME = "DaVinci Resolve"
NO_READBACK = "UI automation has no API read-back"
_MIN_PATH = 2
_MODIFIERS = ("command", "shift", "option", "control")
_KEY_CODES = {"delete": 51, "return": 36, "tab": 48, "escape": 53, "space": 49}

type Runner = Callable[[list[str]], tuple[int, str, str]]
"""Runs an argv; returns (returncode, stdout, stderr)."""


def _subprocess_runner(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, stdin=subprocess.DEVNULL, check=False, capture_output=True, text=True, timeout=15
    )
    return proc.returncode, proc.stdout, proc.stderr


class UiBackend(Protocol):
    """OS-specific UI driver."""

    def available(self) -> bool: ...
    def front_app(self) -> str: ...
    def activate(self) -> None: ...
    def list_menu(self, path: list[str]) -> list[str]: ...
    def menu(self, path: list[str]) -> None: ...
    def keystroke(self, key: str, modifiers: list[str]) -> None: ...


def _q(text: str) -> str:
    """Quote a string for AppleScript."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _menu_ref(path: list[str]) -> str:
    """AppleScript reference to the menu named by the chain ``path``."""
    ref = f"menu bar item {_q(path[0])} of menu bar 1"
    for name in path[1:]:
        ref = f"menu item {_q(name)} of menu 1 of {ref}"
    return f"menu 1 of {ref}"


def _system_events(body: str) -> str:
    return f'tell application "System Events" to tell process {_q(PROCESS_NAME)}\n{body}\nend tell'


class MacUiBackend:
    """osascript / System Events driver (needs the Accessibility permission)."""

    def __init__(self, runner: Runner = _subprocess_runner) -> None:
        self._runner = runner

    def available(self) -> bool:
        return sys.platform == "darwin"

    def _osa(self, script: str) -> str:
        code, out, err = self._runner(["osascript", "-e", script])
        if code != 0:
            low = err.lower()
            if "assistive" in low or "-1719" in low or "-25211" in low or "not allowed" in low:
                raise DavinciError(
                    ErrorCode.WRONG_STATE,
                    "macOS denied UI scripting (Accessibility permission missing).",
                    hint="System Settings > Privacy & Security > Accessibility: enable the "
                    "app running this server (terminal / MCP host), then retry.",
                    retryable=False,
                )
            raise DavinciError(
                ErrorCode.API_CALL_FAILED,
                f"osascript failed: {err.strip()[:300]}",
                retryable=False,
            )
        return out.strip()

    def front_app(self) -> str:
        return self._osa(
            'tell application "System Events" to get name of first application process '
            "whose frontmost is true"
        )

    def activate(self) -> None:
        self._osa(f"tell application {_q(APP_NAME)} to activate")

    def list_menu(self, path: list[str]) -> list[str]:
        if not path:
            body = (
                "set out to {}\nrepeat with m in menu bar items of menu bar 1\n"
                "set end of out to name of m\nend repeat\n"
            )
        else:
            body = (
                f"set out to {{}}\nrepeat with m in menu items of {_menu_ref(path)}\n"
                "set n to name of m\nif n is not missing value then set end of out to n\n"
                "end repeat\n"
            )
        body += "set AppleScript's text item delimiters to linefeed\nreturn out as text"
        out = self._osa(_system_events(body))
        return [line for line in out.splitlines() if line]

    def menu(self, path: list[str]) -> None:
        if len(path) < _MIN_PATH:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS, "A menu path needs at least a menu and an item."
            )
        item = f"menu item {_q(path[-1])} of {_menu_ref(path[:-1])}"
        self._osa(_system_events(f"click {item}"))

    def keystroke(self, key: str, modifiers: list[str]) -> None:
        bad = [m for m in modifiers if m not in _MODIFIERS]
        if bad:
            raise DavinciError(ErrorCode.INVALID_PARAMS, f"Unknown modifiers: {bad}")
        using = ""
        if modifiers:
            using = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
        if key in _KEY_CODES:
            cmd = f"key code {_KEY_CODES[key]}{using}"
        else:
            cmd = f"keystroke {_q(key)}{using}"
        self._osa(_system_events(cmd))


class UnsupportedUiBackend:
    """Stub for platforms without UI automation support yet."""

    def __init__(self, platform: str) -> None:
        self._platform = platform

    def available(self) -> bool:
        return False

    def _no(self) -> DavinciError:
        return DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            f"UI automation is not implemented on {self._platform}.",
            hint="Only macOS is supported for now.",
            retryable=False,
        )

    def front_app(self) -> str:
        raise self._no()

    def activate(self) -> None:
        raise self._no()

    def list_menu(self, path: list[str]) -> list[str]:
        raise self._no()

    def menu(self, path: list[str]) -> None:
        raise self._no()

    def keystroke(self, key: str, modifiers: list[str]) -> None:
        raise self._no()


def default_backend(platform: str | None = None) -> UiBackend:
    plat = platform or sys.platform
    if plat == "darwin":
        return MacUiBackend()
    return UnsupportedUiBackend("windows" if plat.startswith("win") else plat)


_backend: UiBackend | None = None


def set_backend(backend: UiBackend | None) -> None:
    """Install a backend (tests, other OSes); ``None`` restores the default."""
    global _backend  # noqa: PLW0603 - module-level injection point
    _backend = backend


def get_backend() -> UiBackend:
    return _backend if _backend is not None else default_backend()


# --- shortcut table: default keyboard preset only -------------------------------------

SHORTCUTS: dict[str, dict[str, object]] = {
    "blade": {"key": "b", "modifiers": ["command"], "doc": "Split clip at playhead (Edit)."},
    "ripple_delete": {
        "key": "delete",
        "modifiers": ["shift"],
        "doc": "Ripple delete the selection (Edit).",
    },
    "undo": {"key": "z", "modifiers": ["command"], "doc": "Undo."},
    "redo": {"key": "z", "modifiers": ["command", "shift"], "doc": "Redo."},
    "render_in_place": {
        "menu": ["Timeline", "Render In Place"],
        "doc": "Menu command; availability depends on the selection and version.",
    },
    "open_gallery": {"menu": ["Workspace", "Gallery"], "doc": "Show the Gallery (Color page)."},
    "toggle_scopes": {
        "menu": ["Workspace", "Video Scopes"],
        "doc": "Video Scopes submenu; use invoke_menu with the full path for a given scope.",
    },
}
ShortcutName = Literal[
    "blade", "ripple_delete", "undo", "redo", "render_in_place", "open_gallery", "toggle_scopes"
]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UiReceipt(BaseModel):
    action: str
    method: Literal["menu", "keystroke"]
    detail: str
    front_app_before: str | None = None
    warnings: list[str] = Field(default_factory=list)
    items_before: int | None = None
    page: str | None = None


class MenuListing(BaseModel):
    path: list[str]
    items: list[str]


class MenuParams(_Params):
    path: list[str] = Field(
        default_factory=list,
        description="Menu chain, e.g. ['Workspace']. Empty lists the top-level menu bar.",
    )


class InvokeMenuParams(_Params):
    path: list[str] = Field(min_length=2, description="Menu path, e.g. ['Edit','Undo'].")
    validate_path: bool = Field(default=True, description="Check the item exists first.")


class ShortcutParams(_Params):
    command: ShortcutName
    keyboard_preset: str | None = Field(
        default=None,
        description="Name of the user's keyboard preset if known. Shortcuts assume 'Default'.",
    )


class PresetParams(_Params):
    keyboard_preset: str | None = Field(
        default=None, description="User's keyboard preset if known; non-default triggers a warning."
    )


def _preset_warnings(preset: str | None) -> list[str]:
    if preset is None:
        return ["Default keyboard preset assumed; shortcuts differ under custom presets."]
    if preset.strip().lower() != "default":
        return [f"Keyboard preset {preset!r} is not the default; the shortcut may not match."]
    return []


def _prepare(backend: UiBackend) -> str:
    """Check availability, remember the front app and bring Resolve forward."""
    if not backend.available():
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "UI automation is unavailable on this platform.",
            hint="Only macOS is supported for now.",
            retryable=False,
        )
    before = backend.front_app()
    backend.activate()
    return before


def _count_items(s: ResolveSession) -> int:
    tl = s.timeline
    total = 0
    for i in range(1, int(tl.GetTrackCount("video") or 0) + 1):
        total += len(tl.GetItemListInTrack("video", i) or [])
    return total


@action(domain=DOMAIN, kind=Kind.READ, params=MenuParams, result=MenuListing)
def list_menu_items(s: ResolveSession, p: MenuParams) -> MenuListing:
    """List the items of a Resolve menu (top-level menu bar when path is empty)."""
    backend = get_backend()
    if not backend.available():
        raise DavinciError(ErrorCode.CAPABILITY_UNAVAILABLE, "UI automation is unavailable.")
    return MenuListing(path=p.path, items=backend.list_menu(p.path))


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=InvokeMenuParams,
    result=UiReceipt,
    verify_reason=NO_READBACK,
)
def invoke_menu(s: ResolveSession, p: InvokeMenuParams) -> UiReceipt:
    """Click a Resolve menu item by its path (brings Resolve to front)."""
    backend = get_backend()
    before = _prepare(backend)
    if p.validate_path:
        siblings = backend.list_menu(p.path[:-1])
        if p.path[-1] not in siblings:
            raise DavinciError(
                ErrorCode.NOT_FOUND,
                f"Menu item {p.path[-1]!r} not found under {p.path[:-1]}.",
                details={"available": siblings},
                retryable=False,
            )
    backend.menu(p.path)
    return UiReceipt(
        action="invoke_menu", method="menu", detail=" > ".join(p.path), front_app_before=before
    )


def _run_shortcut(name: str, preset: str | None, backend: UiBackend) -> UiReceipt:
    entry = SHORTCUTS[name]
    before = _prepare(backend)
    warnings = _preset_warnings(preset)
    if "menu" in entry:
        path = [str(x) for x in entry["menu"]]  # type: ignore[attr-defined]
        backend.menu(path)
        return UiReceipt(
            action=name,
            method="menu",
            detail=" > ".join(path),
            front_app_before=before,
            warnings=warnings,
        )
    key = str(entry["key"])
    mods = [str(x) for x in entry["modifiers"]]  # type: ignore[attr-defined]
    backend.keystroke(key, mods)
    return UiReceipt(
        action=name,
        method="keystroke",
        detail="+".join([*mods, key]),
        front_app_before=before,
        warnings=warnings,
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ShortcutParams,
    result=UiReceipt,
    verify_reason=NO_READBACK,
)
def send_shortcut(s: ResolveSession, p: ShortcutParams) -> UiReceipt:
    """Run a named Resolve command via its default shortcut or menu (default preset only)."""
    return _run_shortcut(p.command, p.keyboard_preset, get_backend())


def _verify_gallery(s: ResolveSession, p: PresetParams, r: UiReceipt) -> Verification:
    page = s.resolve.GetCurrentPage()
    return Verification(
        ok=page == "color",
        observed={"page": page},
        note="Only the page change is verified; Gallery visibility is not readable.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetParams,
    result=UiReceipt,
    verify=_verify_gallery,
    page="color",
)
def open_gallery_panel(s: ResolveSession, p: PresetParams) -> UiReceipt:
    """Open the Color page and show the Gallery (needed for GalleryStillAlbum.ExportStills)."""
    receipt = _run_shortcut("open_gallery", p.keyboard_preset, get_backend())
    receipt.page = s.resolve.GetCurrentPage()
    return receipt


def _verify_blade(s: ResolveSession, p: PresetParams, r: UiReceipt) -> Verification:
    now = _count_items(s)
    before = r.items_before or 0
    return Verification(
        ok=now > before,
        observed={"items_before": before, "items_after": now},
        note="Blade only changes the count when the playhead is over a clip on a targeted track.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetParams,
    result=UiReceipt,
    verify=_verify_blade,
    page="edit",
)
def blade_at_playhead(s: ResolveSession, p: PresetParams) -> UiReceipt:
    """Split the clips under the playhead on the Edit page (default Cmd+B)."""
    count = _count_items(s)
    receipt = _run_shortcut("blade", p.keyboard_preset, get_backend())
    receipt.items_before = count
    receipt.page = "edit"
    return receipt


def _plan_ripple(s: ResolveSession, p: PresetParams) -> PlanDraft:
    tl = s.timeline
    count = _count_items(s)
    return PlanDraft(
        summary=f"Ripple delete the current selection in {tl.GetName()!r} (Shift+Delete).",
        effects=[
            "Deletes the selected clips and closes the gap; the selection is not readable "
            "through the API, so the exact victims cannot be listed.",
            f"Timeline currently holds {count} video clips.",
        ],
        targets=[f"timeline:{tl.GetUniqueId()}"],
        fingerprint=f"{tl.GetUniqueId()}:{count}",
    )


def _verify_ripple(s: ResolveSession, p: PresetParams, r: UiReceipt) -> Verification:
    now = _count_items(s)
    before = r.items_before or 0
    return Verification(ok=now < before, observed={"items_before": before, "items_after": now})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=PresetParams,
    result=UiReceipt,
    plan=_plan_ripple,
    verify=_verify_ripple,
    page="edit",
)
def ripple_delete_selection(s: ResolveSession, p: PresetParams) -> UiReceipt:
    """Ripple delete the selected timeline clips via the UI (selection not API-readable)."""
    count = _count_items(s)
    receipt = _run_shortcut("ripple_delete", p.keyboard_preset, get_backend())
    receipt.items_before = count
    receipt.page = "edit"
    return receipt


ACTIONS = [
    list_menu_items,
    invoke_menu,
    send_shortcut,
    open_gallery_panel,
    blade_at_playhead,
    ripple_delete_selection,
]
