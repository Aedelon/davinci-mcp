# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Gallery domain: still albums, PowerGrade albums and stills.

Traps encoded: ``GrabStill`` / ``GrabAllStills`` run on the Color page,
``ExportStills`` returns a bare False when the Gallery panel is closed (the API can
neither read nor open it), and the Resolve API has no album deletion call.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import proxies
from davinci_mcp.safety.paths import SafeFileName, check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "gallery"

AlbumKind = Literal["still", "powergrade"]
StillFormat = Literal["dpx", "cin", "tif", "jpg", "png", "ppm", "bmp", "xpm"]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlbumRef(_Params):
    """Select an album by name or 1-based index; omit both for the current still album."""

    kind: AlbumKind = "still"
    name: str | None = None
    index: int | None = Field(default=None, ge=1, description="1-based index within its kind.")

    @model_validator(mode="after")
    def _one(self) -> AlbumRef:
        if self.name is not None and self.index is not None:
            raise ValueError("use only one of name or index")
        if self.name is None and self.index is None and self.kind != "still":
            raise ValueError("a PowerGrade album needs a name or an index")
        return self


def _gallery(s: ResolveSession) -> ResolveObject:
    gallery = s.project.GetGallery()
    if gallery is None:
        raise DavinciError(ErrorCode.WRONG_STATE, "The project has no Gallery object.")
    return gallery


def _albums(gallery: ResolveObject, kind: AlbumKind) -> list[ResolveObject]:
    raw = (
        gallery.GetGalleryStillAlbums() if kind == "still" else gallery.GetGalleryPowerGradeAlbums()
    )
    return proxies(raw)


def _album_name(gallery: ResolveObject, album: ResolveObject) -> str:
    return str(gallery.GetAlbumName(album) or "")


def _resolve_album(s: ResolveSession, ref: AlbumRef | None) -> ResolveObject:
    ref = ref or AlbumRef()
    gallery = _gallery(s)
    if ref.name is None and ref.index is None:
        current = gallery.GetCurrentStillAlbum()
        if current is None:
            raise DavinciError(ErrorCode.WRONG_STATE, "There is no current still album.")
        return current
    albums = _albums(gallery, ref.kind)
    if ref.index is not None:
        if ref.index > len(albums):
            raise DavinciError(
                ErrorCode.NOT_FOUND, f"Only {len(albums)} {ref.kind} album(s) exist."
            )
        return albums[ref.index - 1]
    matches = [a for a in albums if _album_name(gallery, a) == ref.name]
    if not matches:
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"No {ref.kind} album named {ref.name!r}.",
            details={"albums": [_album_name(gallery, a) for a in albums]},
            next_call="gallery_read(op={'action': 'list_albums'})",
        )
    if len(matches) > 1:
        raise DavinciError(
            ErrorCode.AMBIGUOUS_REFERENCE,
            f"{len(matches)} {ref.kind} albums are named {ref.name!r}; select by index.",
        )
    return matches[0]


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _fail(what: str, hint: str | None = None) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, f"{what} returned False.", hint=hint, retryable=False
    )


# -- albums ------------------------------------------------------------------------


class AlbumInfo(BaseModel):
    kind: AlbumKind
    index: int
    name: str
    still_count: int
    current: bool = False


class AlbumList(BaseModel):
    albums: list[AlbumInfo]


@action(domain=DOMAIN, kind=Kind.READ, result=AlbumList)
def list_albums(s: ResolveSession, _: BaseModel) -> AlbumList:
    """List still and PowerGrade albums with their still counts."""
    gallery = _gallery(s)
    current = gallery.GetCurrentStillAlbum()
    current_name = _album_name(gallery, current) if current is not None else None
    out: list[AlbumInfo] = []
    for kind in ("still", "powergrade"):
        for i, album in enumerate(_albums(gallery, kind), start=1):
            name = _album_name(gallery, album)
            out.append(
                AlbumInfo(
                    kind=kind,
                    index=i,
                    name=name,
                    still_count=len(proxies(album.GetStills())),
                    current=kind == "still" and name == current_name,
                )
            )
    return AlbumList(albums=out)


class CreateAlbumParams(_Params):
    kind: AlbumKind = "still"


class AlbumCreated(BaseModel):
    kind: AlbumKind
    name: str
    count_before: int


def _verify_created(s: ResolveSession, p: CreateAlbumParams, result: AlbumCreated) -> Verification:
    count = len(_albums(_gallery(s), p.kind))
    return Verification(ok=count == result.count_before + 1, observed={"album_count": count})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=CreateAlbumParams,
    result=AlbumCreated,
    verify=_verify_created,
)
def create_album(s: ResolveSession, p: CreateAlbumParams) -> AlbumCreated:
    """Create a still album or a PowerGrade album."""
    gallery = _gallery(s)
    before = len(_albums(gallery, p.kind))
    album = (
        gallery.CreateGalleryStillAlbum()
        if p.kind == "still"
        else gallery.CreateGalleryPowerGradeAlbum()
    )
    if album is None:
        raise _fail("Gallery.Create*Album")
    return AlbumCreated(kind=p.kind, name=_album_name(gallery, album), count_before=before)


class SetCurrentAlbumParams(_Params):
    album: AlbumRef


class AlbumResult(BaseModel):
    name: str


def _verify_current(
    s: ResolveSession, p: SetCurrentAlbumParams, result: AlbumResult
) -> Verification:
    gallery = _gallery(s)
    current = _album_name(gallery, gallery.GetCurrentStillAlbum())
    return Verification(ok=current == result.name, observed={"current": current})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetCurrentAlbumParams,
    result=AlbumResult,
    verify=_verify_current,
)
def set_current_album(s: ResolveSession, p: SetCurrentAlbumParams) -> AlbumResult:
    """Make a still album the current one."""
    gallery = _gallery(s)
    album = _resolve_album(s, p.album)
    if not gallery.SetCurrentStillAlbum(album):
        raise _fail("Gallery.SetCurrentStillAlbum")
    return AlbumResult(name=_album_name(gallery, album))


class RenameAlbumParams(_Params):
    album: AlbumRef
    new_name: str = Field(min_length=1)


def _verify_album_renamed(s: ResolveSession, p: RenameAlbumParams, _: AlbumResult) -> Verification:
    names = [_album_name(_gallery(s), a) for a in _albums(_gallery(s), p.album.kind)]
    return Verification(ok=p.new_name in names, observed={"albums": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RenameAlbumParams,
    result=AlbumResult,
    verify=_verify_album_renamed,
)
def rename_album(s: ResolveSession, p: RenameAlbumParams) -> AlbumResult:
    """Rename a still or PowerGrade album."""
    gallery = _gallery(s)
    album = _resolve_album(s, p.album)
    if not gallery.SetAlbumName(album, p.new_name):
        raise _fail("Gallery.SetAlbumName")
    return AlbumResult(name=p.new_name)


# -- stills -----------------------------------------------------------------------


class StillInfo(BaseModel):
    index: int
    label: str


class StillList(BaseModel):
    album: str
    stills: list[StillInfo]


class ListStillsParams(_Params):
    album: AlbumRef | None = None


@action(domain=DOMAIN, kind=Kind.READ, params=ListStillsParams, result=StillList)
def list_stills(s: ResolveSession, p: ListStillsParams) -> StillList:
    """List the stills of an album (default: current) with their labels."""
    album = _resolve_album(s, p.album)
    return StillList(
        album=_album_name(_gallery(s), album),
        stills=[
            StillInfo(index=i, label=str(album.GetLabel(st) or ""))
            for i, st in enumerate(proxies(album.GetStills()), start=1)
        ],
    )


def _select(album: ResolveObject, sel: list[int | str]) -> list[ResolveObject]:
    """Stills for 1-based indices (int) or labels (str); empty selection means all."""
    stills = proxies(album.GetStills())
    if not sel:
        return stills
    out: list[ResolveObject] = []
    for key in sel:
        if isinstance(key, int):
            if not 1 <= key <= len(stills):
                raise DavinciError(
                    ErrorCode.NOT_FOUND, f"Album has {len(stills)} stills; no still #{key}."
                )
            out.append(stills[key - 1])
        else:
            hits = [st for st in stills if album.GetLabel(st) == key]
            if len(hits) != 1:
                raise DavinciError(
                    ErrorCode.NOT_FOUND if not hits else ErrorCode.AMBIGUOUS_REFERENCE,
                    f"{len(hits)} still(s) labelled {key!r}; select by index.",
                )
            out.append(hits[0])
    return out


class GrabStillParams(_Params):
    scope: Literal["current", "all"] = Field(
        default="current", description="Current clip, or every clip of the timeline."
    )
    frame_source: Literal["first", "middle"] = Field(
        default="first", description="Frame used when scope is 'all'."
    )


class GrabResult(BaseModel):
    grabbed: int
    count_before: int


def _verify_grab(s: ResolveSession, p: GrabStillParams, result: GrabResult) -> Verification:
    now = len(proxies(_resolve_album(s, None).GetStills()))
    return Verification(
        ok=now >= result.count_before + result.grabbed, observed={"still_count": now}
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=GrabStillParams,
    result=GrabResult,
    page="color",
    verify=_verify_grab,
)
def grab_still(s: ResolveSession, p: GrabStillParams) -> GrabResult:
    """Grab a still from the current clip, or from all clips, into the current album."""
    before = len(proxies(_resolve_album(s, None).GetStills()))
    timeline = s.timeline
    if p.scope == "current":
        ok = timeline.GrabStill() is not None
        count = 1 if ok else 0
    else:
        grabbed = timeline.GrabAllStills(1 if p.frame_source == "first" else 2)
        ok = bool(grabbed)
        count = len(proxies(grabbed))
    if not ok:
        raise _fail(
            "Timeline.GrabStill", "Needs the Color page and a video clip under the playhead."
        )
    return GrabResult(grabbed=count, count_before=before)


class ImportStillsParams(_Params):
    album: AlbumRef | None = None
    paths: list[str] = Field(min_length=1, description="Still files (.drx + image) to import.")


class ImportResult(BaseModel):
    album: str
    count_before: int
    requested: int


def _verify_import(s: ResolveSession, p: ImportStillsParams, result: ImportResult) -> Verification:
    now = len(proxies(_resolve_album(s, p.album).GetStills()))
    return Verification(ok=now > result.count_before, observed={"still_count": now})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportStillsParams,
    result=ImportResult,
    verify=_verify_import,
)
def import_stills(s: ResolveSession, p: ImportStillsParams) -> ImportResult:
    """Import still files into an album (default: current)."""
    album = _resolve_album(s, p.album)
    paths = [str(check_path(x, runtime().allowed_roots, must_exist=True)) for x in p.paths]
    before = len(proxies(album.GetStills()))
    if not album.ImportStills(paths):
        raise _fail("GalleryStillAlbum.ImportStills")
    return ImportResult(
        album=_album_name(_gallery(s), album), count_before=before, requested=len(paths)
    )


class ExportStillsParams(_Params):
    album: AlbumRef | None = None
    stills: list[int | str] = Field(
        default_factory=lambda: [],
        description="1-based indices or labels of the stills to export (empty = all).",
    )
    folder: str
    prefix: SafeFileName = "still"
    format: StillFormat = "png"


class ExportResult(BaseModel):
    folder: str
    files: list[str]


def _exported(p: ExportStillsParams) -> list[str]:
    folder = Path(p.folder).expanduser()
    return sorted(f.name for f in folder.glob(f"{p.prefix}*")) if folder.is_dir() else []


def _verify_export(s: ResolveSession, p: ExportStillsParams, _: ExportResult) -> Verification:
    files = _exported(p)
    return Verification(ok=bool(files), observed={"files": files})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportStillsParams,
    result=ExportResult,
    verify=_verify_export,
)
def export_stills(s: ResolveSession, p: ExportStillsParams) -> ExportResult:
    """Export stills to a folder (needs the Gallery panel open in Resolve)."""
    album = _resolve_album(s, p.album)
    selected = _select(album, p.stills)
    if not selected:
        raise DavinciError(ErrorCode.NOT_FOUND, "The album has no stills to export.")
    folder = check_path(p.folder, runtime().allowed_roots)
    folder.mkdir(parents=True, exist_ok=True)
    if not album.ExportStills(selected, str(folder), p.prefix, p.format):
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "GalleryStillAlbum.ExportStills returned False.",
            hint="This usually means the Gallery panel is closed. The API can neither read nor "
            "open it: open the Gallery panel on the Color page, then retry. For pixels of the "
            "current frame, use Project.ExportCurrentFrameAsStill instead.",
            retryable=False,
        )
    return ExportResult(folder=str(folder), files=_exported(p))


class SetLabelParams(_Params):
    album: AlbumRef | None = None
    still: int | str = Field(description="1-based index, or current label, of the still.")
    label: str


class LabelResult(BaseModel):
    label: str


def _verify_label(s: ResolveSession, p: SetLabelParams, _: LabelResult) -> Verification:
    album = _resolve_album(s, p.album)
    label = str(album.GetLabel(_select(album, [p.still])[0]) or "")
    return Verification(ok=label == p.label, observed={"label": label})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetLabelParams,
    result=LabelResult,
    verify=_verify_label,
)
def set_still_label(s: ResolveSession, p: SetLabelParams) -> LabelResult:
    """Set the label of a still."""
    album = _resolve_album(s, p.album)
    if not album.SetLabel(_select(album, [p.still])[0], p.label):
        raise _fail("GalleryStillAlbum.SetLabel")
    return LabelResult(label=p.label)


class DeleteStillsParams(_Params):
    album: AlbumRef | None = None
    stills: list[int | str] = Field(
        min_length=1, description="1-based indices or labels of the stills to delete."
    )


class DeleteResult(BaseModel):
    deleted: int
    count_before: int


def _plan_delete(s: ResolveSession, p: DeleteStillsParams) -> PlanDraft:
    album = _resolve_album(s, p.album)
    labels = [str(album.GetLabel(st) or "") for st in _select(album, p.stills)]
    return PlanDraft(
        summary=f"Delete {len(labels)} still(s) from album {_album_name(_gallery(s), album)!r}.",
        effects=["The stills and their saved grades are removed from the Gallery."],
        targets=[f"still:{label}" for label in labels],
        fingerprint=_fingerprint({"labels": labels, "total": len(proxies(album.GetStills()))}),
    )


def _verify_deleted(s: ResolveSession, p: DeleteStillsParams, result: DeleteResult) -> Verification:
    now = len(proxies(_resolve_album(s, p.album).GetStills()))
    return Verification(
        ok=now == result.count_before - result.deleted, observed={"still_count": now}
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteStillsParams,
    result=DeleteResult,
    plan=_plan_delete,
    verify=_verify_deleted,
)
def delete_stills(s: ResolveSession, p: DeleteStillsParams) -> DeleteResult:
    """Delete stills from an album."""
    album = _resolve_album(s, p.album)
    selected = _select(album, p.stills)
    before = len(proxies(album.GetStills()))
    if not album.DeleteStills(selected):
        raise _fail("GalleryStillAlbum.DeleteStills")
    return DeleteResult(deleted=len(selected), count_before=before)


ACTIONS = [
    list_albums,
    list_stills,
    create_album,
    set_current_album,
    rename_album,
    grab_still,
    import_stills,
    export_stills,
    set_still_label,
    delete_stills,
]
