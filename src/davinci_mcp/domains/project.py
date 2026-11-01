# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Project domain: ProjectManager + Project (projects, folders, databases, settings, presets).

Render, Media Pool, timelines, gallery and color groups live in other domains.
Traps encoded here (see research/audit-existant.md section 6.2 C):

- ``CreateProject`` replaces the current project and returns ``None`` behind a
  "Save Current Project" modal: the current project is saved first, an unsaved
  "Untitled Project" is refused unless the client explicitly discards it.
- ``SaveProject`` on "Untitled Project" hangs forever headless: always refused.
- ``DeleteProject`` fails on the current/recent project: the current one is refused.
- ``GetCurrentDatabase() is None`` means a stuck instance: creating/loading is refused.
- ``ArchiveProject`` never produces an archive by script (SIGSEGV with media).
- Project settings presets modified before the first ``SaveProject`` destroy the project.
- ``SetSetting('timelinePlaybackFrameRate')`` always fails.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.api._generated.resolve_21_1 import DatabaseInfo
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "project"
UNTITLED = "Untitled Project"
_ALWAYS_FAILS = frozenset({"timelinePlaybackFrameRate"})
_ATTRIBUTE_KEYS = ("lastModifiedDate", "creationDate", "notes", "liveCollaborationMode")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NameParams(_Params):
    name: str = Field(min_length=1)


class PresetNameParams(_Params):
    name: str = Field(min_length=1, description="Preset name.")


# -- results -----------------------------------------------------------------------


class ProjectEntry(BaseModel):
    name: str
    last_modified: int | None = Field(default=None, description="Epoch seconds when known.")
    attributes: dict[str, str] = Field(default_factory=dict[str, str])


class ProjectList(BaseModel):
    current_folder: str | None
    folders: list[str]
    projects: list[ProjectEntry]
    current_project: str | None


class ProjectInfo(BaseModel):
    name: str
    unique_id: str | None
    folder: str | None
    database: DatabaseInfo | None
    is_untitled: bool


class DatabaseList(BaseModel):
    current: DatabaseInfo | None
    databases: list[DatabaseInfo]


class SettingsResult(BaseModel):
    settings: dict[str, Any]
    missing: list[str] = Field(default_factory=list[str])


class PresetList(BaseModel):
    presets: list[dict[str, Any]]


class SetSettingsResult(BaseModel):
    applied: dict[str, Any]
    not_applied: dict[str, str] = Field(
        description="Keys that did not stick, with the reason (or the value Resolve kept)."
    )


class Done(BaseModel):
    ok: bool = True
    detail: str = ""


class FolderState(BaseModel):
    current_folder: str | None
    folders: list[str]
    projects: list[str]


class TransferResult(BaseModel):
    name: str | None
    path: str | None = None
    projects_before: int = 0


class DeletedResult(BaseModel):
    deleted: str


# -- helpers -----------------------------------------------------------------------


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _fail(what: str, hint: str | None = None) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, f"{what} returned False/None.", hint=hint, retryable=False
    )


def _current(s: ResolveSession) -> ResolveObject | None:
    return s.project_manager.GetCurrentProject()


def _projects(s: ResolveSession) -> list[str]:
    names: list[Any] = s.project_manager.GetProjectListInCurrentFolder() or []
    return [str(n) for n in names]


def _folders(s: ResolveSession) -> list[str]:
    names: list[Any] = s.project_manager.GetFolderListInCurrentFolder() or []
    return [str(n) for n in names]


def _database(s: ResolveSession) -> DatabaseInfo | None:
    raw = s.project_manager.GetCurrentDatabase()
    return _db(raw) if raw else None


def _db(raw: dict[str, Any]) -> DatabaseInfo:
    keys = ("DbType", "DbName", "IpAddress")
    return DatabaseInfo.model_validate({k: raw[k] for k in keys if k in raw})


def _current_name(s: ResolveSession) -> str | None:
    cur = _current(s)
    return str(cur.GetName()) if cur is not None else None


def _prepare_switch(s: ResolveSession, discard_untitled: bool) -> None:
    """Make replacing/switching the current project safe (or refuse)."""
    pm = s.project_manager
    if pm.GetCurrentDatabase() is None:
        raise DavinciError(
            ErrorCode.INSTANCE_STUCK,
            "GetCurrentDatabase() returned None: Resolve is stuck after an unclean shutdown.",
            hint="Create/Load would return False forever. Restart DaVinci Resolve.",
            retryable=False,
        )
    cur = pm.GetCurrentProject()
    if cur is None:
        return
    if cur.GetName() == UNTITLED:
        if not discard_untitled:
            raise DavinciError(
                ErrorCode.WRONG_STATE,
                "The current project is an unsaved 'Untitled Project'; switching would lose it.",
                hint="Rename it (rename_project) and save it, or pass discard_untitled=true.",
                retryable=False,
            )
        return
    if not pm.SaveProject():
        raise _fail("ProjectManager.SaveProject", "Could not save the current project first.")


def _info(s: ResolveSession) -> ProjectInfo:
    cur = s.project
    pm = s.project_manager
    name = str(cur.GetName())
    uid = cur.GetUniqueId() if s.caps.has(cur, "GetUniqueId") else None
    folder = pm.GetCurrentFolder() if s.caps.has(pm, "GetCurrentFolder") else None
    return ProjectInfo(
        name=name,
        unique_id=str(uid) if uid else None,
        folder=str(folder) if folder else None,
        database=_database(s),
        is_untitled=name == UNTITLED,
    )


def _read_settings(s: ResolveSession, keys: list[str] | None) -> SettingsResult:
    project = s.project
    if s.caps.has(project, "GetSettings"):
        full: dict[str, Any] = dict(project.GetSettings() or {})
    elif keys:
        full = {k: project.GetSetting(k) for k in keys}
        full = {k: v for k, v in full.items() if v is not None}
    else:
        full = dict(project.GetSetting() or {})
    if keys is None:
        return SettingsResult(settings=full)
    return SettingsResult(
        settings={k: full[k] for k in keys if k in full},
        missing=[k for k in keys if k not in full],
    )


def _match(a: object, b: object) -> bool:
    """Same value, whatever its spelling: Resolve reads back "25" as 25.0."""
    if str(a) == str(b):
        return True
    try:
        return float(str(a)) == float(str(b))
    except ValueError:
        return False


def _preset_name(item: object) -> str:
    if isinstance(item, dict):
        return str(cast("dict[str, Any]", item).get("Name", ""))
    return str(item)


def _preset_names(s: ResolveSession) -> list[str]:
    raw: list[Any] = s.project.GetProjectSettingsPresetList() or []
    return [_preset_name(item) for item in raw]


def _safe_path(raw: str, *, must_exist: bool) -> Path:
    return check_path(raw, runtime().allowed_roots, must_exist=must_exist)


def _require_saved(s: ResolveSession) -> None:
    """Preset application before the first SaveProject destroys the project (#207)."""
    name = _current_name(s)
    if name is None:
        raise DavinciError(ErrorCode.WRONG_STATE, "No project is open.", retryable=False)
    if name == UNTITLED:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "Refusing to touch settings presets on an unsaved 'Untitled Project'.",
            hint="Rename the project, then save it (21.1 destroys such projects, issue #207).",
            retryable=False,
        )
    if not s.project_manager.SaveProject():
        raise _fail("ProjectManager.SaveProject", "Save before changing settings presets.")


# -- read ------------------------------------------------------------------------


@action(domain=DOMAIN, kind=Kind.READ, result=ProjectList)
def list_projects(s: ResolveSession, _: BaseModel) -> ProjectList:
    """List projects and folders of the current database folder."""
    pm = s.project_manager
    names = _projects(s)
    attrs: dict[str, Any] = {}
    if s.caps.has(pm, "GetProjectAttributesInCurrentFolder"):
        attrs = dict[str, Any](pm.GetProjectAttributesInCurrentFolder() or {})
    entries: list[ProjectEntry] = []
    for name in names:
        last: int | None = None
        if s.caps.has(pm, "GetProjectLastModifiedTime"):
            raw = pm.GetProjectLastModifiedTime(name)
            last = int(raw) if raw else None
        info: dict[str, Any] = attrs.get(name) or {}
        entries.append(
            ProjectEntry(
                name=name,
                last_modified=last,
                attributes={k: str(info[k]) for k in _ATTRIBUTE_KEYS if k in info},
            )
        )
    folder = pm.GetCurrentFolder() if s.caps.has(pm, "GetCurrentFolder") else None
    return ProjectList(
        current_folder=str(folder) if folder else None,
        folders=_folders(s),
        projects=entries,
        current_project=_current_name(s),
    )


@action(domain=DOMAIN, kind=Kind.READ, result=ProjectInfo)
def get_current_project(s: ResolveSession, _: BaseModel) -> ProjectInfo:
    """Name, unique id, folder and database of the open project."""
    return _info(s)


@action(domain=DOMAIN, kind=Kind.READ, result=DatabaseList)
def list_databases(s: ResolveSession, _: BaseModel) -> DatabaseList:
    """List the databases known to Resolve and the current one."""
    raw: list[dict[str, Any]] = s.project_manager.GetDatabaseList() or []
    return DatabaseList(current=_database(s), databases=[_db(d) for d in raw])


class GetSettingsParams(_Params):
    keys: list[str] | None = Field(
        default=None, description="Only these keys (e.g. ['timelineFrameRate']); all when omitted."
    )


@action(domain=DOMAIN, kind=Kind.READ, params=GetSettingsParams, result=SettingsResult)
def get_settings(s: ResolveSession, p: GetSettingsParams) -> SettingsResult:
    """Read project settings (all, or a list of keys)."""
    return _read_settings(s, p.keys)


@action(domain=DOMAIN, kind=Kind.READ, result=PresetList)
def list_settings_presets(s: ResolveSession, _: BaseModel) -> PresetList:
    """List project settings presets (name, width, height)."""
    raw: list[Any] = s.project.GetProjectSettingsPresetList() or []
    presets: list[dict[str, Any]] = [
        cast("dict[str, Any]", p) if isinstance(p, dict) else {"Name": str(p)} for p in raw
    ]
    return PresetList(presets=presets)


# -- write: projects -------------------------------------------------------------


class CreateProjectParams(_Params):
    name: str = Field(min_length=1)
    media_location_path: str | None = Field(
        default=None, description="Project media directory (inside the allowed roots)."
    )
    discard_untitled: bool = Field(
        default=False, description="Allow replacing an unsaved 'Untitled Project'."
    )


def _verify_current(
    s: ResolveSession, p: NameParams | CreateProjectParams, _: ProjectInfo
) -> Verification:
    name = _current_name(s)
    return Verification(ok=name == p.name, observed={"current_project": name})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CreateProjectParams,
    result=ProjectInfo,
    verify=_verify_current,
)
def create_project(s: ResolveSession, p: CreateProjectParams) -> ProjectInfo:
    """Create a project and make it current (saves the current one first)."""
    if p.name in _projects(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"Project {p.name!r} already exists here.", retryable=False
        )
    _prepare_switch(s, p.discard_untitled)
    pm = s.project_manager
    if p.media_location_path:
        media = _safe_path(p.media_location_path, must_exist=True)
        created = pm.CreateProject(p.name, str(media))
    else:
        created = pm.CreateProject(p.name)
    if not created:
        raise _fail(
            "ProjectManager.CreateProject",
            "A 'Save Current Project' modal may be open in Resolve; dismiss it and retry.",
        )
    return _info(s)


class LoadProjectParams(NameParams):
    discard_untitled: bool = Field(
        default=False, description="Allow leaving an unsaved 'Untitled Project'."
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=LoadProjectParams,
    result=ProjectInfo,
    verify=_verify_current,
)
def load_project(s: ResolveSession, p: LoadProjectParams) -> ProjectInfo:
    """Load a project from the current folder (saves the current one first)."""
    if p.name not in _projects(s):
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"No project {p.name!r} in the current folder.",
            next_call="project_read(op={'action': 'list_projects'})",
            retryable=False,
        )
    if _current_name(s) != p.name:
        _prepare_switch(s, p.discard_untitled)
    if not s.project_manager.LoadProject(p.name):
        raise _fail("ProjectManager.LoadProject")
    return _info(s)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    result=Done,
    idempotent=True,
    verify_reason="SaveProject returns a bool and has no observable read-back.",
)
def save_project(s: ResolveSession, _: BaseModel) -> Done:
    """Save the current project (refused for 'Untitled Project')."""
    name = _current_name(s)
    if name is None:
        raise DavinciError(ErrorCode.WRONG_STATE, "No project is open.", retryable=False)
    if name == UNTITLED:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "SaveProject on 'Untitled Project' hangs forever headless.",
            hint="Use rename_project first.",
            retryable=False,
        )
    if not s.project_manager.SaveProject():
        raise _fail("ProjectManager.SaveProject")
    return Done(detail=f"Saved {name!r}.")


class CloseProjectParams(_Params):
    save_first: bool = Field(default=True, description="Save the project before closing.")
    discard_untitled: bool = Field(
        default=False, description="Allow closing an unsaved 'Untitled Project'."
    )


def _verify_closed(s: ResolveSession, _p: CloseProjectParams, _r: Done) -> Verification:
    # Resolve always reopens an empty "Untitled Project" after CloseProject.
    name = _current_name(s)
    return Verification(ok=name in (None, UNTITLED), observed={"current_project": name})


@action(
    domain=DOMAIN, kind=Kind.WRITE, params=CloseProjectParams, result=Done, verify=_verify_closed
)
def close_project(s: ResolveSession, p: CloseProjectParams) -> Done:
    """Close the current project (saved first by default)."""
    cur = _current(s)
    if cur is None:
        raise DavinciError(ErrorCode.WRONG_STATE, "No project is open.", retryable=False)
    name = str(cur.GetName())
    if name == UNTITLED and not p.discard_untitled:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "Closing an unsaved 'Untitled Project' discards it.",
            hint="Pass discard_untitled=true to confirm.",
            retryable=False,
        )
    if p.save_first and name != UNTITLED and not s.project_manager.SaveProject():
        raise _fail("ProjectManager.SaveProject", "Not closing: unsaved changes would be lost.")
    if not s.project_manager.CloseProject(cur):
        raise _fail("ProjectManager.CloseProject")
    return Done(detail=f"Closed {name!r}.")


class RenameParams(_Params):
    new_name: str = Field(min_length=1)


def _verify_renamed(s: ResolveSession, p: RenameParams, _: Done) -> Verification:
    name = _current_name(s)
    return Verification(ok=name == p.new_name, observed={"name": name})


@action(domain=DOMAIN, kind=Kind.WRITE, params=RenameParams, result=Done, verify=_verify_renamed)
def rename_project(s: ResolveSession, p: RenameParams) -> Done:
    """Rename the current project."""
    if p.new_name in _projects(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"Project {p.new_name!r} already exists.", retryable=False
        )
    if not s.project.SetName(p.new_name):
        raise _fail("Project.SetName", "Names must be unique and without forbidden characters.")
    return Done(detail=f"Renamed to {p.new_name!r}.")


# -- write: folders --------------------------------------------------------------


def _folder_state(s: ResolveSession) -> FolderState:
    pm = s.project_manager
    folder = pm.GetCurrentFolder() if s.caps.has(pm, "GetCurrentFolder") else None
    return FolderState(
        current_folder=str(folder) if folder else None, folders=_folders(s), projects=_projects(s)
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=NameParams,
    result=FolderState,
    verify=lambda s, p, _r: Verification(
        ok=p.name in _folders(s), observed={"folders": _folders(s)}
    ),
)
def create_folder(s: ResolveSession, p: NameParams) -> FolderState:
    """Create a project folder in the current database folder."""
    if p.name in _folders(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"Folder {p.name!r} already exists.", retryable=False
        )
    if not s.project_manager.CreateFolder(p.name):
        raise _fail("ProjectManager.CreateFolder")
    return _folder_state(s)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=NameParams,
    result=FolderState,
    verify_reason="GetCurrentFolder is an app-formatted label; the result reports the new state.",
)
def open_folder(s: ResolveSession, p: NameParams) -> FolderState:
    """Enter a sub-folder of the current project folder."""
    if p.name not in _folders(s):
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"No folder {p.name!r} here.",
            details={"folders": _folders(s)},
            retryable=False,
        )
    if not s.project_manager.OpenFolder(p.name):
        raise _fail("ProjectManager.OpenFolder")
    return _folder_state(s)


class GotoFolderParams(_Params):
    to: Literal["root", "parent"]


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=GotoFolderParams,
    result=FolderState,
    idempotent=True,
    verify_reason="GetCurrentFolder is an app-formatted label; the result reports the new state.",
)
def goto_folder(s: ResolveSession, p: GotoFolderParams) -> FolderState:
    """Go to the root or the parent project folder."""
    pm = s.project_manager
    ok = pm.GotoRootFolder() if p.to == "root" else pm.GotoParentFolder()
    if not ok:
        raise _fail(f"ProjectManager.Goto{p.to.title()}Folder", "Already at the root?")
    return _folder_state(s)


# -- write: database ---------------------------------------------------------------


class SetDatabaseParams(_Params):
    database: DatabaseInfo
    discard_untitled: bool = False


def _verify_database(s: ResolveSession, p: SetDatabaseParams, _: DatabaseList) -> Verification:
    cur = _database(s)
    ok = cur is not None and cur.DbName == p.database.DbName and cur.DbType == p.database.DbType
    return Verification(ok=ok, observed=cur.model_dump(exclude_none=True) if cur else {})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetDatabaseParams,
    result=DatabaseList,
    verify=_verify_database,
)
def set_database(s: ResolveSession, p: SetDatabaseParams) -> DatabaseList:
    """Switch the current database (DbType, DbName, optional IpAddress)."""
    if _current_name(s) == UNTITLED and not p.discard_untitled:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "Switching database would lose the unsaved 'Untitled Project'.",
            hint="Rename and save it, or pass discard_untitled=true.",
            retryable=False,
        )
    if not s.project_manager.SetCurrentDatabase(p.database.model_dump(exclude_none=True)):
        raise _fail("ProjectManager.SetCurrentDatabase", "Check DbType/DbName/IpAddress.")
    dbs: list[dict[str, Any]] = s.project_manager.GetDatabaseList() or []
    return DatabaseList(current=_database(s), databases=[_db(d) for d in dbs])


# -- write: settings -------------------------------------------------------------


class SetSettingsParams(_Params):
    settings: dict[str, str | int | float | bool] = Field(
        min_length=1, description="Project setting key -> value, e.g. {'timelineFrameRate': '24'}."
    )


def _verify_settings(s: ResolveSession, p: SetSettingsParams, r: SetSettingsResult) -> Verification:
    now = _read_settings(s, list(p.settings)).settings
    bad = {k: now.get(k) for k in p.settings if not _match(now.get(k), p.settings[k])}
    return Verification(ok=not bad and not r.not_applied, observed={"mismatched": bad})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetSettingsParams,
    result=SetSettingsResult,
    verify=_verify_settings,
)
def set_settings(s: ResolveSession, p: SetSettingsParams) -> SetSettingsResult:
    """Set project settings one key at a time, verified by read-back."""
    project = s.project
    plural = s.caps.has(project, "SetSettings")
    applied: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for key, value in p.settings.items():
        if key in _ALWAYS_FAILS:
            failed[key] = "Always rejected by the scripting API; set it in the UI."
            continue
        sent = str(value) if not isinstance(value, bool) else str(int(value))
        ok = project.SetSettings({key: sent}) if plural else project.SetSetting(key, sent)
        now = _read_settings(s, [key]).settings.get(key)
        if ok and _match(now, sent):
            applied[key] = now
        else:
            failed[key] = f"Resolve kept {now!r}." if ok else "Resolve returned False."
    if not applied:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            "No project setting could be applied.",
            details={"not_applied": failed},
            retryable=False,
        )
    return SetSettingsResult(applied=applied, not_applied=failed)


# -- write: settings presets ------------------------------------------------------


def _verify_preset_listed(s: ResolveSession, p: PresetNameParams, _: Done) -> Verification:
    names = _preset_names(s)
    return Verification(ok=p.name in names, observed={"presets": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=Done,
    verify=_verify_preset_listed,
)
def save_settings_preset(s: ResolveSession, p: PresetNameParams) -> Done:
    """Save the current project settings as a new preset."""
    if p.name in _preset_names(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"Preset {p.name!r} exists; use update_settings_preset.",
            retryable=False,
        )
    if not s.project.SaveCurrentProjectSettingsAsNewPreset(p.name):
        raise _fail("Project.SaveCurrentProjectSettingsAsNewPreset")
    return Done(detail=f"Saved preset {p.name!r}.")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=Done,
    verify_reason="UpdateProjectSettingsPreset has no read-back of preset content.",
)
def update_settings_preset(s: ResolveSession, p: PresetNameParams) -> Done:
    """Overwrite a settings preset with the current project settings."""
    if p.name not in _preset_names(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No preset {p.name!r}.", retryable=False)
    if not s.project.UpdateProjectSettingsPreset(p.name):
        raise _fail("Project.UpdateProjectSettingsPreset")
    return Done(detail=f"Updated preset {p.name!r}.")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PresetNameParams,
    result=Done,
    verify_reason="Applying a preset rewrites many settings; use get_settings to inspect them.",
)
def apply_settings_preset(s: ResolveSession, p: PresetNameParams) -> Done:
    """Apply a settings preset to the current project (saved project only)."""
    if p.name not in _preset_names(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No preset {p.name!r}.", retryable=False)
    _require_saved(s)
    if not s.project.SetProjectSettingsPreset(p.name):
        raise _fail("Project.SetProjectSettingsPreset")
    return Done(detail=f"Applied preset {p.name!r}.")


class ImportPresetParams(_Params):
    path: str = Field(description="Preset file to import (inside the allowed roots).")
    name: str | None = Field(default=None, description="Name given to the imported preset.")


def _verify_imported_preset(
    s: ResolveSession, p: ImportPresetParams, r: TransferResult
) -> Verification:
    names = _preset_names(s)
    ok = p.name in names if p.name else len(names) > r.projects_before
    return Verification(ok=ok, observed={"presets": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportPresetParams,
    result=TransferResult,
    verify=_verify_imported_preset,
)
def import_settings_preset(s: ResolveSession, p: ImportPresetParams) -> TransferResult:
    """Import a project settings preset file."""
    path = _safe_path(p.path, must_exist=True)
    before = len(_preset_names(s))
    ok = (
        s.project.ImportProjectSettingsPreset(str(path), p.name)
        if p.name
        else s.project.ImportProjectSettingsPreset(str(path))
    )
    if not ok:
        raise _fail("Project.ImportProjectSettingsPreset")
    return TransferResult(name=p.name, path=str(path), projects_before=before)


class ExportPresetParams(PresetNameParams):
    path: str = Field(description="Destination file (inside the allowed roots).")


def _verify_file(path: str) -> Verification:
    exists = Path(path).exists()
    return Verification(ok=exists, observed={"exists": exists})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportPresetParams,
    result=TransferResult,
    verify=lambda _s, _p, r: _verify_file(r.path or ""),
)
def export_settings_preset(s: ResolveSession, p: ExportPresetParams) -> TransferResult:
    """Export a project settings preset to a file."""
    if p.name not in _preset_names(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No preset {p.name!r}.", retryable=False)
    path = _safe_path(p.path, must_exist=False)
    if path.exists():
        raise DavinciError(ErrorCode.WRONG_STATE, f"{path} already exists.", retryable=False)
    if not s.project.ExportProjectSettingsPreset(p.name, str(path)):
        raise _fail("Project.ExportProjectSettingsPreset")
    return TransferResult(name=p.name, path=str(path))


# -- write: import / export / restore / archive -------------------------------------


class ImportProjectParams(_Params):
    path: str = Field(description="A .drp file (inside the allowed roots).")
    name: str | None = Field(default=None, description="Project name (defaults to the file's).")


def _verify_imported(s: ResolveSession, p: ImportProjectParams, r: TransferResult) -> Verification:
    names = _projects(s)
    ok = p.name in names if p.name else len(names) > r.projects_before
    return Verification(ok=ok, observed={"projects_count": len(names), "listed": ok})


def _import(s: ResolveSession, p: ImportProjectParams, method: str) -> TransferResult:
    path = _safe_path(p.path, must_exist=True)
    if p.name and p.name in _projects(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"Project {p.name!r} already exists.", retryable=False
        )
    before = len(_projects(s))
    fn = getattr(s.project_manager, method)
    ok = fn(str(path), p.name) if p.name else fn(str(path))
    if not ok:
        raise _fail(f"ProjectManager.{method}", "The name may exist already, or the file is bad.")
    return TransferResult(name=p.name, path=str(path), projects_before=before)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportProjectParams,
    result=TransferResult,
    verify=_verify_imported,
)
def import_project(s: ResolveSession, p: ImportProjectParams) -> TransferResult:
    """Import a .drp project file into the current folder."""
    return _import(s, p, "ImportProject")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportProjectParams,
    result=TransferResult,
    verify=_verify_imported,
)
def restore_project(s: ResolveSession, p: ImportProjectParams) -> TransferResult:
    """Restore a project from an archive (.dra folder) into the current folder."""
    return _import(s, p, "RestoreProject")


class ExportProjectParams(NameParams):
    path: str = Field(description="Destination .drp (inside the allowed roots).")
    with_stills_and_luts: bool = True


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportProjectParams,
    result=TransferResult,
    verify=lambda _s, _p, r: _verify_file(r.path or ""),
)
def export_project(s: ResolveSession, p: ExportProjectParams) -> TransferResult:
    """Export a project of the current folder to a .drp file."""
    if p.name not in _projects(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No project {p.name!r} here.", retryable=False)
    path = _safe_path(p.path, must_exist=False)
    if path.exists():
        raise DavinciError(ErrorCode.WRONG_STATE, f"{path} already exists.", retryable=False)
    if not s.project_manager.ExportProject(p.name, str(path), p.with_stills_and_luts):
        raise _fail("ProjectManager.ExportProject")
    return TransferResult(name=p.name, path=str(path))


class ArchiveProjectParams(ExportProjectParams):
    archive_src_media: bool = True
    archive_render_cache: bool = True
    archive_proxy_media: bool = False
    force: bool = Field(
        default=False, description="Run the call despite its known failures (may crash Resolve)."
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ArchiveProjectParams,
    result=TransferResult,
    verify=lambda _s, _p, r: _verify_file(r.path or ""),
    timeout_s=300.0,
)
def archive_project(s: ResolveSession, p: ArchiveProjectParams) -> TransferResult:
    """Archive a project (broken by script on 21.x; needs force=true)."""
    if not p.force:
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "ArchiveProject never produces an archive by script on 21.x.",
            hint="It returns False instantly without media and SIGSEGVs with media/proxies. "
            "Use export_project (.drp) instead, or pass force=true at your own risk.",
            retryable=False,
        )
    if p.name not in _projects(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No project {p.name!r} here.", retryable=False)
    path = _safe_path(p.path, must_exist=False)
    ok = s.project_manager.ArchiveProject(
        p.name, str(path), p.archive_src_media, p.archive_render_cache, p.archive_proxy_media
    )
    if not ok:
        raise _fail("ProjectManager.ArchiveProject")
    return TransferResult(name=p.name, path=str(path))


# -- destructive -----------------------------------------------------------------


def _plan_delete_project(s: ResolveSession, p: NameParams) -> PlanDraft:
    if p.name not in _projects(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No project {p.name!r} here.", retryable=False)
    if p.name == _current_name(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"{p.name!r} is the current project; DeleteProject fails on it.",
            hint="Close it first with close_project (not done silently), then retry.",
            retryable=False,
        )
    pm = s.project_manager
    last = (
        pm.GetProjectLastModifiedTime(p.name)
        if s.caps.has(pm, "GetProjectLastModifiedTime")
        else None
    )
    return PlanDraft(
        summary=f"Delete project {p.name!r} from the current database folder.",
        effects=[
            "The project and its timelines are removed from the database.",
            "Export a .drp first if you may need it back (export_project).",
        ],
        targets=[f"project:{p.name}"],
        fingerprint=_fingerprint({"name": p.name, "modified": last}),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=NameParams,
    result=DeletedResult,
    plan=_plan_delete_project,
    verify=lambda s, p, _r: Verification(
        ok=p.name not in _projects(s), observed={"still_listed": p.name in _projects(s)}
    ),
)
def delete_project(s: ResolveSession, p: NameParams) -> DeletedResult:
    """Delete a (non-current) project from the current folder."""
    if p.name == _current_name(s):
        raise DavinciError(
            ErrorCode.WRONG_STATE, "Cannot delete the current project.", retryable=False
        )
    if not s.project_manager.DeleteProject(p.name):
        raise _fail(
            "ProjectManager.DeleteProject",
            "Fails on a current or recently-current project (first try is flaky): "
            "close it and retry.",
        )
    return DeletedResult(deleted=p.name)


def _plan_delete_folder(s: ResolveSession, p: NameParams) -> PlanDraft:
    if p.name not in _folders(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No folder {p.name!r} here.", retryable=False)
    return PlanDraft(
        summary=f"Delete project folder {p.name!r}.",
        effects=["The folder and the projects it contains may be removed from the database."],
        targets=[f"folder:{p.name}"],
        fingerprint=_fingerprint({"folder": p.name, "siblings": _folders(s)}),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=NameParams,
    result=DeletedResult,
    plan=_plan_delete_folder,
    verify=lambda s, p, _r: Verification(
        ok=p.name not in _folders(s), observed={"folders": _folders(s)}
    ),
)
def delete_folder(s: ResolveSession, p: NameParams) -> DeletedResult:
    """Delete a project folder of the current folder."""
    if not s.project_manager.DeleteFolder(p.name):
        raise _fail(
            "ProjectManager.DeleteFolder", "The folder may be non-empty or the current one."
        )
    return DeletedResult(deleted=p.name)


def _plan_delete_preset(s: ResolveSession, p: PresetNameParams) -> PlanDraft:
    if p.name not in _preset_names(s):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No preset {p.name!r}.", retryable=False)
    return PlanDraft(
        summary=f"Delete project settings preset {p.name!r}.",
        effects=["The preset is removed; projects already using its values are unaffected."],
        targets=[f"settings_preset:{p.name}"],
        fingerprint=_fingerprint({"preset": p.name, "all": _preset_names(s)}),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=PresetNameParams,
    result=DeletedResult,
    plan=_plan_delete_preset,
    verify=lambda s, p, _r: Verification(
        ok=p.name not in _preset_names(s), observed={"presets": _preset_names(s)}
    ),
)
def delete_settings_preset(s: ResolveSession, p: PresetNameParams) -> DeletedResult:
    """Delete a project settings preset."""
    if not s.project.DeleteProjectSettingsPreset(p.name):
        raise _fail("Project.DeleteProjectSettingsPreset")
    return DeletedResult(deleted=p.name)


ACTIONS = [
    list_projects,
    get_current_project,
    list_databases,
    get_settings,
    list_settings_presets,
    create_project,
    load_project,
    save_project,
    close_project,
    rename_project,
    create_folder,
    open_folder,
    goto_folder,
    set_database,
    set_settings,
    save_settings_preset,
    update_settings_preset,
    apply_settings_preset,
    import_settings_preset,
    export_settings_preset,
    import_project,
    restore_project,
    export_project,
    archive_project,
    delete_project,
    delete_folder,
    delete_settings_preset,
]
