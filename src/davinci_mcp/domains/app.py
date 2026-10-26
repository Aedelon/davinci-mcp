# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""App domain: Resolve application level (pages, version, keyframe mode, presets, session).

Traps encoded here (research/audit-existant.md section 6.2 A):

- ``SetHighPriority`` on 21.1 needs a real bool and has no getter.
- ``DisableBackgroundTasksForCurrentResolveSession`` is irreversible for the session.
- Keyframe mode constants must be read from the live ``resolve`` handle.
- Keyboard presets exist from 21.1 only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import const
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "app"

Page = Literal["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"]
PresetKind = Literal["layout", "keyboard", "user_preferences", "burn_in", "fairlight"]
_KEYFRAME = {
    "all": "KEYFRAME_MODE_ALL",
    "color": "KEYFRAME_MODE_COLOR",
    "sizing": "KEYFRAME_MODE_SIZING",
}

# kind -> operation -> Resolve method name
_METHODS: dict[str, dict[str, str]] = {
    "layout": {
        "list": "GetLayoutPresetList",
        "load": "LoadLayoutPreset",
        "save": "SaveLayoutPreset",
        "update": "UpdateLayoutPreset",
        "import": "ImportLayoutPreset",
        "export": "ExportLayoutPreset",
        "delete": "DeleteLayoutPreset",
    },
    "keyboard": {
        "list": "GetKeyboardPresetList",
        "load": "LoadKeyboardPreset",
        "import": "ImportKeyboardPreset",
        "export": "ExportKeyboardPreset",
        "delete": "DeleteKeyboardPreset",
    },
    "user_preferences": {
        "list": "GetUserPreferencesPresetList",
        "load": "LoadUserPreferencesPreset",
        "save": "SaveUserPreferencesPreset",
        "import": "ImportUserPreferencesPreset",
        "export": "ExportUserPreferencesPreset",
        "delete": "DeleteUserPreferencesPreset",
    },
    "burn_in": {
        "list": "GetBurnInPresetList",
        "import": "ImportBurnInPreset",
        "export": "ExportBurnInPreset",
        "delete": "DeleteBurnInPreset",
    },
    "fairlight": {"list": "GetFairlightPresets"},
}
_NAMELESS_IMPORT = frozenset({"burn_in"})  # ImportBurnInPreset(path) takes no name


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppInfo(BaseModel):
    product: str
    version: str
    version_parts: list[Any]
    is_studio: bool
    current_page: str | None
    keyframe_mode: str | None = Field(description="'all' | 'color' | 'sizing' when known.")


class Done(BaseModel):
    ok: bool = True
    detail: str = ""


class PresetListResult(BaseModel):
    kind: str
    presets: list[str]
    current: str | None = None


class PresetOpResult(BaseModel):
    kind: str
    name: str | None
    path: str | None = None
    count_before: int = 0


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _fail(what: str, hint: str | None = None) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, f"{what} returned False/None.", hint=hint, retryable=False
    )


def _method(s: ResolveSession, kind: str, op: str) -> str:
    name = _METHODS.get(kind, {}).get(op)
    if name is None:
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            f"Operation {op!r} is not available for {kind} presets.",
            details={"supported": sorted(_METHODS.get(kind, {}))},
            retryable=False,
        )
    s.caps.require(s.resolve, name, since=(21, 1, 0) if kind == "keyboard" else None)
    return name


def _names(s: ResolveSession, kind: str) -> list[str]:
    raw: list[Any] = getattr(s.resolve, _method(s, kind, "list"))() or []
    return [str(n) for n in raw]


def _keyframe_name(s: ResolveSession) -> str | None:
    if not s.caps.has(s.resolve, "GetKeyframeMode"):
        return None
    mode = s.resolve.GetKeyframeMode()
    for key, const_name in _KEYFRAME.items():
        if getattr(s.resolve, const_name, None) == mode:
            return key
    return None


# -- read ------------------------------------------------------------------------


@action(domain=DOMAIN, kind=Kind.READ, result=AppInfo)
def get_app_info(s: ResolveSession, _: BaseModel) -> AppInfo:
    """Product, version, edition, current page and keyframe mode."""
    r = s.resolve
    return AppInfo(
        product=str(r.GetProductName()),
        version=str(r.GetVersionString()),
        version_parts=list(r.GetVersion() or []),
        is_studio=bool(r.IsStudio()),
        current_page=r.GetCurrentPage(),
        keyframe_mode=_keyframe_name(s),
    )


class ListPresetsParams(_Params):
    kind: PresetKind


@action(domain=DOMAIN, kind=Kind.READ, params=ListPresetsParams, result=PresetListResult)
def list_presets(s: ResolveSession, p: ListPresetsParams) -> PresetListResult:
    """List layout, keyboard, user-preferences, burn-in or Fairlight presets."""
    current: str | None = None
    if p.kind == "keyboard":
        s.caps.require(s.resolve, "GetCurrentKeyboardPreset", since=(21, 1, 0))
        current = s.resolve.GetCurrentKeyboardPreset() or None
    return PresetListResult(kind=p.kind, presets=_names(s, p.kind), current=current)


# -- write: pages / keyframe mode -----------------------------------------------------


class OpenPageParams(_Params):
    page: Page


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=OpenPageParams,
    result=Done,
    idempotent=True,
    verify=lambda s, p, _r: Verification(
        ok=s.resolve.GetCurrentPage() == p.page, observed={"page": s.resolve.GetCurrentPage()}
    ),
)
def open_page(s: ResolveSession, p: OpenPageParams) -> Done:
    """Switch Resolve to a page (media, cut, edit, fusion, color, fairlight, deliver)."""
    if not s.resolve.OpenPage(p.page):
        raise _fail("Resolve.OpenPage")
    return Done(detail=f"Page {p.page}.")


class KeyframeModeParams(_Params):
    mode: Literal["all", "color", "sizing"]


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=KeyframeModeParams,
    result=Done,
    idempotent=True,
    verify=lambda s, p, _r: Verification(
        ok=_keyframe_name(s) == p.mode, observed={"keyframe_mode": _keyframe_name(s)}
    ),
)
def set_keyframe_mode(s: ResolveSession, p: KeyframeModeParams) -> Done:
    """Set the keyframe mode (all, color or sizing)."""
    s.caps.require(s.resolve, "SetKeyframeMode")
    value = const(s, _KEYFRAME[p.mode])
    if not s.resolve.SetKeyframeMode(value):
        raise _fail("Resolve.SetKeyframeMode")
    return Done(detail=f"Keyframe mode {p.mode}.")


# -- write: presets ------------------------------------------------------------------


class PresetNameParams(_Params):
    kind: PresetKind
    name: str = Field(min_length=1)


def _verify_listed(s: ResolveSession, p: PresetNameParams, _: PresetOpResult) -> Verification:
    names = _names(s, p.kind)
    return Verification(ok=p.name in names, observed={"presets": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=PresetOpResult,
    verify=_verify_listed,
)
def save_preset(s: ResolveSession, p: PresetNameParams) -> PresetOpResult:
    """Save the current layout or user preferences as a named preset."""
    method = _method(s, p.kind, "save")
    if not getattr(s.resolve, method)(p.name):
        raise _fail(f"Resolve.{method}")
    return PresetOpResult(kind=p.kind, name=p.name)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=PresetOpResult,
    verify_reason="UpdateLayoutPreset has no read-back of the preset content.",
)
def update_preset(s: ResolveSession, p: PresetNameParams) -> PresetOpResult:
    """Overwrite a layout preset with the current window layout."""
    method = _method(s, p.kind, "update")
    if p.name not in _names(s, p.kind):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.kind} preset {p.name!r}.", retryable=False)
    if not getattr(s.resolve, method)(p.name):
        raise _fail(f"Resolve.{method}")
    return PresetOpResult(kind=p.kind, name=p.name)


def _verify_loaded(s: ResolveSession, p: PresetNameParams, _: PresetOpResult) -> Verification:
    if p.kind == "keyboard" and s.caps.has(s.resolve, "GetCurrentKeyboardPreset"):
        cur = s.resolve.GetCurrentKeyboardPreset()
        return Verification(ok=cur == p.name, observed={"current_keyboard_preset": cur})
    return Verification(ok=True, note="No getter for the active preset; load returned True.")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=PresetOpResult,
    verify=_verify_loaded,
)
def load_preset(s: ResolveSession, p: PresetNameParams) -> PresetOpResult:
    """Load a layout, keyboard or user-preferences preset."""
    method = _method(s, p.kind, "load")
    if p.name not in _names(s, p.kind):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.kind} preset {p.name!r}.", retryable=False)
    if not getattr(s.resolve, method)(p.name):
        raise _fail(f"Resolve.{method}")
    return PresetOpResult(kind=p.kind, name=p.name)


class ImportPresetParams(_Params):
    kind: PresetKind
    path: str = Field(description="Preset file (inside the allowed roots).")
    name: str | None = Field(default=None, description="Name for the imported preset.")


def _verify_imported(s: ResolveSession, p: ImportPresetParams, r: PresetOpResult) -> Verification:
    names = _names(s, p.kind)
    ok = (
        p.name in names
        if p.name and p.kind not in _NAMELESS_IMPORT
        else len(names) > r.count_before
    )
    return Verification(ok=ok, observed={"presets": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportPresetParams,
    result=PresetOpResult,
    verify=_verify_imported,
)
def import_preset(s: ResolveSession, p: ImportPresetParams) -> PresetOpResult:
    """Import a layout, keyboard, user-preferences or burn-in preset file."""
    method = _method(s, p.kind, "import")
    path = check_path(p.path, runtime().allowed_roots, must_exist=True)
    before = len(_names(s, p.kind))
    fn = getattr(s.resolve, method)
    ok = fn(str(path), p.name) if p.name and p.kind not in _NAMELESS_IMPORT else fn(str(path))
    if not ok:
        raise _fail(f"Resolve.{method}", "The preset name may already exist.")
    return PresetOpResult(kind=p.kind, name=p.name, path=str(path), count_before=before)


class ExportPresetParams(PresetNameParams):
    path: str = Field(description="Destination file (inside the allowed roots).")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportPresetParams,
    result=PresetOpResult,
    verify=lambda _s, _p, r: Verification(
        ok=Path(r.path or "").exists(), observed={"path": r.path}
    ),
)
def export_preset(s: ResolveSession, p: ExportPresetParams) -> PresetOpResult:
    """Export a layout, keyboard, user-preferences or burn-in preset to a file."""
    method = _method(s, p.kind, "export")
    if p.name not in _names(s, p.kind):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.kind} preset {p.name!r}.", retryable=False)
    path = check_path(p.path, runtime().allowed_roots, must_exist=False)
    if path.exists():
        raise DavinciError(ErrorCode.WRONG_STATE, f"{path} already exists.", retryable=False)
    if not getattr(s.resolve, method)(p.name, str(path)):
        raise _fail(f"Resolve.{method}")
    return PresetOpResult(kind=p.kind, name=p.name, path=str(path))


class SetHighPriorityParams(_Params):
    enabled: bool


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetHighPriorityParams,
    result=Done,
    idempotent=True,
    verify_reason="SetHighPriority has no getter.",
)
def set_high_priority(s: ResolveSession, p: SetHighPriorityParams) -> Done:
    """Raise or restore the Resolve process priority (needs a real bool on 21.1)."""
    s.caps.require(s.resolve, "SetHighPriority")
    if s.resolve.SetHighPriority(bool(p.enabled)) is False:
        raise _fail("Resolve.SetHighPriority", "21.1 refuses non-bool arguments.")
    return Done(detail=f"High priority {'on' if p.enabled else 'off'}.")


# -- destructive ------------------------------------------------------------------


def _plan_delete_preset(s: ResolveSession, p: PresetNameParams) -> PlanDraft:
    _method(s, p.kind, "delete")
    names = _names(s, p.kind)
    if p.name not in names:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.kind} preset {p.name!r}.", retryable=False)
    return PlanDraft(
        summary=f"Delete the {p.kind} preset {p.name!r}.",
        effects=["The preset is removed from Resolve; export it first to keep a copy."],
        targets=[f"{p.kind}_preset:{p.name}"],
        fingerprint=_fingerprint({"kind": p.kind, "name": p.name, "all": names}),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=PresetNameParams,
    result=PresetOpResult,
    plan=_plan_delete_preset,
    verify=lambda s, p, _r: Verification(
        ok=p.name not in _names(s, p.kind), observed={"presets": _names(s, p.kind)}
    ),
)
def delete_preset(s: ResolveSession, p: PresetNameParams) -> PresetOpResult:
    """Delete a layout, keyboard, user-preferences or burn-in preset."""
    method = _method(s, p.kind, "delete")
    if not getattr(s.resolve, method)(p.name):
        raise _fail(f"Resolve.{method}", "Built-in presets cannot be deleted.")
    return PresetOpResult(kind=p.kind, name=p.name)


def _plan_disable_bg(s: ResolveSession, _: BaseModel) -> PlanDraft:
    s.caps.require(s.resolve, "DisableBackgroundTasksForCurrentResolveSession")
    return PlanDraft(
        summary="Disable background tasks for the whole running Resolve session.",
        effects=[
            "Irreversible until Resolve is restarted; applies to the whole instance.",
            "Caching, thumbnails and other background jobs stop.",
        ],
        targets=["resolve:session"],
        fingerprint=_fingerprint({"version": s.caps.version_string}),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    result=Done,
    plan=_plan_disable_bg,
    verify_reason="The call returns None and there is no getter.",
)
def disable_background_tasks(s: ResolveSession, _: BaseModel) -> Done:
    """Disable background tasks for this Resolve session (irreversible)."""
    s.resolve.DisableBackgroundTasksForCurrentResolveSession()
    return Done(detail="Background tasks disabled until Resolve restarts.")


def _plan_quit(s: ResolveSession, _: BaseModel) -> PlanDraft:
    cur = s.project_manager.GetCurrentProject()
    name = str(cur.GetName()) if cur is not None else None
    return PlanDraft(
        summary="Quit DaVinci Resolve.",
        effects=[
            "Resolve closes and the MCP connection is lost until it is relaunched.",
            f"Unsaved work in the open project ({name!r}) may be lost; save it first."
            if name
            else "No project is open.",
            "A running render may block the quit.",
        ],
        targets=["resolve:application"],
        fingerprint=_fingerprint({"project": name}),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    result=Done,
    plan=_plan_quit,
    verify_reason="Resolve exits, so no state can be read back.",
)
def quit_resolve(s: ResolveSession, _: BaseModel) -> Done:
    """Quit the DaVinci Resolve application."""
    if s.resolve.Quit() is False:
        raise _fail("Resolve.Quit", "A render in progress blocks the quit.")
    return Done(detail="Quit requested.")


ACTIONS = [
    get_app_info,
    list_presets,
    open_page,
    set_keyframe_mode,
    save_preset,
    update_preset,
    load_preset,
    import_preset,
    export_preset,
    set_high_priority,
    delete_preset,
    disable_background_tasks,
    quit_resolve,
]
