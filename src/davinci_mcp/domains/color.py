# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Color domain: node graphs, LUTs, versions, CDL, DRX grades, color groups, DCTL.

Resolve exposes no node editing and no primary-value reads: only LUT, cache mode,
enabled state, CDL, DRX and grade copy. Traps encoded here: ``AddVersion`` /
``ApplyGradeFromDRX`` / ``ExportLUT`` need the Color page, ``CopyGrades`` replaces
target grades without creating a version, ``SetLUT`` only resolves LUTs under the
master LUT root, ``SetCDL`` and ``SetNodeEnabled`` are write-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import (
    ItemRef,
    TimelineRef,
    const,
    proxies,
    resolve_item,
    resolve_timeline,
)
from davinci_mcp.safety.paths import SafeFileName, check_path

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "color"

VersionType = Literal["local", "remote"]
CacheModeName = Literal["auto", "enabled", "disabled"]
LutExportType = Literal["17pt_cube", "33pt_cube", "65pt_cube", "panasonic_vlut"]

_CACHE_CONSTS: dict[str, str] = {
    "auto": "CACHE_AUTO_ENABLED",
    "enabled": "CACHE_ENABLED",
    "disabled": "CACHE_DISABLED",
}
_LUT_CONSTS: dict[str, str] = {
    "17pt_cube": "EXPORT_LUT_17PTCUBE",
    "33pt_cube": "EXPORT_LUT_33PTCUBE",
    "65pt_cube": "EXPORT_LUT_65PTCUBE",
    "panasonic_vlut": "EXPORT_LUT_PANASONICVLUT",
}


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- targets ------------------------------------------------------------------


class GraphTarget(_Params):
    """Which node graph: a clip (item), a color group (group + stage) or the timeline."""

    item: ItemRef | None = Field(default=None, description="Clip graph of this timeline item.")
    group: str | None = Field(default=None, description="Color group name (pre/post clip graph).")
    stage: Literal["pre", "post"] = Field(default="post", description="Group graph to use.")
    layer: int | None = Field(default=None, ge=1, description="Node stack layer (clip graph only).")
    timeline: TimelineRef | None = Field(
        default=None, description="Timeline graph when neither item nor group is given."
    )

    @model_validator(mode="after")
    def _exclusive(self) -> GraphTarget:
        if self.item is not None and self.group is not None:
            raise ValueError("use only one of item or group")
        return self


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _find_group(s: ResolveSession, name: str) -> ResolveObject:
    groups = proxies(s.project.GetColorGroupsList())
    matches = [g for g in groups if g.GetName() == name]
    if not matches:
        raise DavinciError(
            ErrorCode.NOT_FOUND,
            f"No color group named {name!r}.",
            details={"groups": [g.GetName() for g in groups]},
            next_call="color_read(op={'action': 'list_color_groups'})",
            retryable=False,
        )
    return matches[0]


def _graph(s: ResolveSession, target: GraphTarget) -> ResolveObject:
    graph: ResolveObject
    if target.item is not None:
        item = resolve_item(s, target.item)
        graph = item.GetNodeGraph() if target.layer is None else item.GetNodeGraph(target.layer)
    elif target.group is not None:
        group = _find_group(s, target.group)
        graph = (
            group.GetPreClipNodeGraph() if target.stage == "pre" else group.GetPostClipNodeGraph()
        )
    else:
        graph = resolve_timeline(s, target.timeline).GetNodeGraph()
    if graph is None:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            "Resolve returned no node graph for this target.",
            hint="The item may not be a video clip, or the layer index exceeds nodeStackLayers.",
            retryable=False,
        )
    return graph


def _check_node(graph: ResolveObject, index: int) -> int:
    count = int(graph.GetNumNodes() or 0)
    if not 1 <= index <= count:
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            f"Node index {index} is out of range (graph has {count} nodes).",
            retryable=False,
        )
    return count


def _cache_name(s: ResolveSession, raw: object) -> str:
    for name, const_name in _CACHE_CONSTS.items():
        if getattr(s.resolve, const_name, None) == raw:
            return name
    return str(raw)


def _fail(what: str, hint: str | None = None) -> DavinciError:
    return DavinciError(
        ErrorCode.API_CALL_FAILED, f"{what} returned False.", hint=hint, retryable=False
    )


# -- node graph ---------------------------------------------------------------


class NodeInfo(BaseModel):
    index: int
    label: str
    tools: list[str]
    lut: str
    cache_mode: str


class NodeGraphInfo(BaseModel):
    node_count: int
    nodes: list[NodeInfo]
    note: str = "Node enabled state and CDL/primaries are write-only in the Resolve API."


class GetNodeGraphParams(_Params):
    target: GraphTarget = Field(default_factory=GraphTarget)


def _read_graph(s: ResolveSession, graph: ResolveObject) -> NodeGraphInfo:
    count = int(graph.GetNumNodes() or 0)
    nodes = [
        NodeInfo(
            index=i,
            label=str(graph.GetNodeLabel(i) or ""),
            tools=[str(t) for t in proxies(graph.GetToolsInNode(i))],
            lut=str(graph.GetLUT(i) or ""),
            cache_mode=_cache_name(s, graph.GetNodeCacheMode(i)),
        )
        for i in range(1, count + 1)
    ]
    return NodeGraphInfo(node_count=count, nodes=nodes)


@action(domain=DOMAIN, kind=Kind.READ, params=GetNodeGraphParams, result=NodeGraphInfo)
def get_node_graph(s: ResolveSession, p: GetNodeGraphParams) -> NodeGraphInfo:
    """Read node count, labels, tools, LUT and cache mode of a clip, group or timeline graph."""
    return _read_graph(s, _graph(s, p.target))


class NodeChange(BaseModel):
    node: int
    value: str


class SetNodeLutParams(_Params):
    target: GraphTarget
    node: int = Field(ge=1, description="1-based node index.")
    lut_path: str = Field(
        description="LUT file under the master LUT root (an empty string clears the node's LUT)."
    )


def _verify_lut(s: ResolveSession, p: SetNodeLutParams, result: NodeChange) -> Verification:
    observed = str(_graph(s, p.target).GetLUT(p.node) or "")
    expected = str(result.value)
    ok = observed == "" if expected == "" else Path(observed).name == Path(expected).name
    return Verification(ok=ok, observed={"lut": observed})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetNodeLutParams,
    result=NodeChange,
    verify=_verify_lut,
)
def set_node_lut(s: ResolveSession, p: SetNodeLutParams) -> NodeChange:
    """Set (or clear) a node LUT; files outside the LUT root are installed into it first."""
    graph = _graph(s, p.target)
    _check_node(graph, p.node)
    path = "" if p.lut_path == "" else _lut_reference(s, p.lut_path)
    if not graph.SetLUT(p.node, path):
        raise _fail(
            "Graph.SetLUT",
            "The LUT file may be invalid, or the path is not under the master LUT root.",
        )
    return NodeChange(node=p.node, value=path)


def lut_root() -> Path:
    """Resolve's master LUT folder (SetLUT resolves relative paths against it)."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT")
    if sys.platform == "win32":
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        return base / "Blackmagic Design/DaVinci Resolve/Support/LUT"
    return Path("/opt/resolve/LUT")


def _lut_reference(s: ResolveSession, lut_path: str) -> str:
    """Path SetLUT accepts: relative to the LUT root, installing outside files first.

    Measured on 21.1.1: SetLUT rejects absolute paths outside the LUT root. Files from the
    allowed roots are copied into <LUT root>/MCP (Resolve's own folder) and the LUT list is
    refreshed, which is what makes them selectable.
    """
    root = lut_root()
    candidate = Path(lut_path)
    if not candidate.is_absolute():
        return lut_path
    if candidate.resolve().is_relative_to(root.resolve()):
        return str(candidate.resolve().relative_to(root.resolve()))
    source = check_path(lut_path, runtime().allowed_roots, must_exist=True)
    data = source.read_bytes()
    target = root / "MCP" / source.name
    if target.exists() and target.read_bytes() != data:
        digest = hashlib.sha256(data).hexdigest()[:8]
        target = target.with_name(f"{source.stem}_{digest}{source.suffix}")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        s.project.RefreshLUTList()
    return f"MCP/{target.name}"


class SetNodeEnabledParams(_Params):
    target: GraphTarget
    node: int = Field(ge=1)
    enabled: bool


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetNodeEnabledParams,
    result=NodeChange,
    verify_reason="Graph has no getter for the node enabled state (write-only in the API).",
)
def set_node_enabled(s: ResolveSession, p: SetNodeEnabledParams) -> NodeChange:
    """Enable or disable a node of a clip, group or timeline graph."""
    graph = _graph(s, p.target)
    _check_node(graph, p.node)
    if not graph.SetNodeEnabled(p.node, p.enabled):
        raise _fail("Graph.SetNodeEnabled")
    return NodeChange(node=p.node, value=str(p.enabled).lower())


class SetNodeCacheModeParams(_Params):
    target: GraphTarget
    node: int = Field(ge=1)
    mode: CacheModeName


def _verify_cache(s: ResolveSession, p: SetNodeCacheModeParams, _: NodeChange) -> Verification:
    observed = _cache_name(s, _graph(s, p.target).GetNodeCacheMode(p.node))
    return Verification(ok=observed == p.mode, observed={"cache_mode": observed})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetNodeCacheModeParams,
    result=NodeChange,
    verify=_verify_cache,
)
def set_node_cache_mode(s: ResolveSession, p: SetNodeCacheModeParams) -> NodeChange:
    """Set the render cache mode (auto, enabled, disabled) of a node."""
    graph = _graph(s, p.target)
    _check_node(graph, p.node)
    if not graph.SetNodeCacheMode(p.node, const(s, _CACHE_CONSTS[p.mode])):
        raise _fail("Graph.SetNodeCacheMode")
    return NodeChange(node=p.node, value=p.mode)


class ApplyDrxParams(_Params):
    target: GraphTarget
    path: str = Field(description="Path of the .drx grade file.")
    grade_mode: Literal[0, 1, 2] = Field(
        default=0, description="0 = no keyframes, 1 = source timecode aligned, 2 = start frames."
    )
    apply_to_all_layers: bool = False
    reset_first: bool = Field(
        default=False,
        description="Reset all grades first: with the same node structure Resolve keeps the "
        "existing node layout and ignores the DRX positions.",
    )


class GradeApplied(BaseModel):
    path: str
    node_count: int


def _verify_drx(s: ResolveSession, p: ApplyDrxParams, result: GradeApplied) -> Verification:
    count = int(_graph(s, p.target).GetNumNodes() or 0)
    return Verification(
        ok=count >= 1,
        observed={"node_count": count},
        note="Only the node count can be read back; grade values are not readable.",
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ApplyDrxParams,
    result=GradeApplied,
    page="color",
    snapshot="timeline",
    verify=_verify_drx,
)
def apply_grade_from_drx(s: ResolveSession, p: ApplyDrxParams) -> GradeApplied:
    """Apply a DRX grade to a graph (Color page); replaces the current grade, no new version."""
    graph = _graph(s, p.target)
    path = check_path(p.path, runtime().allowed_roots, must_exist=True)
    if p.reset_first and not graph.ResetAllGrades():
        raise _fail("Graph.ResetAllGrades")
    if not graph.ApplyGradeFromDRX(str(path), p.grade_mode, p.apply_to_all_layers):
        raise _fail(
            "Graph.ApplyGradeFromDRX",
            "This call fails from the Edit page and for non-video items.",
        )
    return GradeApplied(path=str(path), node_count=int(graph.GetNumNodes() or 0))


class ResetGradesResult(BaseModel):
    node_count: int


def _plan_reset(s: ResolveSession, p: GetNodeGraphParams) -> PlanDraft:
    info = _read_graph(s, _graph(s, p.target))
    return PlanDraft(
        summary=f"Reset all grades of a graph with {info.node_count} node(s).",
        effects=[
            "Removes every correction of the active version; no version is created.",
            "Cannot be undone through the API.",
        ],
        targets=[p.target.model_dump_json(exclude_none=True)],
        fingerprint=_fingerprint(info.model_dump()),
    )


def _verify_reset(s: ResolveSession, p: GetNodeGraphParams, _: ResetGradesResult) -> Verification:
    graph = _graph(s, p.target)
    luts = [str(graph.GetLUT(i) or "") for i in range(1, int(graph.GetNumNodes() or 0) + 1)]
    return Verification(ok=not any(luts), observed={"luts": luts})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=GetNodeGraphParams,
    result=ResetGradesResult,
    snapshot="timeline",
    plan=_plan_reset,
    verify=_verify_reset,
)
def reset_all_grades(s: ResolveSession, p: GetNodeGraphParams) -> ResetGradesResult:
    """Reset all grades of a clip, group or timeline graph."""
    graph = _graph(s, p.target)
    if not graph.ResetAllGrades():
        raise _fail("Graph.ResetAllGrades")
    return ResetGradesResult(node_count=int(graph.GetNumNodes() or 0))


class ItemOnlyParams(_Params):
    item: ItemRef


class Done(BaseModel):
    done: bool = True


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ItemOnlyParams,
    result=Done,
    verify_reason="Node colors are not readable through the API.",
)
def reset_node_colors(s: ResolveSession, p: ItemOnlyParams) -> Done:
    """Reset the node colors of every node in the active version of a clip."""
    if not resolve_item(s, p.item).ResetAllNodeColors():
        raise _fail("TimelineItem.ResetAllNodeColors")
    return Done()


# -- CDL ------------------------------------------------------------------------

RGB = tuple[float, float, float]


class SetCdlParams(_Params):
    item: ItemRef
    node: int = Field(ge=1, description="1-based node index.")
    slope: RGB = (1.0, 1.0, 1.0)
    offset: RGB = (0.0, 0.0, 0.0)
    power: RGB = (1.0, 1.0, 1.0)
    saturation: float = Field(default=1.0, ge=0)


class CdlApplied(BaseModel):
    node: int
    cdl: dict[str, Any]


def _triple(v: RGB) -> str:
    return " ".join(f"{x:g}" for x in v)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetCdlParams,
    result=CdlApplied,
    verify_reason="TimelineItem has no GetCDL: CDL values are write-only in the API.",
)
def set_cdl(s: ResolveSession, p: SetCdlParams) -> CdlApplied:
    """Set CDL slope, offset, power and saturation on a node of a clip."""
    item = resolve_item(s, p.item)
    graph = item.GetNodeGraph()
    if graph is not None:
        _check_node(graph, p.node)
    cdl: dict[str, Any] = {
        "NodeIndex": p.node,
        "Slope": _triple(p.slope),
        "Offset": _triple(p.offset),
        "Power": _triple(p.power),
        "Saturation": p.saturation,
    }
    if not item.SetCDL(cdl):
        raise _fail("TimelineItem.SetCDL")
    return CdlApplied(node=p.node, cdl=cdl)


# -- versions -----------------------------------------------------------------


def _vtype(v: VersionType) -> int:
    return 0 if v == "local" else 1


class VersionList(BaseModel):
    local: list[str]
    remote: list[str]
    current_name: str | None
    current_type: VersionType | None


class ListVersionsParams(_Params):
    item: ItemRef


@action(domain=DOMAIN, kind=Kind.READ, params=ListVersionsParams, result=VersionList)
def list_versions(s: ResolveSession, p: ListVersionsParams) -> VersionList:
    """List the local and remote color versions of a clip and the current one."""
    item = resolve_item(s, p.item)
    current: dict[str, Any] = item.GetCurrentVersion() or {}
    raw_type = current.get("versionType")
    return VersionList(
        local=[str(n) for n in proxies(item.GetVersionNameList(0))],
        remote=[str(n) for n in proxies(item.GetVersionNameList(1))],
        current_name=current.get("versionName"),
        current_type=None if raw_type is None else ("local" if raw_type == 0 else "remote"),
    )


class VersionParams(_Params):
    item: ItemRef
    name: str = Field(min_length=1)
    version_type: VersionType = "local"


class VersionResult(BaseModel):
    name: str
    version_type: VersionType


def _names(item: ResolveObject, vt: VersionType) -> list[str]:
    return [str(n) for n in proxies(item.GetVersionNameList(_vtype(vt)))]


def _verify_added_version(s: ResolveSession, p: VersionParams, _: VersionResult) -> Verification:
    names = _names(resolve_item(s, p.item), p.version_type)
    return Verification(ok=p.name in names, observed={"versions": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=VersionParams,
    result=VersionResult,
    page="color",
    verify=_verify_added_version,
)
def add_version(s: ResolveSession, p: VersionParams) -> VersionResult:
    """Add a color version to a clip (Color page; fails from the Edit page)."""
    item = resolve_item(s, p.item)
    if p.name in _names(item, p.version_type):
        raise DavinciError(
            ErrorCode.WRONG_STATE, f"A {p.version_type} version {p.name!r} already exists."
        )
    if not item.AddVersion(p.name, _vtype(p.version_type)):
        raise _fail("TimelineItem.AddVersion", "Needs the Color page and a video clip.")
    return VersionResult(name=p.name, version_type=p.version_type)


def _verify_loaded(s: ResolveSession, p: VersionParams, _: VersionResult) -> Verification:
    current: dict[str, Any] = resolve_item(s, p.item).GetCurrentVersion() or {}
    return Verification(ok=current.get("versionName") == p.name, observed=current)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=VersionParams,
    result=VersionResult,
    page="color",
    verify=_verify_loaded,
)
def load_version(s: ResolveSession, p: VersionParams) -> VersionResult:
    """Make a named color version the active one of a clip."""
    item = resolve_item(s, p.item)
    if p.name not in _names(item, p.version_type):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.version_type} version {p.name!r}.")
    if not item.LoadVersionByName(p.name, _vtype(p.version_type)):
        raise _fail("TimelineItem.LoadVersionByName")
    return VersionResult(name=p.name, version_type=p.version_type)


class RenameVersionParams(_Params):
    item: ItemRef
    name: str = Field(min_length=1, description="Current version name.")
    new_name: str = Field(min_length=1)
    version_type: VersionType = "local"


def _verify_renamed(s: ResolveSession, p: RenameVersionParams, _: VersionResult) -> Verification:
    names = _names(resolve_item(s, p.item), p.version_type)
    return Verification(
        ok=p.new_name in names and p.name not in names, observed={"versions": names}
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RenameVersionParams,
    result=VersionResult,
    page="color",
    verify=_verify_renamed,
)
def rename_version(s: ResolveSession, p: RenameVersionParams) -> VersionResult:
    """Rename a color version of a clip."""
    item = resolve_item(s, p.item)
    if p.name not in _names(item, p.version_type):
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.version_type} version {p.name!r}.")
    if not item.RenameVersionByName(p.name, p.new_name, _vtype(p.version_type)):
        raise _fail("TimelineItem.RenameVersionByName")
    return VersionResult(name=p.new_name, version_type=p.version_type)


def _plan_delete_version(s: ResolveSession, p: VersionParams) -> PlanDraft:
    item = resolve_item(s, p.item)
    names = _names(item, p.version_type)
    if p.name not in names:
        raise DavinciError(ErrorCode.NOT_FOUND, f"No {p.version_type} version {p.name!r}.")
    return PlanDraft(
        summary=f"Delete {p.version_type} color version {p.name!r} of {item.GetName()!r}.",
        effects=["The version and its grade are lost; this cannot be undone through the API."],
        targets=[f"item:{item.GetUniqueId()}#{p.version_type}:{p.name}"],
        fingerprint=_fingerprint({"names": names, "current": item.GetCurrentVersion()}),
    )


def _verify_version_deleted(s: ResolveSession, p: VersionParams, _: VersionResult) -> Verification:
    names = _names(resolve_item(s, p.item), p.version_type)
    return Verification(ok=p.name not in names, observed={"versions": names})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=VersionParams,
    result=VersionResult,
    page="color",
    plan=_plan_delete_version,
    verify=_verify_version_deleted,
)
def delete_version(s: ResolveSession, p: VersionParams) -> VersionResult:
    """Delete a named color version of a clip."""
    item = resolve_item(s, p.item)
    if not item.DeleteVersionByName(p.name, _vtype(p.version_type)):
        raise _fail("TimelineItem.DeleteVersionByName", "The current version may not be deletable.")
    return VersionResult(name=p.name, version_type=p.version_type)


# -- LUT export -----------------------------------------------------------------


class ExportLutParams(_Params):
    item: ItemRef
    path: str = Field(description="Output file path (a .cube file, or .vlut for Panasonic).")
    lut_type: LutExportType = "33pt_cube"


class ExportedFile(BaseModel):
    path: str
    size: int


def _verify_export(s: ResolveSession, p: ExportLutParams, result: ExportedFile) -> Verification:
    f = Path(result.path)
    size = f.stat().st_size if f.is_file() else 0
    return Verification(ok=size > 0, observed={"exists": f.is_file(), "size": size})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ExportLutParams,
    result=ExportedFile,
    page="color",
    verify=_verify_export,
)
def export_lut(s: ResolveSession, p: ExportLutParams) -> ExportedFile:
    """Export the grade of a clip as a LUT file (works only from the Color page)."""
    item = resolve_item(s, p.item)
    path = check_path(p.path, runtime().allowed_roots)
    if not item.ExportLUT(const(s, _LUT_CONSTS[p.lut_type]), str(path)):
        raise _fail("TimelineItem.ExportLUT", "True is returned only from the Color page.")
    return ExportedFile(path=str(path), size=path.stat().st_size if path.is_file() else 0)


# -- copy grades --------------------------------------------------------------------


class CopyGradesParams(_Params):
    source: ItemRef
    targets: list[ItemRef] = Field(min_length=1)


class CopyGradesResult(BaseModel):
    source_id: str
    target_ids: list[str]


def _copy_state(s: ResolveSession, p: CopyGradesParams) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ref in p.targets:
        item = resolve_item(s, ref)
        graph = item.GetNodeGraph()
        out.append(
            {
                "id": item.GetUniqueId(),
                "name": item.GetName(),
                "nodes": int(graph.GetNumNodes() or 0) if graph is not None else 0,
                "version": item.GetCurrentVersion(),
            }
        )
    return out


def _plan_copy(s: ResolveSession, p: CopyGradesParams) -> PlanDraft:
    state = _copy_state(s, p)
    source = resolve_item(s, p.source)
    return PlanDraft(
        summary=f"Copy the grade of {source.GetName()!r} onto {len(state)} clip(s).",
        effects=[
            "Replaces the whole grade of each target (current layer); no version is created.",
            "Targets: " + ", ".join(str(t["name"]) for t in state),
        ],
        targets=[f"item:{t['id']}" for t in state],
        fingerprint=_fingerprint(state),
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=CopyGradesParams,
    result=CopyGradesResult,
    snapshot="timeline",
    plan=_plan_copy,
    verify_reason="Grades are not readable; CopyGrades returns True without creating a version.",
)
def copy_grades(s: ResolveSession, p: CopyGradesParams) -> CopyGradesResult:
    """Copy the current grade of one clip onto many (overwrites target grades)."""
    source = resolve_item(s, p.source)
    targets = [resolve_item(s, t) for t in p.targets]
    if not source.CopyGrades(targets):
        raise _fail("TimelineItem.CopyGrades")
    return CopyGradesResult(
        source_id=source.GetUniqueId(), target_ids=[t.GetUniqueId() for t in targets]
    )


# -- color groups ------------------------------------------------------------------


class ColorGroupInfo(BaseModel):
    name: str
    clip_ids: list[str]


class ColorGroupList(BaseModel):
    groups: list[ColorGroupInfo]


class ListGroupsParams(_Params):
    timeline: TimelineRef | None = None


@action(domain=DOMAIN, kind=Kind.READ, params=ListGroupsParams, result=ColorGroupList)
def list_color_groups(s: ResolveSession, p: ListGroupsParams) -> ColorGroupList:
    """List the color groups of the project with their clips in a timeline."""
    timeline = resolve_timeline(s, p.timeline)
    return ColorGroupList(
        groups=[
            ColorGroupInfo(
                name=str(g.GetName()),
                clip_ids=[c.GetUniqueId() for c in proxies(g.GetClipsInTimeline(timeline))],
            )
            for g in proxies(s.project.GetColorGroupsList())
        ]
    )


class GroupNameParams(_Params):
    name: str = Field(min_length=1)


class GroupResult(BaseModel):
    name: str


def _verify_group_exists(s: ResolveSession, p: GroupNameParams, _: GroupResult) -> Verification:
    names = [g.GetName() for g in proxies(s.project.GetColorGroupsList())]
    return Verification(ok=p.name in names, observed={"groups": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=GroupNameParams,
    result=GroupResult,
    verify=_verify_group_exists,
)
def create_color_group(s: ResolveSession, p: GroupNameParams) -> GroupResult:
    """Create a color group (the name must be unique)."""
    if any(g.GetName() == p.name for g in proxies(s.project.GetColorGroupsList())):
        raise DavinciError(ErrorCode.WRONG_STATE, f"Color group {p.name!r} already exists.")
    if s.project.AddColorGroup(p.name) is None:
        raise _fail("Project.AddColorGroup")
    return GroupResult(name=p.name)


class RenameGroupParams(_Params):
    name: str = Field(min_length=1)
    new_name: str = Field(min_length=1)


def _verify_group_renamed(s: ResolveSession, p: RenameGroupParams, _: GroupResult) -> Verification:
    names = [g.GetName() for g in proxies(s.project.GetColorGroupsList())]
    return Verification(ok=p.new_name in names and p.name not in names, observed={"groups": names})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RenameGroupParams,
    result=GroupResult,
    verify=_verify_group_renamed,
)
def rename_color_group(s: ResolveSession, p: RenameGroupParams) -> GroupResult:
    """Rename a color group."""
    if not _find_group(s, p.name).SetName(p.new_name):
        raise _fail("ColorGroup.SetName")
    return GroupResult(name=p.new_name)


def _plan_delete_group(s: ResolveSession, p: GroupNameParams) -> PlanDraft:
    group = _find_group(s, p.name)
    clips = [c.GetUniqueId() for c in proxies(group.GetClipsInTimeline())]
    return PlanDraft(
        summary=f"Delete color group {p.name!r} ({len(clips)} clip(s) in the current timeline).",
        effects=[
            "Clips become ungrouped; the group's pre/post clip grades are lost.",
        ],
        targets=[f"color_group:{p.name}"],
        fingerprint=_fingerprint({"name": p.name, "clips": sorted(clips)}),
    )


def _verify_group_deleted(s: ResolveSession, p: GroupNameParams, _: GroupResult) -> Verification:
    names = [g.GetName() for g in proxies(s.project.GetColorGroupsList())]
    return Verification(ok=p.name not in names, observed={"groups": names})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=GroupNameParams,
    result=GroupResult,
    plan=_plan_delete_group,
    verify=_verify_group_deleted,
)
def delete_color_group(s: ResolveSession, p: GroupNameParams) -> GroupResult:
    """Delete a color group; its clips become ungrouped."""
    if not s.project.DeleteColorGroup(_find_group(s, p.name)):
        raise _fail("Project.DeleteColorGroup")
    return GroupResult(name=p.name)


class AssignGroupParams(_Params):
    group: str = Field(min_length=1)
    items: list[ItemRef] = Field(min_length=1)


class AssignResult(BaseModel):
    group: str
    item_ids: list[str]


def _group_of(item: ResolveObject) -> str | None:
    g = item.GetColorGroup()
    return None if g is None else str(g.GetName())


def _verify_assigned(s: ResolveSession, p: AssignGroupParams, result: AssignResult) -> Verification:
    seen = {i: _group_of(resolve_item(s, r)) for i, r in zip(result.item_ids, p.items, strict=True)}
    return Verification(ok=all(g == p.group for g in seen.values()), observed=seen)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AssignGroupParams,
    result=AssignResult,
    verify=_verify_assigned,
)
def assign_to_color_group(s: ResolveSession, p: AssignGroupParams) -> AssignResult:
    """Assign clips to an existing color group."""
    group = _find_group(s, p.group)
    ids: list[str] = []
    for ref in p.items:
        item = resolve_item(s, ref)
        if not item.AssignToColorGroup(group):
            raise _fail("TimelineItem.AssignToColorGroup")
        ids.append(item.GetUniqueId())
    return AssignResult(group=p.group, item_ids=ids)


class RemoveGroupParams(_Params):
    items: list[ItemRef] = Field(min_length=1)


def _verify_removed(s: ResolveSession, p: RemoveGroupParams, result: AssignResult) -> Verification:
    seen = {i: _group_of(resolve_item(s, r)) for i, r in zip(result.item_ids, p.items, strict=True)}
    return Verification(ok=all(g is None for g in seen.values()), observed=seen)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RemoveGroupParams,
    result=AssignResult,
    verify=_verify_removed,
)
def remove_from_color_group(s: ResolveSession, p: RemoveGroupParams) -> AssignResult:
    """Remove clips from their color group."""
    ids: list[str] = []
    for ref in p.items:
        item = resolve_item(s, ref)
        if not item.RemoveFromColorGroup():
            raise _fail("TimelineItem.RemoveFromColorGroup")
        ids.append(item.GetUniqueId())
    return AssignResult(group="", item_ids=ids)


# -- DCTL (21.1) -----------------------------------------------------------------------


class ValidateDctlParams(_Params):
    source: str | None = Field(default=None, description="DCTL source code.")
    path: str | None = Field(default=None, description="Path of a .dctl file to validate.")

    @model_validator(mode="after")
    def _one(self) -> ValidateDctlParams:
        if (self.source is None) == (self.path is None):
            raise ValueError("use exactly one of source or path")
        return self


class DctlValidation(BaseModel):
    valid: bool
    error: str | None = None


@action(
    domain=DOMAIN,
    kind=Kind.READ,
    params=ValidateDctlParams,
    result=DctlValidation,
    since=(21, 1, 0),
)
def validate_dctl(s: ResolveSession, p: ValidateDctlParams) -> DctlValidation:
    """Validate DCTL source code (sensitive to formatting: one-line functions can fail)."""
    s.caps.require(s.resolve, "ValidateDCTL", since=(21, 1, 0))
    if p.path is not None:
        source = check_path(p.path, runtime().allowed_roots, must_exist=True).read_text()
    else:
        source = p.source or ""
    error = s.resolve.ValidateDCTL(source)
    return DctlValidation(valid=not error, error=str(error) if error else None)


class EncryptDctlParams(_Params):
    input_path: str = Field(description="Path of the .dctl file to encrypt.")
    output_folder: str = Field(description="Folder receiving the encrypted file.")
    name: SafeFileName | None = None
    expiry: str | None = Field(default=None, description="ISO 8601 expiry (default: none).")


class EncryptedDctl(BaseModel):
    output_folder: str
    files: list[str]


def _encrypted_files(p: EncryptDctlParams) -> list[str]:
    stem = Path(p.name).stem if p.name else Path(p.input_path).stem
    folder = Path(p.output_folder).expanduser()
    return sorted(f.name for f in folder.glob(f"{stem}*")) if folder.is_dir() else []


def _verify_encrypted(s: ResolveSession, p: EncryptDctlParams, _: EncryptedDctl) -> Verification:
    files = _encrypted_files(p)
    return Verification(ok=bool(files), observed={"files": files})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=EncryptDctlParams,
    result=EncryptedDctl,
    since=(21, 1, 0),
    verify=_verify_encrypted,
)
def encrypt_dctl(s: ResolveSession, p: EncryptDctlParams) -> EncryptedDctl:
    """Encrypt a DCTL file into an output folder."""
    s.caps.require(s.resolve, "EncryptDCTL", since=(21, 1, 0))
    src = check_path(p.input_path, runtime().allowed_roots, must_exist=True)
    out = check_path(p.output_folder, runtime().allowed_roots)
    out.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {"OutputFolder": str(out)}
    if p.name:
        options["Name"] = p.name
    if p.expiry:
        options["Expiry"] = p.expiry
    if not s.resolve.EncryptDCTL(str(src), options):
        raise _fail("Resolve.EncryptDCTL")
    return EncryptedDctl(output_folder=str(out), files=_encrypted_files(p))


ACTIONS = [
    get_node_graph,
    set_node_lut,
    set_node_enabled,
    set_node_cache_mode,
    apply_grade_from_drx,
    reset_all_grades,
    reset_node_colors,
    set_cdl,
    list_versions,
    add_version,
    load_version,
    rename_version,
    delete_version,
    export_lut,
    copy_grades,
    list_color_groups,
    create_color_group,
    rename_color_group,
    delete_color_group,
    assign_to_color_group,
    remove_from_color_group,
    validate_dctl,
    encrypt_dctl,
]
