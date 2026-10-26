# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: ANN401
"""Media domain: MediaStorage, MediaPool, Folder and MediaPoolItem (incl. clip markers).

Excluded on purpose (other domains): timeline creation/append, transcription, AI
analysis, audio classification. Traps encoded here (research/audit-existant.md 6.2 D):

- ``ImportMedia`` / ``AddItemListToMediaPool`` always land in the *current* folder:
  the target folder is selected first and the previous one restored afterwards.
- ``SetClipColor`` only accepts the 16 clip colors (typed ``ClipColor``), and silently
  ignores generators/titles: every colour write is read back.
- ``SetClipProperty`` / ``SetMetadata`` return True without persisting some keys
  (e.g. ``Reel Name``): every property write is read back.
- ``AUDIO_SYNC_*`` / ``MULTICAM_*`` / ``CLONE_CHECKSUM_*`` are only valid as live
  constants (``resolve.NAME``): string names are converted with ``const``.
- ``GetClipProperty('Transcription')`` is truncated: not exposed here (ai domain).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import JobRequest, Kind, PlanDraft, action
from davinci_mcp.api._generated.resolve_21_1 import (
    AudioSyncSettings,
    ClipColor,
    CloneChecksumType,
    FlagColor,
    MarkerColor,
    MarkType,
    MulticamOptions,
)
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import (
    ClipRef,
    FolderRef,
    const,
    proxies,
    resolve_clip,
    resolve_folder,
    walk_folders,
)
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "media"
V21_1 = (21, 1, 0)
DEFAULT_PROPERTY_KEYS = (
    "Clip Name",
    "File Path",
    "Type",
    "Duration",
    "FPS",
    "Resolution",
    "Start TC",
    "Video Codec",
    "Online Status",
    "Proxy Media Path",
)
_COMPLETE_PCT = 100
_MIN_SYNC_CLIPS = 2
_CONST_PREFIXES = ("AUDIO_SYNC_", "MULTICAM_", "CLONE_CHECKSUM_")


# -- params / results -----------------------------------------------------------------


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Volumes(BaseModel):
    volumes: list[str]


class BrowseParams(_Params):
    path: str = Field(description="Absolute folder path in Media Storage.")
    limit: int = Field(default=200, ge=1, le=2000)
    offset: int = Field(default=0, ge=0)


class StorageListing(BaseModel):
    path: str
    subfolders: list[str]
    files: list[str]
    total_files: int
    offset: int


class TreeParams(_Params):
    max_depth: int | None = Field(default=None, ge=0, description="0 = root only.")


class FolderInfo(BaseModel):
    id: str
    name: str
    path: str
    depth: int
    subfolders: int
    clips: int


class FolderTree(BaseModel):
    current_folder_id: str | None
    folders: list[FolderInfo]


class ListClipsParams(_Params):
    folder: FolderRef | None = Field(default=None, description="Default: root folder.")
    recursive: bool = False
    name_contains: str | None = Field(default=None, description="Case-insensitive substring.")
    type: str | None = Field(default=None, description="Substring of the 'Type' property.")
    clip_color: ClipColor | None = None
    flag: FlagColor | None = None
    keys: list[str] = Field(
        default_factory=list, description="Clip property keys to include (default none)."
    )
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class ClipSummary(BaseModel):
    id: str
    name: str
    folder: str | None = None
    clip_color: str | None = None
    flags: list[str] | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class ClipList(BaseModel):
    total: int
    offset: int
    clips: list[ClipSummary]


class GetClipParams(_Params):
    clip: ClipRef
    keys: list[str] | None = Field(
        default=None, description="Property keys (default: a short standard set)."
    )
    all_properties: bool = Field(default=False, description="Return every clip property.")


class Marker(BaseModel):
    frame: int = Field(description="Frame offset from the clip start.")
    color: str
    name: str
    note: str
    duration: int
    custom_data: str


class ClipDetails(BaseModel):
    id: str
    name: str
    media_id: str | None
    folder: str | None
    clip_color: str
    flags: list[str]
    is_timeline: bool
    online_status: str | None
    proxy_media_path: str | None
    mark_in_out: dict[str, Any]
    marker_count: int
    properties: dict[str, Any]
    metadata: dict[str, Any]
    third_party_metadata: dict[str, Any]


class ClipOnly(_Params):
    clip: ClipRef


class MarkerList(BaseModel):
    clip_id: str
    markers: list[Marker]


class FolderInfoShort(BaseModel):
    id: str
    path: str


class Selection(BaseModel):
    current_folder: FolderInfoShort
    selected_clips: list[ClipSummary]


class AudioMapping(BaseModel):
    clip_id: str
    raw: str
    parsed: Any = None


class CloneStatusOut(BaseModel):
    job_status: str | None
    completion_percentage: float | None
    error: str | None


class MatteParams(_Params):
    clip: ClipRef | None = Field(default=None, description="Clip mattes of this clip.")
    folder: FolderRef | None = Field(default=None, description="Else timeline mattes of folder.")


class MatteList(BaseModel):
    clip_mattes: list[str]
    timeline_mattes: list[ClipSummary]


class ImportItem(_Params):
    path: str = Field(description="File, folder or image-sequence pattern ('%06d').")

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: object) -> object:
        return {"path": value} if isinstance(value, str) else value

    start_index: int | None = Field(default=None, ge=0, description="Sequence start frame.")
    end_index: int | None = Field(default=None, ge=0, description="Sequence end frame.")


class ImportParams(_Params):
    items: list[ImportItem] = Field(min_length=1)
    folder: FolderRef | None = Field(default=None, description="Target folder (default: current).")
    via_media_storage: bool = Field(
        default=False, description="Use MediaStorage.AddItemListToMediaPool instead of ImportMedia."
    )


class ImportResult(BaseModel):
    imported: list[ClipSummary]
    folder_id: str
    unmatched_paths: list[str]


class CreateFolderParams(_Params):
    name: str = Field(min_length=1)
    parent: FolderRef | None = Field(default=None, description="Default: root folder.")
    set_current: bool = False


class FolderOut(BaseModel):
    id: str
    name: str
    path: str


class SetFolderParams(_Params):
    folder: FolderRef


class MoveClipsParams(_Params):
    clips: list[ClipRef] = Field(min_length=1)
    target: FolderRef


class MoveFoldersParams(_Params):
    folders: list[FolderRef] = Field(min_length=1)
    target: FolderRef


class ClipIds(BaseModel):
    clip_ids: list[str]


class FolderIds(BaseModel):
    folder_ids: list[str]


class SetPropertiesParams(_Params):
    clip: ClipRef
    properties: dict[str, str] = Field(min_length=1, description="Clip property key -> value.")


class SetPropertiesResult(BaseModel):
    clip_id: str
    applied: dict[str, str]
    rejected: list[str]


class SetMetadataParams(_Params):
    clip: ClipRef
    metadata: dict[str, str] = Field(min_length=1)
    third_party: bool = Field(default=False, description="Write third-party metadata.")


class SetNameParams(_Params):
    clip: ClipRef
    name: str = Field(min_length=1)


class SetColorParams(_Params):
    clip: ClipRef
    color: ClipColor | None = Field(
        default=None, description="One of the 16 clip colors; null clears the color."
    )


class ClipColorOut(BaseModel):
    clip_id: str
    clip_color: str


class FlagParams(_Params):
    clip: ClipRef
    color: FlagColor


class ClearFlagsParams(_Params):
    clip: ClipRef
    color: FlagColor | Literal["All"] = "All"


class FlagsOut(BaseModel):
    clip_id: str
    flags: list[str]


class AddMarkerParams(_Params):
    clip: ClipRef
    frame: int = Field(ge=0, description="Frame offset from the clip start.")
    color: MarkerColor = "Blue"
    name: str = ""
    note: str = ""
    duration: int = Field(default=1, ge=1)
    custom_data: str = ""


class MarkerCustomDataParams(_Params):
    clip: ClipRef
    frame: int = Field(ge=0)
    custom_data: str


class MarkInOutParams(_Params):
    clip: ClipRef
    mark_in: int = Field(ge=0)
    mark_out: int = Field(ge=0)
    mark_type: MarkType = "all"


class ClearMarkInOutParams(_Params):
    clip: ClipRef
    mark_type: MarkType = "all"


class MarkInOutOut(BaseModel):
    clip_id: str
    mark_in_out: dict[str, Any]


class PathClipParams(_Params):
    clip: ClipRef
    path: str = Field(description="Absolute file path (must exist, inside allowed roots).")


class ReplaceClipParams(PathClipParams):
    preserve_sub_clip: bool = Field(default=False, description="Keep original sub-clip extents.")


class PathOut(BaseModel):
    clip_id: str
    path: str


class RelinkParams(_Params):
    clips: list[ClipRef] = Field(min_length=1)
    folder_path: str = Field(description="Existing directory that now holds the media.")


class MulticamParams(_Params):
    clips: list[ClipRef] = Field(min_length=2)
    options: MulticamOptions = Field(default_factory=MulticamOptions)


class AutoSyncParams(_Params):
    clips: list[ClipRef] = Field(min_length=2, description="At least one video and one audio clip.")
    settings: AudioSyncSettings = Field(default_factory=AudioSyncSettings)


class CloneParams(_Params):
    source_dir: str
    target_dirs: list[str] = Field(min_length=1)
    preserve_folder_name: bool | None = None
    checksum_type: CloneChecksumType | None = None


class MattesParams(_Params):
    clip: ClipRef
    paths: list[str] = Field(min_length=1)
    stereo_eye: Literal["left", "right"] | None = None


class TimelineMattesParams(_Params):
    paths: list[str] = Field(min_length=1)


class StereoParams(_Params):
    left: ClipRef
    right: ClipRef


class DrbImportParams(_Params):
    path: str = Field(description="DRB file to import.")
    source_clips_path: str | None = None


class DrbExportParams(_Params):
    folder: FolderRef
    path: str = Field(description="Destination .drb path.")


class ExportMetadataParams(_Params):
    path: str = Field(description="Destination .csv path.")
    clips: list[ClipRef] | None = Field(default=None, description="Default: all clips.")


class WrittenFile(BaseModel):
    path: str
    size: int


class RevealParams(_Params):
    path: str


class Done(BaseModel):
    ok: bool = True
    detail: str | None = None


class DeleteClipsParams(_Params):
    clips: list[ClipRef] = Field(min_length=1)


class DeleteFoldersParams(_Params):
    folders: list[FolderRef] = Field(min_length=1)


class DeleteMarkersParams(_Params):
    clip: ClipRef
    frame: int | None = Field(default=None, ge=0)
    color: MarkerColor | Literal["All"] | None = None
    custom_data: str | None = None

    @model_validator(mode="after")
    def _one(self) -> DeleteMarkersParams:
        if sum(v is not None for v in (self.frame, self.color, self.custom_data)) != 1:
            raise ValueError("use exactly one of frame, color or custom_data")
        return self


class DeletedMarkers(BaseModel):
    clip_id: str
    deleted: list[Marker]


class UnlinkClipsParams(_Params):
    clips: list[ClipRef] = Field(min_length=1)


class DeleteMattesParams(_Params):
    clip: ClipRef
    paths: list[str] = Field(min_length=1)


class ClearMetadataParams(_Params):
    clip: ClipRef
    keys: list[str] = Field(min_length=1, description="Metadata keys to blank.")
    third_party: bool = False


# -- helpers --------------------------------------------------------------------------


def _fp(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _failed(what: str, hint: str | None = None) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, f"{what} returned False.", hint=hint, retryable=False
    )


def _path(raw: str, *, must_exist: bool = False) -> str:
    return str(check_path(raw, runtime().allowed_roots, must_exist=must_exist))


def _same_path(a: object, b: str) -> bool:
    if not isinstance(a, str) or not a:
        return False
    return a == b or Path(a).resolve() == Path(b).resolve()


def _pool(s: ResolveSession) -> ResolveObject:
    return s.project.GetMediaPool()


def _storage(s: ResolveSession) -> ResolveObject:
    return s.resolve.GetMediaStorage()


def _scan(s: ResolveSession) -> list[tuple[str, ResolveObject, ResolveObject]]:
    """Fresh (folder path, folder, clip) walk (never the cached clip index)."""
    out: list[tuple[str, ResolveObject, ResolveObject]] = []
    for path, folder in walk_folders(_pool(s).GetRootFolder()):
        out.extend((path, folder, clip) for clip in proxies(folder.GetClipList()))
    return out


def _clip_folder(s: ResolveSession, clip: ResolveObject) -> str | None:
    uid = clip.GetUniqueId()
    return next((p for p, _, c in _scan(s) if c.GetUniqueId() == uid), None)


def _folder_path(s: ResolveSession, folder: ResolveObject) -> str:
    uid = folder.GetUniqueId()
    for path, f in walk_folders(_pool(s).GetRootFolder()):
        if f.GetUniqueId() == uid:
            return path
    return str(folder.GetName())


def _clips(s: ResolveSession, refs: Sequence[ClipRef]) -> list[ResolveObject]:
    out: list[ResolveObject] = []
    seen: set[str] = set()
    for ref in refs:
        clip = resolve_clip(s, ref)
        if clip.GetUniqueId() not in seen:
            seen.add(clip.GetUniqueId())
            out.append(clip)
    return out


def _folders(s: ResolveSession, refs: Sequence[FolderRef]) -> list[ResolveObject]:
    root_id = _pool(s).GetRootFolder().GetUniqueId()
    out: list[ResolveObject] = []
    for ref in refs:
        folder = resolve_folder(s, ref)
        if folder.GetUniqueId() == root_id:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS,
                "The root folder cannot be selected here.",
                retryable=False,
            )
        out.append(folder)
    return out


def _ids(objs: Sequence[ResolveObject]) -> list[str]:
    return [str(o.GetUniqueId()) for o in objs]


def _summary(clip: ResolveObject, folder: str | None = None) -> ClipSummary:
    return ClipSummary(id=clip.GetUniqueId(), name=clip.GetName(), folder=folder)


def _prop(clip: ResolveObject, key: str) -> Any:
    return clip.GetClipProperty(key)


def _markers(clip: ResolveObject) -> dict[int, Marker]:
    raw: dict[Any, Any] = clip.GetMarkers() or {}
    return {
        int(frame): Marker(
            frame=int(frame),
            color=str(info.get("color", "")),
            name=str(info.get("name", "")),
            note=str(info.get("note", "")),
            duration=int(info.get("duration", 1)),
            custom_data=str(info.get("customData", "")),
        )
        for frame, info in raw.items()
    }


def _live(s: ResolveSession, value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_CONST_PREFIXES):
        return const(s, value)
    return value


def _live_dict(s: ResolveSession, model: BaseModel) -> dict[str, Any]:
    data = model.model_dump(exclude_none=True, by_alias=True)
    return {k: _live(s, v) for k, v in data.items()}


def _dict(value: Any) -> dict[str, Any]:
    return dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}


# -- read: storage --------------------------------------------------------------------


@action(domain=DOMAIN, kind=Kind.READ, result=Volumes)
def list_volumes(s: ResolveSession, _: BaseModel) -> Volumes:
    """List the volumes mounted in Media Storage."""
    return Volumes(volumes=[str(v) for v in proxies(_storage(s).GetMountedVolumeList())])


@action(domain=DOMAIN, kind=Kind.READ, params=BrowseParams, result=StorageListing)
def browse_storage(s: ResolveSession, p: BrowseParams) -> StorageListing:
    """List subfolders and (paginated) files of a Media Storage folder."""
    path = _path(p.path, must_exist=True)
    storage = _storage(s)
    files = [str(f) for f in proxies(storage.GetFileList(path))]
    return StorageListing(
        path=path,
        subfolders=[str(f) for f in proxies(storage.GetSubFolderList(path))],
        files=files[p.offset : p.offset + p.limit],
        total_files=len(files),
        offset=p.offset,
    )


# -- read: media pool -----------------------------------------------------------------


@action(domain=DOMAIN, kind=Kind.READ, params=TreeParams, result=FolderTree)
def media_pool_tree(s: ResolveSession, p: TreeParams) -> FolderTree:
    """Media Pool folder tree with ids, paths and clip counts."""
    pool = _pool(s)
    current = pool.GetCurrentFolder()
    folders: list[FolderInfo] = []
    for path, folder in walk_folders(pool.GetRootFolder()):
        depth = path.count("/")
        if p.max_depth is not None and depth > p.max_depth:
            continue
        folders.append(
            FolderInfo(
                id=folder.GetUniqueId(),
                name=folder.GetName(),
                path=path,
                depth=depth,
                subfolders=len(proxies(folder.GetSubFolderList())),
                clips=len(proxies(folder.GetClipList())),
            )
        )
    return FolderTree(
        current_folder_id=current.GetUniqueId() if current is not None else None, folders=folders
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ListClipsParams, result=ClipList)
def list_clips(s: ResolveSession, p: ListClipsParams) -> ClipList:
    """List clips of a folder with filters, pagination and selectable properties."""
    target = resolve_folder(s, p.folder)
    target_uid = target.GetUniqueId()
    target_path = _folder_path(s, target)
    candidates: list[tuple[str, ResolveObject]] = []
    for path, folder in walk_folders(_pool(s).GetRootFolder()):
        inside = path == target_path or (p.recursive and path.startswith(target_path + "/"))
        if folder.GetUniqueId() == target_uid or inside:
            candidates.extend((path, c) for c in proxies(folder.GetClipList()))
    needle = p.name_contains.lower() if p.name_contains else None
    matched: list[tuple[str, ResolveObject]] = []
    for path, clip in candidates:
        if needle and needle not in str(clip.GetName()).lower():
            continue
        if p.type and p.type.lower() not in str(_prop(clip, "Type") or "").lower():
            continue
        if p.clip_color and clip.GetClipColor() != p.clip_color:
            continue
        if p.flag and p.flag not in proxies(clip.GetFlagList()):
            continue
        matched.append((path, clip))
    page = matched[p.offset : p.offset + p.limit]
    clips = [
        ClipSummary(
            id=c.GetUniqueId(),
            name=c.GetName(),
            folder=path,
            clip_color=c.GetClipColor() or "",
            flags=[str(f) for f in proxies(c.GetFlagList())],
            properties={k: _prop(c, k) for k in p.keys},
        )
        for path, c in page
    ]
    return ClipList(total=len(matched), offset=p.offset, clips=clips)


@action(domain=DOMAIN, kind=Kind.READ, params=GetClipParams, result=ClipDetails)
def get_clip(s: ResolveSession, p: GetClipParams) -> ClipDetails:
    """Full details of one clip: properties, metadata, flags, color, proxy, markers."""
    clip = resolve_clip(s, p.clip)
    if p.all_properties:
        props = _dict(clip.GetClipProperty())
    else:
        props = {k: _prop(clip, k) for k in (p.keys or DEFAULT_PROPERTY_KEYS)}
    timeline = clip.GetTimeline() if s.caps.has(clip, "GetTimeline") else None
    return ClipDetails(
        id=clip.GetUniqueId(),
        name=clip.GetName(),
        media_id=clip.GetMediaId(),
        folder=_clip_folder(s, clip),
        clip_color=clip.GetClipColor() or "",
        flags=[str(f) for f in proxies(clip.GetFlagList())],
        is_timeline=timeline is not None,
        online_status=_prop(clip, "Online Status"),
        proxy_media_path=_prop(clip, "Proxy Media Path") or None,
        mark_in_out=_dict(clip.GetMarkInOut()),
        marker_count=len(_markers(clip)),
        properties=props,
        metadata=_dict(clip.GetMetadata()),
        third_party_metadata=_dict(clip.GetThirdPartyMetadata()),
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ClipOnly, result=MarkerList)
def get_clip_markers(s: ResolveSession, p: ClipOnly) -> MarkerList:
    """List the markers of a Media Pool clip."""
    clip = resolve_clip(s, p.clip)
    markers = sorted(_markers(clip).values(), key=lambda m: m.frame)
    return MarkerList(clip_id=clip.GetUniqueId(), markers=markers)


@action(domain=DOMAIN, kind=Kind.READ, result=Selection)
def get_selection(s: ResolveSession, _: BaseModel) -> Selection:
    """Current Media Pool folder and selected clips."""
    pool = _pool(s)
    folder = pool.GetCurrentFolder()
    return Selection(
        current_folder=FolderInfoShort(id=folder.GetUniqueId(), path=_folder_path(s, folder)),
        selected_clips=[_summary(c) for c in proxies(pool.GetSelectedClips())],
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ClipOnly, result=AudioMapping)
def get_audio_mapping(s: ResolveSession, p: ClipOnly) -> AudioMapping:
    """Audio mapping of a clip (raw JSON string plus parsed value)."""
    clip = resolve_clip(s, p.clip)
    raw = str(clip.GetAudioMapping() or "")
    parsed: Any = None
    if raw:
        with contextlib.suppress(ValueError):
            parsed = json.loads(raw)
    return AudioMapping(clip_id=clip.GetUniqueId(), raw=raw, parsed=parsed)


@action(domain=DOMAIN, kind=Kind.READ, result=CloneStatusOut, since=V21_1)
def get_clone_status(s: ResolveSession, _: BaseModel) -> CloneStatusOut:
    """Status of the current or most recent clone job."""
    status = _dict(_storage(s).GetCloneStatus())
    pct = status.get("CompletionPercentage")
    return CloneStatusOut(
        job_status=status.get("JobStatus"),
        completion_percentage=float(pct) if isinstance(pct, (int, float)) else None,
        error=status.get("Error") or None,
    )


@action(domain=DOMAIN, kind=Kind.READ, params=MatteParams, result=MatteList)
def list_mattes(s: ResolveSession, p: MatteParams) -> MatteList:
    """List the mattes of a clip, or the timeline mattes of a folder."""
    pool = _pool(s)
    clip_mattes: list[str] = []
    if p.clip is not None:
        clip_mattes = [str(m) for m in proxies(pool.GetClipMatteList(resolve_clip(s, p.clip)))]
    folder = resolve_folder(s, p.folder)
    return MatteList(
        clip_mattes=clip_mattes,
        timeline_mattes=[_summary(m) for m in proxies(pool.GetTimelineMatteList(folder))],
    )


# -- write: import / folders ----------------------------------------------------------


def _info_path(info: dict[str, Any] | str) -> str:
    return info if isinstance(info, str) else str(info.get("FilePath") or info.get("media"))


def _is_sequence(info: dict[str, Any] | str) -> bool:
    return isinstance(info, dict) and ("StartIndex" in info or "startFrame" in info)


def _import_item(s: ResolveSession, item: ImportItem, storage: bool) -> dict[str, Any] | str:
    sequence = item.start_index is not None or item.end_index is not None or "%" in item.path
    path = _path(item.path, must_exist=not sequence)
    if storage:
        info: dict[str, Any] = {"media": path}
        if item.start_index is not None:
            info["startFrame"] = item.start_index
        if item.end_index is not None:
            info["endFrame"] = item.end_index
        return info
    if not sequence:
        # Measured on Resolve 21.1.1: ImportMedia([{"FilePath": p}]) returns None although the
        # stub documents it; plain path strings work. Dicts are kept for image sequences.
        return path
    info = {"FilePath": path}
    if item.start_index is not None:
        info["StartIndex"] = item.start_index
    if item.end_index is not None:
        info["EndIndex"] = item.end_index
    return info


def _verify_import(s: ResolveSession, p: ImportParams, result: ImportResult) -> Verification:
    folder = resolve_folder(s, FolderRef(id=result.folder_id))
    present = set(_ids(proxies(folder.GetClipList())))
    missing = [c.id for c in result.imported if c.id not in present]
    return Verification(
        ok=bool(result.imported) and not missing,
        observed={"missing_from_folder": missing, "imported": len(result.imported)},
    )


@action(
    domain=DOMAIN, kind=Kind.WRITE, params=ImportParams, result=ImportResult, verify=_verify_import
)
def import_media(s: ResolveSession, p: ImportParams) -> ImportResult:
    """Import files/folders/sequences into the current or a chosen Media Pool folder."""
    pool = _pool(s)
    infos = [_import_item(s, item, p.via_media_storage) for item in p.items]
    previous = pool.GetCurrentFolder()
    target = resolve_folder(s, p.folder) if p.folder is not None else previous
    switched = target.GetUniqueId() != previous.GetUniqueId()
    if switched and not pool.SetCurrentFolder(target):
        raise _failed("MediaPool.SetCurrentFolder")
    try:
        if p.via_media_storage:
            created = _storage(s).AddItemListToMediaPool(infos)
        else:
            created = pool.ImportMedia(infos)
    finally:
        if switched:
            pool.SetCurrentFolder(previous)
    clips = proxies(created)
    if not clips:
        raise _failed(
            "Media import",
            "Check the paths, unsupported formats, or whether the clips already exist.",
        )
    imported_paths = [_prop(c, "File Path") for c in clips]
    unmatched = [
        _info_path(info)
        for info in infos
        if not _is_sequence(info)
        and not Path(_info_path(info)).is_dir()
        and not any(_same_path(ip, _info_path(info)) for ip in imported_paths)
    ]
    return ImportResult(
        imported=[_summary(c, _folder_path(s, target)) for c in clips],
        folder_id=target.GetUniqueId(),
        unmatched_paths=unmatched,
    )


def _verify_folder(s: ResolveSession, p: CreateFolderParams, result: FolderOut) -> Verification:
    parent = resolve_folder(s, p.parent)
    names = [f.GetName() for f in proxies(parent.GetSubFolderList())]
    return Verification(ok=p.name in names, observed={"subfolders": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CreateFolderParams,
    result=FolderOut,
    verify=_verify_folder,
)
def create_folder(s: ResolveSession, p: CreateFolderParams) -> FolderOut:
    """Create a subfolder in the Media Pool."""
    pool = _pool(s)
    parent = resolve_folder(s, p.parent)
    if p.name in [f.GetName() for f in proxies(parent.GetSubFolderList())]:
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"Folder {p.name!r} already exists there.", retryable=False
        )
    folder = pool.AddSubFolder(parent, p.name)
    if folder is None:
        raise _failed("MediaPool.AddSubFolder")
    if p.set_current:
        pool.SetCurrentFolder(folder)
    return FolderOut(
        id=folder.GetUniqueId(), name=p.name, path=f"{_folder_path(s, parent)}/{p.name}"
    )


def _verify_current(s: ResolveSession, p: SetFolderParams, result: FolderOut) -> Verification:
    now = _pool(s).GetCurrentFolder()
    return Verification(
        ok=now is not None and now.GetUniqueId() == result.id,
        observed={"current_id": now.GetUniqueId() if now is not None else None},
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetFolderParams,
    result=FolderOut,
    verify=_verify_current,
    idempotent=True,
)
def set_current_folder(s: ResolveSession, p: SetFolderParams) -> FolderOut:
    """Make a folder the current Media Pool folder."""
    folder = resolve_folder(s, p.folder)
    if not _pool(s).SetCurrentFolder(folder):
        raise _failed("MediaPool.SetCurrentFolder")
    return FolderOut(id=folder.GetUniqueId(), name=folder.GetName(), path=_folder_path(s, folder))


def _verify_moved_clips(s: ResolveSession, p: MoveClipsParams, result: ClipIds) -> Verification:
    target = resolve_folder(s, p.target)
    present = set(_ids(proxies(target.GetClipList())))
    missing = [i for i in result.clip_ids if i not in present]
    return Verification(ok=not missing, observed={"missing_in_target": missing})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=MoveClipsParams,
    result=ClipIds,
    verify=_verify_moved_clips,
)
def move_clips(s: ResolveSession, p: MoveClipsParams) -> ClipIds:
    """Move clips to another Media Pool folder."""
    clips = _clips(s, p.clips)
    if not _pool(s).MoveClips(clips, resolve_folder(s, p.target)):
        raise _failed("MediaPool.MoveClips")
    return ClipIds(clip_ids=_ids(clips))


def _verify_moved_folders(
    s: ResolveSession, p: MoveFoldersParams, result: FolderIds
) -> Verification:
    target = resolve_folder(s, p.target)
    present = set(_ids(proxies(target.GetSubFolderList())))
    missing = [i for i in result.folder_ids if i not in present]
    return Verification(ok=not missing, observed={"missing_in_target": missing})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=MoveFoldersParams,
    result=FolderIds,
    verify=_verify_moved_folders,
)
def move_folders(s: ResolveSession, p: MoveFoldersParams) -> FolderIds:
    """Move folders into another Media Pool folder."""
    folders = _folders(s, p.folders)
    if not _pool(s).MoveFolders(folders, resolve_folder(s, p.target)):
        raise _failed("MediaPool.MoveFolders")
    return FolderIds(folder_ids=_ids(folders))


# -- write: clip attributes -----------------------------------------------------------


def _verify_props(
    s: ResolveSession, p: SetPropertiesParams, result: SetPropertiesResult
) -> Verification:
    clip = resolve_clip(s, ClipRef(id=result.clip_id))
    observed = {k: _prop(clip, k) for k in result.applied}
    bad = {k: v for k, v in observed.items() if str(v) != result.applied[k]}
    note = "Resolve may accept a value without persisting it (e.g. 'Reel Name')." if bad else None
    return Verification(ok=not bad and not result.rejected, observed=observed, note=note)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetPropertiesParams,
    result=SetPropertiesResult,
    verify=_verify_props,
)
def set_clip_properties(s: ResolveSession, p: SetPropertiesParams) -> SetPropertiesResult:
    """Set clip properties (key -> value), reading each one back."""
    clip = resolve_clip(s, p.clip)
    applied: dict[str, str] = {}
    rejected: list[str] = []
    for key, value in p.properties.items():
        if clip.SetClipProperty(key, value):
            applied[key] = value
        else:
            rejected.append(key)
    if not applied:
        raise _failed("MediaPoolItem.SetClipProperty", f"Rejected keys: {rejected}")
    return SetPropertiesResult(clip_id=clip.GetUniqueId(), applied=applied, rejected=rejected)


def _read_metadata(clip: ResolveObject, key: str, third_party: bool) -> Any:
    return clip.GetThirdPartyMetadata(key) if third_party else clip.GetMetadata(key)


def _verify_metadata(s: ResolveSession, p: SetMetadataParams, result: ClipIds) -> Verification:
    clip = resolve_clip(s, p.clip)
    observed = {k: _read_metadata(clip, k, p.third_party) for k in p.metadata}
    bad = [k for k, v in observed.items() if str(v) != p.metadata[k]]
    return Verification(
        ok=not bad, observed=observed, note=f"Not persisted: {bad}" if bad else None
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetMetadataParams,
    result=ClipIds,
    verify=_verify_metadata,
)
def set_clip_metadata(s: ResolveSession, p: SetMetadataParams) -> ClipIds:
    """Set clip metadata (or third-party metadata) key/value pairs."""
    clip = resolve_clip(s, p.clip)
    ok = (
        clip.SetThirdPartyMetadata(dict(p.metadata))
        if p.third_party
        else clip.SetMetadata(dict(p.metadata))
    )
    if not ok:
        raise _failed("MediaPoolItem.SetMetadata")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


def _verify_name(s: ResolveSession, p: SetNameParams, result: ClipIds) -> Verification:
    name = resolve_clip(s, ClipRef(id=result.clip_ids[0])).GetName()
    return Verification(ok=name == p.name, observed={"name": name})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetNameParams,
    result=ClipIds,
    verify=_verify_name,
    idempotent=True,
)
def set_clip_name(s: ResolveSession, p: SetNameParams) -> ClipIds:
    """Rename a clip (MediaPoolItem.SetName)."""
    clip = resolve_clip(s, p.clip)
    if not clip.SetName(p.name):
        raise _failed("MediaPoolItem.SetName")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


def _verify_color(s: ResolveSession, p: SetColorParams, result: ClipColorOut) -> Verification:
    now = resolve_clip(s, ClipRef(id=result.clip_id)).GetClipColor() or ""
    note = (
        "Generators and titles accept a color but do not persist it."
        if now != result.clip_color
        else None
    )
    return Verification(ok=now == result.clip_color, observed={"clip_color": now}, note=note)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetColorParams,
    result=ClipColorOut,
    verify=_verify_color,
    idempotent=True,
)
def set_clip_color(s: ResolveSession, p: SetColorParams) -> ClipColorOut:
    """Set a clip color (16 clip colors) or clear it when color is null."""
    clip = resolve_clip(s, p.clip)
    ok = clip.ClearClipColor() if p.color is None else clip.SetClipColor(p.color)
    if not ok:
        raise _failed("MediaPoolItem.SetClipColor", "Only the 16 clip colors are accepted.")
    return ClipColorOut(clip_id=clip.GetUniqueId(), clip_color=p.color or "")


def _verify_flag_added(s: ResolveSession, p: FlagParams, result: FlagsOut) -> Verification:
    flags = [str(f) for f in proxies(resolve_clip(s, ClipRef(id=result.clip_id)).GetFlagList())]
    return Verification(ok=p.color in flags, observed={"flags": flags})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=FlagParams,
    result=FlagsOut,
    verify=_verify_flag_added,
    idempotent=True,
)
def add_flag(s: ResolveSession, p: FlagParams) -> FlagsOut:
    """Add a flag of the given color to a clip."""
    clip = resolve_clip(s, p.clip)
    if not clip.AddFlag(p.color):
        raise _failed("MediaPoolItem.AddFlag")
    return FlagsOut(clip_id=clip.GetUniqueId(), flags=[str(f) for f in proxies(clip.GetFlagList())])


def _verify_flags_cleared(s: ResolveSession, p: ClearFlagsParams, result: FlagsOut) -> Verification:
    flags = [str(f) for f in proxies(resolve_clip(s, ClipRef(id=result.clip_id)).GetFlagList())]
    ok = not flags if p.color == "All" else p.color not in flags
    return Verification(ok=ok, observed={"flags": flags})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ClearFlagsParams,
    result=FlagsOut,
    verify=_verify_flags_cleared,
    idempotent=True,
)
def clear_flags(s: ResolveSession, p: ClearFlagsParams) -> FlagsOut:
    """Clear one flag color, or all flags, of a clip."""
    clip = resolve_clip(s, p.clip)
    if not clip.ClearFlags(p.color):
        raise _failed("MediaPoolItem.ClearFlags")
    return FlagsOut(clip_id=clip.GetUniqueId(), flags=[str(f) for f in proxies(clip.GetFlagList())])


# -- write: clip markers --------------------------------------------------------------


def _verify_marker(s: ResolveSession, p: AddMarkerParams, result: Marker) -> Verification:
    found = _markers(resolve_clip(s, p.clip)).get(p.frame)
    ok = found is not None and found.color == p.color and found.name == p.name
    return Verification(ok=ok, observed=found.model_dump() if found else {})


@action(
    domain=DOMAIN, kind=Kind.WRITE, params=AddMarkerParams, result=Marker, verify=_verify_marker
)
def add_clip_marker(s: ResolveSession, p: AddMarkerParams) -> Marker:
    """Add a marker to a Media Pool clip at a frame offset."""
    clip = resolve_clip(s, p.clip)
    if p.frame in _markers(clip):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"A marker already exists at frame {p.frame}.", retryable=False
        )
    if not clip.AddMarker(p.frame, p.color, p.name, p.note, p.duration, p.custom_data):
        raise _failed("MediaPoolItem.AddMarker", "The frame may be outside the clip.")
    return Marker(
        frame=p.frame,
        color=p.color,
        name=p.name,
        note=p.note,
        duration=p.duration,
        custom_data=p.custom_data,
    )


def _verify_marker_data(
    s: ResolveSession, p: MarkerCustomDataParams, result: Marker
) -> Verification:
    value = resolve_clip(s, p.clip).GetMarkerCustomData(p.frame)
    return Verification(ok=value == p.custom_data, observed={"custom_data": value})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=MarkerCustomDataParams,
    result=Marker,
    verify=_verify_marker_data,
    idempotent=True,
)
def update_marker_custom_data(s: ResolveSession, p: MarkerCustomDataParams) -> Marker:
    """Update the custom data of a clip marker."""
    clip = resolve_clip(s, p.clip)
    marker = _markers(clip).get(p.frame)
    if marker is None:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No marker at frame {p.frame}.", retryable=False)
    if not clip.UpdateMarkerCustomData(p.frame, p.custom_data):
        raise _failed("MediaPoolItem.UpdateMarkerCustomData")
    return marker.model_copy(update={"custom_data": p.custom_data})


def _verify_inout(s: ResolveSession, p: MarkInOutParams, result: MarkInOutOut) -> Verification:
    now = _dict(resolve_clip(s, p.clip).GetMarkInOut())
    return Verification(ok=bool(now), observed=now)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=MarkInOutParams,
    result=MarkInOutOut,
    verify=_verify_inout,
    idempotent=True,
)
def set_mark_in_out(s: ResolveSession, p: MarkInOutParams) -> MarkInOutOut:
    """Set the mark in/out of a clip."""
    if p.mark_out < p.mark_in:
        raise DavinciError(ErrorCode.INVALID_PARAMS, "mark_out < mark_in.", retryable=False)
    clip = resolve_clip(s, p.clip)
    if not clip.SetMarkInOut(p.mark_in, p.mark_out, p.mark_type):
        raise _failed("MediaPoolItem.SetMarkInOut")
    return MarkInOutOut(clip_id=clip.GetUniqueId(), mark_in_out=_dict(clip.GetMarkInOut()))


def _verify_inout_cleared(
    s: ResolveSession, p: ClearMarkInOutParams, result: MarkInOutOut
) -> Verification:
    now = _dict(resolve_clip(s, p.clip).GetMarkInOut())
    return Verification(ok=not now or p.mark_type != "all", observed=now)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ClearMarkInOutParams,
    result=MarkInOutOut,
    verify=_verify_inout_cleared,
    idempotent=True,
)
def clear_mark_in_out(s: ResolveSession, p: ClearMarkInOutParams) -> MarkInOutOut:
    """Clear the mark in/out of a clip."""
    clip = resolve_clip(s, p.clip)
    if not clip.ClearMarkInOut(p.mark_type):
        raise _failed("MediaPoolItem.ClearMarkInOut")
    return MarkInOutOut(clip_id=clip.GetUniqueId(), mark_in_out=_dict(clip.GetMarkInOut()))


def _verify_selected(s: ResolveSession, p: ClipOnly, result: ClipIds) -> Verification:
    selected = _ids(proxies(_pool(s).GetSelectedClips()))
    return Verification(ok=result.clip_ids[0] in selected, observed={"selected": selected})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ClipOnly,
    result=ClipIds,
    verify=_verify_selected,
    idempotent=True,
)
def select_clip(s: ResolveSession, p: ClipOnly) -> ClipIds:
    """Select a clip in the Media Pool."""
    clip = resolve_clip(s, p.clip)
    if not _pool(s).SetSelectedClip(clip):
        raise _failed("MediaPool.SetSelectedClip")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


# -- write: proxy / replace / relink --------------------------------------------------


def _verify_proxy_linked(s: ResolveSession, p: PathClipParams, result: PathOut) -> Verification:
    value = _prop(resolve_clip(s, ClipRef(id=result.clip_id)), "Proxy Media Path")
    return Verification(ok=bool(value), observed={"Proxy Media Path": value})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PathClipParams,
    result=PathOut,
    verify=_verify_proxy_linked,
)
def link_proxy_media(s: ResolveSession, p: PathClipParams) -> PathOut:
    """Link a proxy media file to a clip."""
    path = _path(p.path, must_exist=True)
    clip = resolve_clip(s, p.clip)
    if not clip.LinkProxyMedia(path):
        raise _failed("MediaPoolItem.LinkProxyMedia", "Resolution/frame rate may not match.")
    return PathOut(clip_id=clip.GetUniqueId(), path=path)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PathClipParams,
    result=PathOut,
    verify_reason="Resolve exposes no stable read-back of the full-resolution link.",
)
def link_full_resolution_media(s: ResolveSession, p: PathClipParams) -> PathOut:
    """Link full-resolution media to a proxy-only clip."""
    path = _path(p.path, must_exist=True)
    clip = resolve_clip(s, p.clip)
    if not clip.LinkFullResolutionMedia(path):
        raise _failed(
            "MediaPoolItem.LinkFullResolutionMedia", "Returns False without a proxy-only clip."
        )
    return PathOut(clip_id=clip.GetUniqueId(), path=path)


def _verify_replaced(s: ResolveSession, p: ReplaceClipParams, result: PathOut) -> Verification:
    now = _prop(resolve_clip(s, ClipRef(id=result.clip_id)), "File Path")
    return Verification(ok=_same_path(now, result.path), observed={"File Path": now})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ReplaceClipParams,
    result=PathOut,
    verify=_verify_replaced,
)
def replace_clip(s: ResolveSession, p: ReplaceClipParams) -> PathOut:
    """Replace the underlying media of a clip with another file."""
    path = _path(p.path, must_exist=True)
    clip = resolve_clip(s, p.clip)
    ok = clip.ReplaceClipPreserveSubClip(path) if p.preserve_sub_clip else clip.ReplaceClip(path)
    if not ok:
        raise _failed("MediaPoolItem.ReplaceClip")
    return PathOut(clip_id=clip.GetUniqueId(), path=path)


def _verify_relink(s: ResolveSession, p: RelinkParams, result: ClipIds) -> Verification:
    folder = str(Path(_path(p.folder_path)).resolve())
    observed: dict[str, Any] = {}
    for cid in result.clip_ids:
        now = _prop(resolve_clip(s, ClipRef(id=cid)), "File Path")
        observed[cid] = now
    bad = [
        c
        for c, v in observed.items()
        if not (isinstance(v, str) and str(Path(v).resolve()).startswith(folder))
    ]
    return Verification(ok=not bad, observed=observed, note=f"Not relinked: {bad}" if bad else None)


@action(domain=DOMAIN, kind=Kind.WRITE, params=RelinkParams, result=ClipIds, verify=_verify_relink)
def relink_clips(s: ResolveSession, p: RelinkParams) -> ClipIds:
    """Relink clips to media found in a folder."""
    folder = _path(p.folder_path, must_exist=True)
    if not Path(folder).is_dir():
        raise DavinciError(
            ErrorCode.INVALID_PARAMS, "folder_path must be a directory.", retryable=False
        )
    clips = _clips(s, p.clips)
    if not _pool(s).RelinkClips(clips, folder):
        raise _failed("MediaPool.RelinkClips")
    return ClipIds(clip_ids=_ids(clips))


# -- write: creation / sync -----------------------------------------------------------


def _verify_created(s: ResolveSession, p: BaseModel, result: ClipIds) -> Verification:
    present = {c.GetUniqueId() for _, _, c in _scan(s)}
    missing = [i for i in result.clip_ids if i not in present]
    return Verification(ok=bool(result.clip_ids) and not missing, observed={"missing": missing})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=MulticamParams,
    result=ClipIds,
    verify=_verify_created,
    since=V21_1,
)
def create_multicam_clip(s: ResolveSession, p: MulticamParams) -> ClipIds:
    """Create multicam clip(s) from clips (21.1+); options use live constants."""
    pool = _pool(s)
    s.caps.require(pool, "CreateMulticamClip", since=V21_1)
    clips = _clips(s, p.clips)
    created = proxies(pool.CreateMulticamClip(clips, _live_dict(s, p.options)))
    if not created:
        raise _failed("MediaPool.CreateMulticamClip")
    return ClipIds(clip_ids=_ids(created))


def _verify_stereo(s: ResolveSession, p: StereoParams, result: ClipIds) -> Verification:
    return _verify_created(s, p, result)


@action(domain=DOMAIN, kind=Kind.WRITE, params=StereoParams, result=ClipIds, verify=_verify_stereo)
def create_stereo_clip(s: ResolveSession, p: StereoParams) -> ClipIds:
    """Create a stereoscopic 3D clip from a left and a right clip."""
    left, right = resolve_clip(s, p.left), resolve_clip(s, p.right)
    clip = _pool(s).CreateStereoClip(left, right)
    if clip is None:
        raise _failed("MediaPool.CreateStereoClip")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


def _verify_synced(s: ResolveSession, p: AutoSyncParams, result: ClipIds) -> Verification:
    observed = {
        cid: _prop(resolve_clip(s, ClipRef(id=cid)), "Synced Audio") for cid in result.clip_ids
    }
    return Verification(
        ok=any(bool(v) for v in observed.values()),
        observed=observed,
        note="Checks that at least one clip reports a 'Synced Audio' file.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AutoSyncParams,
    result=ClipIds,
    verify=_verify_synced,
)
def auto_sync_audio(s: ResolveSession, p: AutoSyncParams) -> ClipIds:
    """Auto-sync audio of clips (needs at least one video and one audio clip)."""
    clips = _clips(s, p.clips)
    if len(clips) < _MIN_SYNC_CLIPS:
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            "Select at least one video and one audio clip.",
            retryable=False,
        )
    if not _pool(s).AutoSyncAudio(clips, _live_dict(s, p.settings)):
        raise _failed("MediaPool.AutoSyncAudio")
    return ClipIds(clip_ids=_ids(clips))


def _verify_mattes(s: ResolveSession, p: MattesParams, result: ClipIds) -> Verification:
    mattes = [str(m) for m in proxies(_pool(s).GetClipMatteList(resolve_clip(s, p.clip)))]
    missing = [x for x in p.paths if not any(_same_path(m, _path(x)) for m in mattes)]
    return Verification(ok=not missing, observed={"mattes": mattes})


@action(domain=DOMAIN, kind=Kind.WRITE, params=MattesParams, result=ClipIds, verify=_verify_mattes)
def add_clip_mattes(s: ResolveSession, p: MattesParams) -> ClipIds:
    """Add matte files to a clip."""
    paths = [_path(x, must_exist=True) for x in p.paths]
    clip = resolve_clip(s, p.clip)
    storage = _storage(s)
    ok = (
        storage.AddClipMattesToMediaPool(clip, paths, p.stereo_eye)
        if p.stereo_eye
        else storage.AddClipMattesToMediaPool(clip, paths)
    )
    if not ok:
        raise _failed("MediaStorage.AddClipMattesToMediaPool")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TimelineMattesParams,
    result=ClipIds,
    verify=_verify_created,
)
def add_timeline_mattes(s: ResolveSession, p: TimelineMattesParams) -> ClipIds:
    """Add matte files as timeline mattes in the current folder."""
    paths = [_path(x, must_exist=True) for x in p.paths]
    created = proxies(_storage(s).AddTimelineMattesToMediaPool(paths))
    if not created:
        raise _failed("MediaStorage.AddTimelineMattesToMediaPool")
    return ClipIds(clip_ids=_ids(created))


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=DrbImportParams,
    result=Done,
    verify_reason="The API returns only a bool; the created folder is not identified.",
)
def import_folder_drb(s: ResolveSession, p: DrbImportParams) -> Done:
    """Import a DRB folder export into the Media Pool."""
    path = _path(p.path, must_exist=True)
    source = _path(p.source_clips_path, must_exist=True) if p.source_clips_path else None
    pool = _pool(s)
    ok = pool.ImportFolderFromFile(path, source) if source else pool.ImportFolderFromFile(path)
    if not ok:
        raise _failed("MediaPool.ImportFolderFromFile")
    return Done()


def _verify_file(s: ResolveSession, p: BaseModel, result: WrittenFile) -> Verification:
    target = Path(result.path)
    return Verification(
        ok=target.is_file() and target.stat().st_size > 0,
        observed={"exists": target.exists()},
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=DrbExportParams,
    result=WrittenFile,
    verify=_verify_file,
)
def export_folder_drb(s: ResolveSession, p: DrbExportParams) -> WrittenFile:
    """Export a Media Pool folder as a DRB file."""
    path = _path(p.path)
    folder = resolve_folder(s, p.folder)
    if not folder.Export(path):
        raise _failed("Folder.Export")
    size = Path(path).stat().st_size if Path(path).exists() else 0
    return WrittenFile(path=path, size=size)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportMetadataParams,
    result=WrittenFile,
    verify=_verify_file,
)
def export_metadata(s: ResolveSession, p: ExportMetadataParams) -> WrittenFile:
    """Export clip metadata to a CSV file."""
    path = _path(p.path)
    if not path.lower().endswith(".csv"):
        raise DavinciError(ErrorCode.INVALID_PARAMS, "path must end with .csv.", retryable=False)
    pool = _pool(s)
    ok = pool.ExportMetadata(path, _clips(s, p.clips)) if p.clips else pool.ExportMetadata(path)
    if not ok:
        raise _failed("MediaPool.ExportMetadata")
    size = Path(path).stat().st_size if Path(path).exists() else 0
    return WrittenFile(path=path, size=size)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    result=Done,
    idempotent=True,
    verify_reason="Collaboration refresh has no observable state.",
)
def refresh_folders(s: ResolveSession, _: BaseModel) -> Done:
    """Refresh Media Pool folders (collaboration mode)."""
    if not _pool(s).RefreshFolders():
        raise _failed("MediaPool.RefreshFolders", "Only meaningful in collaboration mode.")
    return Done()


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RevealParams,
    result=Done,
    idempotent=True,
    verify_reason="Pure UI effect (expands the path in Media Storage).",
)
def reveal_in_storage(s: ResolveSession, p: RevealParams) -> Done:
    """Reveal a path in Resolve's Media Storage panel."""
    if not _storage(s).RevealInStorage(_path(p.path, must_exist=True)):
        raise _failed("MediaStorage.RevealInStorage")
    return Done()


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ClipOnly,
    result=Done,
    verify_reason="Monitoring runs in the background; no state to read back.",
)
def monitor_growing_file(s: ResolveSession, p: ClipOnly) -> Done:
    """Monitor a clip's file while it keeps growing."""
    if not resolve_clip(s, p.clip).MonitorGrowingFile():
        raise _failed("MediaPoolItem.MonitorGrowingFile")
    return Done()


# -- write: clone (long running) ------------------------------------------------------


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CloneParams,
    since=V21_1,
    long_running=True,
)
def clone_media(s: ResolveSession, p: CloneParams) -> JobRequest:
    """Clone media with checksum verification (21.1+, long-running job)."""
    storage = _storage(s)
    s.caps.require(storage, "StartCloneMedia", since=V21_1)
    source = _path(p.source_dir, must_exist=True)
    targets = [_path(t) for t in p.target_dirs]
    settings: dict[str, Any] = {}
    if p.preserve_folder_name is not None:
        settings["PreserveFolderName"] = p.preserve_folder_name
    if p.checksum_type is not None:
        settings["ChecksumType"] = const(s, p.checksum_type)

    def launch(sess: ResolveSession) -> Any:
        ms = _storage(sess)
        if settings and not ms.SetCloneToolSettings(settings):
            raise _failed("MediaStorage.SetCloneToolSettings")
        return ms.StartCloneMedia(source, targets if len(targets) > 1 else targets[0])

    def poll(sess: ResolveSession) -> Mapping[str, Any]:
        status = _dict(_storage(sess).GetCloneStatus())
        pct = status.get("CompletionPercentage")
        if isinstance(pct, (int, float)) and pct >= _COMPLETE_PCT:
            status["done"] = True
        return status

    def cancel(sess: ResolveSession) -> Any:
        return _storage(sess).StopCloneMedia()

    return JobRequest(
        kind="media.clone",
        label=f"Clone {Path(source).name} -> {len(targets)} target(s)",
        launch=launch,
        poll=poll,
        cancel=cancel,
    )


# -- destructive ----------------------------------------------------------------------


def _clip_state(clip: ResolveObject) -> dict[str, Any]:
    return {
        "id": clip.GetUniqueId(),
        "name": clip.GetName(),
        "usage": _prop(clip, "Usage"),
        "path": _prop(clip, "File Path"),
    }


def _plan_delete_clips(s: ResolveSession, p: DeleteClipsParams) -> PlanDraft:
    states = [_clip_state(c) for c in _clips(s, p.clips)]
    used = [st["name"] for st in states if st["usage"] not in (None, "", "0", 0)]
    effects = ["Removes the clips from the Media Pool (source files stay on disk)."]
    if used:
        effects.append(f"Clips used in timelines: {used}; their timeline items go offline.")
    return PlanDraft(
        summary=f"Delete {len(states)} clip(s): {[st['name'] for st in states][:5]}.",
        effects=effects,
        targets=[f"clip:{st['id']}" for st in states],
        fingerprint=_fp(states),
    )


def _verify_clips_gone(s: ResolveSession, p: DeleteClipsParams, result: ClipIds) -> Verification:
    present = {c.GetUniqueId() for _, _, c in _scan(s)}
    left = [i for i in result.clip_ids if i in present]
    return Verification(ok=not left, observed={"still_present": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteClipsParams,
    result=ClipIds,
    plan=_plan_delete_clips,
    verify=_verify_clips_gone,
)
def delete_clips(s: ResolveSession, p: DeleteClipsParams) -> ClipIds:
    """Delete clips from the Media Pool."""
    clips = _clips(s, p.clips)
    ids = _ids(clips)
    if not _pool(s).DeleteClips(clips):
        raise _failed("MediaPool.DeleteClips")
    return ClipIds(clip_ids=ids)


def _folder_state(s: ResolveSession, folder: ResolveObject) -> dict[str, Any]:
    nested = [f for _, f in walk_folders(folder)]
    return {
        "id": folder.GetUniqueId(),
        "path": _folder_path(s, folder),
        "folders": len(nested),
        "clips": sum(len(proxies(f.GetClipList())) for f in nested),
    }


def _plan_delete_folders(s: ResolveSession, p: DeleteFoldersParams) -> PlanDraft:
    states = [_folder_state(s, f) for f in _folders(s, p.folders)]
    return PlanDraft(
        summary=f"Delete {len(states)} folder(s): {[st['path'] for st in states]}.",
        effects=[
            f"Removes {st['path']} with {st['clips']} clip(s) in {st['folders']} folder(s)."
            for st in states
        ],
        targets=[f"folder:{st['id']}" for st in states],
        fingerprint=_fp(states),
    )


def _verify_folders_gone(
    s: ResolveSession, p: DeleteFoldersParams, result: FolderIds
) -> Verification:
    present = {f.GetUniqueId() for _, f in walk_folders(_pool(s).GetRootFolder())}
    left = [i for i in result.folder_ids if i in present]
    return Verification(ok=not left, observed={"still_present": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteFoldersParams,
    result=FolderIds,
    plan=_plan_delete_folders,
    verify=_verify_folders_gone,
)
def delete_folders(s: ResolveSession, p: DeleteFoldersParams) -> FolderIds:
    """Delete Media Pool folders (with their content)."""
    folders = _folders(s, p.folders)
    ids = _ids(folders)
    if not _pool(s).DeleteFolders(folders):
        raise _failed("MediaPool.DeleteFolders")
    return FolderIds(folder_ids=ids)


def _matching_markers(clip: ResolveObject, p: DeleteMarkersParams) -> list[Marker]:
    markers = _markers(clip)
    if p.frame is not None:
        return [markers[p.frame]] if p.frame in markers else []
    if p.color is not None:
        return [m for m in markers.values() if p.color in ("All", m.color)]
    first = next(
        (
            m
            for m in sorted(markers.values(), key=lambda m: m.frame)
            if m.custom_data == p.custom_data
        ),
        None,
    )
    return [first] if first else []


def _plan_delete_markers(s: ResolveSession, p: DeleteMarkersParams) -> PlanDraft:
    clip = resolve_clip(s, p.clip)
    found = _matching_markers(clip, p)
    if not found:
        raise DavinciError(ErrorCode.NOT_FOUND, "No clip marker matches.", retryable=False)
    return PlanDraft(
        summary=f"Delete {len(found)} marker(s) of clip {clip.GetName()!r}.",
        effects=["Removes the markers; this cannot be undone through the API."],
        targets=[f"clip:{clip.GetUniqueId()}@{m.frame}" for m in found],
        fingerprint=_fp([m.model_dump() for m in found]),
    )


def _verify_markers_gone(
    s: ResolveSession, p: DeleteMarkersParams, result: DeletedMarkers
) -> Verification:
    remaining = _markers(resolve_clip(s, p.clip))
    left = [m.frame for m in result.deleted if remaining.get(m.frame) == m]
    return Verification(ok=not left, observed={"still_present": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteMarkersParams,
    result=DeletedMarkers,
    plan=_plan_delete_markers,
    verify=_verify_markers_gone,
)
def delete_clip_markers(s: ResolveSession, p: DeleteMarkersParams) -> DeletedMarkers:
    """Delete clip markers by frame, color ('All' allowed) or custom data."""
    clip = resolve_clip(s, p.clip)
    found = _matching_markers(clip, p)
    if not found:
        raise DavinciError(ErrorCode.NOT_FOUND, "No clip marker matches.", retryable=False)
    if p.frame is not None:
        ok = clip.DeleteMarkerAtFrame(p.frame)
    elif p.color is not None:
        ok = clip.DeleteMarkersByColor(p.color)
    else:
        ok = clip.DeleteMarkerByCustomData(p.custom_data)
    if not ok:
        raise _failed("MediaPoolItem.DeleteMarker*")
    return DeletedMarkers(clip_id=clip.GetUniqueId(), deleted=found)


def _plan_unlink(s: ResolveSession, p: UnlinkClipsParams) -> PlanDraft:
    states = [_clip_state(c) for c in _clips(s, p.clips)]
    return PlanDraft(
        summary=f"Unlink {len(states)} clip(s) from their media files.",
        effects=["The clips go offline until relinked."],
        targets=[f"clip:{st['id']}" for st in states],
        fingerprint=_fp(states),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=UnlinkClipsParams,
    result=ClipIds,
    plan=_plan_unlink,
    verify_reason="Offline state is localized in 'Online Status'; no reliable read-back.",
)
def unlink_clips(s: ResolveSession, p: UnlinkClipsParams) -> ClipIds:
    """Unlink clips from their media files (they go offline)."""
    clips = _clips(s, p.clips)
    if not _pool(s).UnlinkClips(clips):
        raise _failed("MediaPool.UnlinkClips")
    return ClipIds(clip_ids=_ids(clips))


def _plan_unlink_proxy(s: ResolveSession, p: ClipOnly) -> PlanDraft:
    clip = resolve_clip(s, p.clip)
    proxy = _prop(clip, "Proxy Media Path")
    if not proxy:
        raise DavinciError(ErrorCode.WRONG_STATE, "The clip has no linked proxy.", retryable=False)
    return PlanDraft(
        summary=f"Unlink the proxy of {clip.GetName()!r}.",
        effects=[f"Detaches {proxy}; the file stays on disk."],
        targets=[f"clip:{clip.GetUniqueId()}"],
        fingerprint=_fp({"id": clip.GetUniqueId(), "proxy": proxy}),
    )


def _verify_proxy_unlinked(s: ResolveSession, p: ClipOnly, result: ClipIds) -> Verification:
    value = _prop(resolve_clip(s, ClipRef(id=result.clip_ids[0])), "Proxy Media Path")
    return Verification(ok=not value, observed={"Proxy Media Path": value})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=ClipOnly,
    result=ClipIds,
    plan=_plan_unlink_proxy,
    verify=_verify_proxy_unlinked,
)
def unlink_proxy_media(s: ResolveSession, p: ClipOnly) -> ClipIds:
    """Remove the proxy link of a clip."""
    clip = resolve_clip(s, p.clip)
    if not clip.UnlinkProxyMedia():
        raise _failed("MediaPoolItem.UnlinkProxyMedia")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


def _plan_delete_mattes(s: ResolveSession, p: DeleteMattesParams) -> PlanDraft:
    clip = resolve_clip(s, p.clip)
    mattes = [str(m) for m in proxies(_pool(s).GetClipMatteList(clip))]
    return PlanDraft(
        summary=f"Delete {len(p.paths)} matte(s) of {clip.GetName()!r}.",
        effects=["Removes the matte links from the clip."],
        targets=[f"clip:{clip.GetUniqueId()}"],
        fingerprint=_fp(mattes),
    )


def _verify_mattes_gone(s: ResolveSession, p: DeleteMattesParams, result: ClipIds) -> Verification:
    mattes = [str(m) for m in proxies(_pool(s).GetClipMatteList(resolve_clip(s, p.clip)))]
    left = [x for x in p.paths if any(_same_path(m, x) for m in mattes)]
    return Verification(ok=not left, observed={"mattes": mattes})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteMattesParams,
    result=ClipIds,
    plan=_plan_delete_mattes,
    verify=_verify_mattes_gone,
)
def delete_clip_mattes(s: ResolveSession, p: DeleteMattesParams) -> ClipIds:
    """Delete mattes of a clip by file path."""
    clip = resolve_clip(s, p.clip)
    if not _pool(s).DeleteClipMattes(clip, list(p.paths)):
        raise _failed("MediaPool.DeleteClipMattes")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


def _plan_clear_metadata(s: ResolveSession, p: ClearMetadataParams) -> PlanDraft:
    clip = resolve_clip(s, p.clip)
    before = {k: _read_metadata(clip, k, p.third_party) for k in p.keys}
    return PlanDraft(
        summary=f"Blank {len(p.keys)} metadata key(s) of {clip.GetName()!r}.",
        effects=[f"Overwrites current values: {before}."],
        targets=[f"clip:{clip.GetUniqueId()}"],
        fingerprint=_fp(before),
    )


def _verify_metadata_cleared(
    s: ResolveSession, p: ClearMetadataParams, result: ClipIds
) -> Verification:
    clip = resolve_clip(s, p.clip)
    observed = {k: _read_metadata(clip, k, p.third_party) for k in p.keys}
    return Verification(ok=not any(observed.values()), observed=observed)


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=ClearMetadataParams,
    result=ClipIds,
    plan=_plan_clear_metadata,
    verify=_verify_metadata_cleared,
)
def clear_clip_metadata(s: ResolveSession, p: ClearMetadataParams) -> ClipIds:
    """Blank metadata keys of a clip (values are overwritten with empty strings)."""
    clip = resolve_clip(s, p.clip)
    blank = dict.fromkeys(p.keys, "")
    ok = clip.SetThirdPartyMetadata(blank) if p.third_party else clip.SetMetadata(blank)
    if not ok:
        raise _failed("MediaPoolItem.SetMetadata")
    return ClipIds(clip_ids=[clip.GetUniqueId()])


ACTIONS = [
    list_volumes,
    browse_storage,
    media_pool_tree,
    list_clips,
    get_clip,
    get_clip_markers,
    get_selection,
    get_audio_mapping,
    get_clone_status,
    list_mattes,
    import_media,
    create_folder,
    set_current_folder,
    move_clips,
    move_folders,
    set_clip_properties,
    set_clip_metadata,
    set_clip_name,
    set_clip_color,
    add_flag,
    clear_flags,
    add_clip_marker,
    update_marker_custom_data,
    set_mark_in_out,
    clear_mark_in_out,
    select_clip,
    link_proxy_media,
    link_full_resolution_media,
    replace_clip,
    relink_clips,
    create_multicam_clip,
    create_stereo_clip,
    auto_sync_audio,
    add_clip_mattes,
    add_timeline_mattes,
    import_folder_drb,
    export_folder_drb,
    export_metadata,
    refresh_folders,
    reveal_in_storage,
    monitor_growing_file,
    clone_media,
    delete_clips,
    delete_folders,
    delete_clip_markers,
    unlink_clips,
    unlink_proxy_media,
    delete_clip_mattes,
    clear_clip_metadata,
]
