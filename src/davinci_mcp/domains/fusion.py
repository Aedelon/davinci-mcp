# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: ANN401
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Fusion domain: compositions on timeline items, flow structure, inputs, keyframes, Fuses.

Fusion has no typed stub, so every call here follows the documented Fusion scripting
conventions and is checked against the live ``dir()`` lists
(``research/api-introspection/live-dir-21.1.1.10.json``). Traps encoded:

* ``dir()`` omits ``GetAttrs``/``SetAttrs`` on tools, inputs and comps although they work:
  they are called defensively, never gated by ``caps.has``.
* ``SetInput`` between ``Comp.Lock()`` and ``Unlock()`` is stored but not applied at render:
  value/keyframe edits never lock; only structural edits do (always in ``try/finally``).
* Every write is one undo step (``StartUndo``/``EndUndo``).
* ``AddModifier`` takes the registry id (``BezierSpline``).
* A comp created through the API is ignored at render unless MediaOut descends from MediaIn.
* ``GetPosTable`` has a variable format, and ``GetInputList`` may hand back names instead of
  proxies through the bridge: both shapes are handled.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import ItemRef, resolve_item
from davinci_mcp.safety.paths import check_path

if TYPE_CHECKING:
    from collections.abc import Generator

    from davinci_mcp.connection.backend import ResolveObject
    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "fusion"


# Fusion settings text, comp files and expressions are Lua. A keyword denylist is
# bypassable (string concatenation, _G[...], load...), so: whole-file / whole-snippet
# features require code execution, and expressions must match a strict allowlist.


def _require_code_execution(feature: str) -> None:
    if not runtime().settings.safety.enable_code_execution:
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            f"{feature} can execute code inside Resolve and is disabled.",
            hint="Set safety.enable_code_execution = true in config.toml to allow it.",
            retryable=False,
        )


_EXPR_TOKEN = re.compile(
    r"\s+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){0,3}|[-+*/%^(),<>=~]"
)
_EXPR_FUNCTIONS = frozenset(
    {
        "abs",
        "min",
        "max",
        "floor",
        "ceil",
        "sqrt",
        "exp",
        "log",
        "log10",
        "sin",
        "cos",
        "tan",
        "asin",
        "acos",
        "atan",
        "atan2",
        "pow",
        "iif",
    }
)
_EXPR_VALUES = frozenset({"time", "pi", "and", "or", "not", "true", "false"})
_EXPR_COMPONENTS = frozenset({"X", "Y", "Z"})
_MAX_REF_PARTS = 2  # Input, then optionally X/Y/Z
_GROUP_OPENERS = frozenset({"", "(", ",", "+", "-", "*", "/", "%", "^", "<", ">", "=", "~"})
# Lua and Fusion globals: never accepted as a tool reference nor as a tool name, so a tool
# renamed "os" or "comp" cannot smuggle a global into an expression.
RESERVED_NAMES = frozenset(
    {
        "_G",
        "_ENV",
        "os",
        "io",
        "debug",
        "package",
        "string",
        "table",
        "math",
        "coroutine",
        "utf8",
        "bit",
        "bit32",
        "jit",
        "ffi",
        "load",
        "loadstring",
        "loadfile",
        "dofile",
        "require",
        "getfenv",
        "setfenv",
        "rawget",
        "rawset",
        "setmetatable",
        "getmetatable",
        "pcall",
        "xpcall",
        "select",
        "next",
        "pairs",
        "ipairs",
        "print",
        "error",
        "assert",
        "self",
        "comp",
        "fusion",
        "app",
        "bmd",
        "resolve",
        "fu",
        "composition",
        "tool",
    }
)


_LUA_KEYWORDS = frozenset(
    {
        "and",
        "break",
        "do",
        "else",
        "elseif",
        "end",
        "false",
        "for",
        "function",
        "goto",
        "if",
        "in",
        "local",
        "nil",
        "not",
        "or",
        "repeat",
        "return",
        "then",
        "true",
        "until",
        "while",
    }
)
_MORE_GLOBALS = frozenset(
    {
        "eyeon",
        "ui",
        "dispatcher",
        "tostring",
        "tonumber",
        "unpack",
        "type",
        "collectgarbage",
        "newproxy",
        "module",
        "arg",
        "Execute",
        "RunScript",
        "_VERSION",
    }
)
_ALL_RESERVED = RESERVED_NAMES | _LUA_KEYWORDS | _MORE_GLOBALS


def _reserved(name: str) -> bool:
    """Lua keywords/globals, Fusion globals and every expression helper name."""
    return name in _ALL_RESERVED or name in _EXPR_FUNCTIONS or name in _EXPR_VALUES


def _is_name(token: str) -> bool:
    return bool(token) and (token[0].isalpha() or token[0] == "_")


def check_tool_name(name: str) -> str:
    """Tool names must be plain identifiers that do not shadow Lua/Fusion globals."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or _reserved(name):
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            f"Invalid tool name {name!r}: use a plain identifier that is not a reserved name.",
            retryable=False,
        )
    return name


def _check_expression(expression: str, inputs_by_tool: dict[str, set[str]]) -> None:
    """Allow numbers, arithmetic, math functions and ``Tool.Input[.X|Y|Z]`` references only.

    A reference must name an existing tool AND one of its inputs; only allowlisted math
    functions may be called, so tool members (``Tool.Comp.Execute``) are unreachable.
    """
    if runtime().settings.safety.enable_code_execution:
        return
    pos = 0
    previous = ""
    while pos < len(expression):
        match = _EXPR_TOKEN.match(expression, pos)
        if match is None:
            raise _expression_refused(expression, f"unexpected {expression[pos]!r}")
        token = match.group(0)
        pos = match.end()
        if token.isspace():
            continue
        is_paren = token == "("  # noqa: S105 - a token, not a secret
        # "(" is a call after a name and after ")" / a reference: only allowlisted functions
        # may be called; otherwise it must open a group (start, operator, "," or "(").
        if is_paren and previous not in _EXPR_FUNCTIONS and previous not in _GROUP_OPENERS:
            raise _expression_refused(expression, f"{previous!r} cannot be called")
        if _is_name(token):
            _check_name(expression, token, inputs_by_tool)
        previous = token


def _check_name(expression: str, token: str, inputs_by_tool: dict[str, set[str]]) -> None:
    head, *parts = token.split(".")
    if not parts:
        if token not in _EXPR_FUNCTIONS and token not in _EXPR_VALUES:
            raise _expression_refused(expression, f"name {token!r} is not allowed")
        return
    inputs = None if _reserved(head) else inputs_by_tool.get(head)
    if inputs is None:
        raise _expression_refused(expression, f"{head!r} is not a tool of this comp")
    if parts[0] not in inputs:
        raise _expression_refused(expression, f"{head}.{parts[0]} is not an input")
    if len(parts) > _MAX_REF_PARTS or (
        len(parts) == _MAX_REF_PARTS and parts[1] not in _EXPR_COMPONENTS
    ):
        raise _expression_refused(expression, f"{token!r} is not Tool.Input[.X|Y|Z]")


def _expression_refused(expression: str, why: str) -> DavinciError:
    return DavinciError(
        ErrorCode.CAPABILITY_UNAVAILABLE,
        f"Expression refused ({why}): {expression!r}.",
        hint="Without code execution, expressions may use numbers, + - * / % ^ ( ), time, "
        "math functions and Tool.Input references. Enable safety.enable_code_execution "
        "for arbitrary Lua.",
        retryable=False,
    )


def _guard_input_id(input_id: str) -> None:
    """Script inputs (FrameRenderScript, StartRenderScript...) run Lua at render time."""
    if "script" in input_id.lower():
        _require_code_execution(f"setting the script input {input_id!r}")


_AUTO_POS = -32768  # AddTool(id, x, y): "place automatically"
_FUSE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]*\.fuse$")
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CompParams(_Params):
    item: ItemRef
    comp: str | int | None = Field(
        default=None, description="Comp name or 1-based index on the item (default: the first)."
    )


# -- low-level helpers ---------------------------------------------------------------


def _fail(
    message: str, hint: str | None = None, code: ErrorCode = ErrorCode.API_CALL_FAILED
) -> NoReturn:
    raise DavinciError(code, message, hint=hint, retryable=False)


def _not_found(message: str, hint: str | None = None) -> NoReturn:
    return _fail(message, hint, ErrorCode.NOT_FOUND)


def _fingerprint(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _prop(obj: ResolveObject, name: str) -> Any:
    """Read a Fusion property that may come back as a value or as a zero-arg callable."""
    value = getattr(obj, name, None)
    return value() if callable(value) else value


def _attrs(obj: ResolveObject) -> dict[str, Any]:
    """``GetAttrs()`` (absent from dir() but functional); {} when unavailable."""
    fn = getattr(obj, "GetAttrs", None)
    if not callable(fn):
        return {}
    try:
        raw = fn()
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _listify(raw: Any) -> list[Any]:
    """Fusion tables arrive as dict {1: x, 2: y} or as lists."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = list(raw.items())
        with suppress(TypeError, ValueError):
            items.sort(key=lambda kv: float(kv[0]))
        return [v for _, v in items]
    return list(raw) if isinstance(raw, (list, tuple)) else [raw]


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return str(value)


def _norm(value: Any) -> Any:
    """Normalise a value for comparison (Fusion tables {1: a, 2: b} equal lists)."""
    if isinstance(value, dict) and value:
        keys = [str(k) for k in value]
        if all(k.isdigit() for k in keys):
            ordered = sorted(value.items(), key=lambda kv: int(str(kv[0])))
            if [int(str(k)) for k, _ in ordered] == list(range(1, len(ordered) + 1)):
                return [_norm(v) for _, v in ordered]
        return {str(k): _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def _same(a: Any, b: Any) -> bool:
    a, b = _norm(a), _norm(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-4, abs_tol=1e-6)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if (isinstance(a, str) and isinstance(b, (int, float))) or (
        isinstance(b, str) and isinstance(a, (int, float))
    ):
        return str(a) == str(b)
    return a == b


# -- comp / tool / input access -------------------------------------------------------


def _comp_names(item: ResolveObject) -> list[str]:
    return [str(n) for n in _listify(item.GetFusionCompNameList())]


def _open_comp(s: ResolveSession, p: _CompParams) -> tuple[ResolveObject, ResolveObject, str]:
    """(item, comp, comp name) for the params; NOT_FOUND with a hint when absent."""
    item = resolve_item(s, p.item)
    for method in ("GetFusionCompNameList", "GetFusionCompByIndex", "GetFusionCompByName"):
        s.caps.require(item, method)
    names = _comp_names(item)
    if not names:
        _not_found(
            f"{item.GetName()!r} has no Fusion composition.",
            "Create one with fusion_write(action='add_comp').",
        )
    if isinstance(p.comp, str):
        if p.comp not in names:
            _not_found(f"No comp named {p.comp!r}; available: {names}.")
        return item, item.GetFusionCompByName(p.comp), p.comp
    index = p.comp or 1
    if not 1 <= index <= len(names):
        _not_found(f"Comp index {index} out of range (item has {len(names)}).")
    comp = item.GetFusionCompByIndex(index)
    if comp is None:
        _fail(f"GetFusionCompByIndex({index}) returned nothing.")
    return item, comp, names[index - 1]


def _tools(comp: ResolveObject, selected_only: bool = False) -> list[ResolveObject]:
    return [t for t in _listify(comp.GetToolList(selected_only)) if t is not None]


def _tool_name(tool: ResolveObject) -> str:
    name = _prop(tool, "Name")
    return str(name if name is not None else _attrs(tool).get("TOOLS_Name", ""))


def _tool_id(tool: ResolveObject) -> str:
    rid = _prop(tool, "ID")
    return str(rid if rid is not None else _attrs(tool).get("TOOLS_RegID", ""))


def _find_tool(comp: ResolveObject, name: str) -> ResolveObject:
    tool = comp.FindTool(name)
    if tool is None:
        known = [_tool_name(t) for t in _tools(comp)][:50]
        _not_found(f"No tool named {name!r} in the comp.", f"Existing tools: {known}")
    return tool


def _input_objects(tool: ResolveObject) -> list[Any]:
    return _listify(tool.GetInputList())


def _input_label(entry: Any) -> str:
    """Name of an input entry: a proxy (ID/Name) or a bare string."""
    if isinstance(entry, str):
        return entry
    ident = _prop(entry, "ID")
    return str(ident if ident is not None else _prop(entry, "Name"))


def _find_input(s: ResolveSession, tool: ResolveObject, name: str) -> ResolveObject:
    """FusionInput proxy by registry id or display name."""
    for entry in _input_objects(tool):
        if isinstance(entry, str):
            continue
        if name in (str(_prop(entry, "ID")), str(_prop(entry, "Name"))):
            return entry
    candidate = getattr(tool, name, None)
    if candidate is not None and s.caps.has(candidate, "GetConnectedOutput"):
        return candidate
    return _not_found(
        f"Tool {_tool_name(tool)!r} has no input {name!r}.",
        "List valid ids with fusion_read(action='tool_inputs').",
    )


def _is_animated(inp: ResolveObject) -> bool:
    return inp.GetConnectedOutput() is not None


def _keyframes(inp: ResolveObject) -> dict[str, Any]:
    raw = inp.GetKeyFrames()
    if not isinstance(raw, dict):
        return {}
    return {str(k): _json(v) for k, v in raw.items()}


def _output_tool_name(output: ResolveObject) -> str | None:
    getter = getattr(output, "GetTool", None)
    if not callable(getter):
        return None
    try:
        owner = getter()
    except Exception:
        return None
    return _tool_name(owner) if owner is not None else None


def _pos(comp: ResolveObject, tool: ResolveObject) -> tuple[float, float] | None:
    try:
        table = _listify(comp.CurrentFrame.FlowView.GetPosTable(tool))
        return float(table[0]), float(table[1])
    except Exception:
        return None


@contextmanager
def _transaction(comp: ResolveObject, label: str, *, lock: bool = False) -> Generator[None]:
    """One undo step; optionally lock the comp (structural edits only), always unlocked after."""
    comp.StartUndo(label)
    try:
        if lock:
            comp.Lock()
            try:
                yield
            finally:
                comp.Unlock()
        else:
            yield
    finally:
        comp.EndUndo(True)


# =====================================================================================
# READ
# =====================================================================================


class CompList(BaseModel):
    item_id: str
    count: int
    names: list[str]


class ToolBrief(BaseModel):
    name: str
    id: str
    x: float | None = None
    y: float | None = None


class Connection(BaseModel):
    from_tool: str | None
    to_tool: str
    to_input: str


class CompInfo(BaseModel):
    comp: str
    tool_count: int
    tools: list[ToolBrief]
    connections: list[Connection]
    active_tool: str | None
    selected: list[str]
    current_time: float | None
    render_start: float | None
    render_end: float | None
    global_start: float | None
    global_end: float | None
    undo_depth: int
    undo_recent: list[str]


class InputInfo(BaseModel):
    id: str
    name: str | None = None
    data_type: str | None = None
    value: Any = None
    animated: bool = False
    expression: str | None = None
    connected_from: str | None = None
    keyframes: dict[str, Any] | None = None


class ToolInputs(BaseModel):
    tool: str
    tool_id: str
    total: int
    offset: int
    inputs: list[InputInfo]


class RegEntry(BaseModel):
    id: str
    name: str | None = None
    type: str | None = None


class RegTypes(BaseModel):
    total_matched: int
    offset: int
    types: list[RegEntry]


class FuseFile(BaseModel):
    path: str
    size: int


class FuseList(BaseModel):
    directory: str
    exists: bool
    fuses: list[FuseFile]


class ListCompsParams(_Params):
    item: ItemRef


@action(domain=DOMAIN, kind=Kind.READ, params=ListCompsParams, result=CompList)
def list_comps(s: ResolveSession, p: ListCompsParams) -> CompList:
    """List the Fusion compositions attached to a timeline item."""
    item = resolve_item(s, p.item)
    s.caps.require(item, "GetFusionCompNameList")
    names = _comp_names(item)
    return CompList(item_id=item.GetUniqueId(), count=len(names), names=names)


class CompInfoParams(_CompParams):
    include_connections: bool = Field(
        default=True, description="Walk every input for connections (slow on big comps)."
    )


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@action(domain=DOMAIN, kind=Kind.READ, params=CompInfoParams, result=CompInfo, timeout_s=60.0)
def comp_info(s: ResolveSession, p: CompInfoParams) -> CompInfo:
    """Flow structure of a comp: tools, connections, selection, time range, undo stack."""
    _, comp, name = _open_comp(s, p)
    tools = _tools(comp)
    briefs: list[ToolBrief] = []
    for tool in tools:
        pos = _pos(comp, tool)
        briefs.append(
            ToolBrief(
                name=_tool_name(tool),
                id=_tool_id(tool),
                x=pos[0] if pos else None,
                y=pos[1] if pos else None,
            )
        )
    connections: list[Connection] = []
    if p.include_connections:
        for tool in tools:
            for entry in _input_objects(tool):
                if isinstance(entry, str):
                    continue
                out = entry.GetConnectedOutput()
                if out is not None:
                    connections.append(
                        Connection(
                            from_tool=_output_tool_name(out),
                            to_tool=_tool_name(tool),
                            to_input=_input_label(entry),
                        )
                    )
    active = _prop(comp, "ActiveTool")
    attrs = _attrs(comp)
    undo = _listify(comp.GetUndoStack())
    recent = [
        str(e.get("Name", e.get("name", "")) if isinstance(e, dict) else e) for e in undo[-5:]
    ]
    return CompInfo(
        comp=name,
        tool_count=len(tools),
        tools=briefs,
        connections=connections,
        active_tool=_tool_name(active) if active is not None else None,
        selected=[_tool_name(t) for t in _tools(comp, True)],
        current_time=_num(_prop(comp, "CurrentTime")),
        render_start=_num(attrs.get("COMPN_RenderStart")),
        render_end=_num(attrs.get("COMPN_RenderEnd")),
        global_start=_num(attrs.get("COMPN_GlobalStart")),
        global_end=_num(attrs.get("COMPN_GlobalEnd")),
        undo_depth=len(undo),
        undo_recent=recent,
    )


class ToolInputsParams(_CompParams):
    tool: str
    names: list[str] | None = Field(default=None, description="Only these input ids/names.")
    time: float | None = Field(default=None, description="Comp time for values (default: now).")
    include_keyframes: bool = False
    limit: int = Field(default=60, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def _describe_input(
    s: ResolveSession, tool: ResolveObject, entry: Any, time: float | None, keys: bool
) -> InputInfo:
    label = _input_label(entry)
    value = tool.GetInput(label) if time is None else tool.GetInput(label, time)
    if isinstance(entry, str):
        return InputInfo(id=label, value=_json(value))
    attrs = _attrs(entry)
    out = entry.GetConnectedOutput()
    expression = entry.GetExpression()
    return InputInfo(
        id=label,
        name=_prop(entry, "Name"),
        data_type=attrs.get("INPS_DataType"),
        value=_json(value),
        animated=out is not None and expression is None,
        expression=str(expression) if expression else None,
        connected_from=_output_tool_name(out) if out is not None else None,
        keyframes=_keyframes(entry) if keys else None,
    )


@action(domain=DOMAIN, kind=Kind.READ, params=ToolInputsParams, result=ToolInputs, timeout_s=60.0)
def tool_inputs(s: ResolveSession, p: ToolInputsParams) -> ToolInputs:
    """Inputs of a tool: ids, data types, values at a time, animation, keyframes (paginated)."""
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    entries = _input_objects(tool)
    if p.names is not None:
        wanted = set(p.names)
        entries = [
            e
            for e in entries
            if _input_label(e) in wanted or (not isinstance(e, str) and _prop(e, "Name") in wanted)
        ]
    page = entries[p.offset : p.offset + p.limit]
    return ToolInputs(
        tool=_tool_name(tool),
        tool_id=_tool_id(tool),
        total=len(entries),
        offset=p.offset,
        inputs=[_describe_input(s, tool, e, p.time, p.include_keyframes) for e in page],
    )


class RegTypesParams(_Params):
    query: str | None = Field(default=None, description="Case-insensitive substring of the id.")
    limit: int = Field(default=50, ge=1, le=300)
    offset: int = Field(default=0, ge=0)


@action(domain=DOMAIN, kind=Kind.READ, params=RegTypesParams, result=RegTypes)
def registry_types(s: ResolveSession, p: RegTypesParams) -> RegTypes:
    """Search the ~1300 registered Fusion tool/modifier ids (Fusion.GetRegSummary), paginated."""
    fusion = s.resolve.Fusion()
    if fusion is None:
        _fail("Resolve.Fusion() returned nothing.", "Open a project first.")
    s.caps.require(fusion, "GetRegSummary")
    entries: list[RegEntry] = []
    for raw in _listify(fusion.GetRegSummary()):
        if isinstance(raw, dict):
            ident = str(raw.get("ID", raw.get("Id", "")))
            entries.append(RegEntry(id=ident, name=raw.get("Name"), type=raw.get("Type")))
        else:
            entries.append(RegEntry(id=str(raw)))
    if p.query:
        q = p.query.lower()
        entries = [e for e in entries if q in e.id.lower() or q in (e.name or "").lower()]
    return RegTypes(
        total_matched=len(entries), offset=p.offset, types=entries[p.offset : p.offset + p.limit]
    )


def _official_fuses_dir() -> Path:
    """The user Fuses folder of the current platform."""
    if sys.platform == "darwin":
        base = "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Fuses"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "~/AppData/Roaming")
        base = f"{appdata}/Blackmagic Design/DaVinci Resolve/Support/Fusion/Fuses"
    else:
        base = "~/.local/share/DaVinciResolve/Fusion/Fuses"
    return Path(base).expanduser()


def _fuses_root(directory: str | None) -> Path:
    """Official Fuses folder, or a folder inside the configured allowed roots; else refuse."""
    official = _official_fuses_dir().resolve()
    if directory is None:
        return official
    candidate = Path(directory).expanduser().resolve()
    if candidate == official or candidate.is_relative_to(official):
        return candidate
    return check_path(directory, runtime().allowed_roots)


class ListFusesParams(_Params):
    directory: str | None = Field(
        default=None,
        description="Fuses folder to scan (default: the official user Fuses folder; "
        "other folders must be inside the allowed roots).",
    )
    limit: int = Field(default=300, ge=1, le=1000)


@action(domain=DOMAIN, kind=Kind.READ, params=ListFusesParams, result=FuseList)
def list_fuses(_: ResolveSession, p: ListFusesParams) -> FuseList:
    """List installed .fuse files in the user Fuses folder."""
    root = _fuses_root(p.directory)
    if not root.is_dir():
        return FuseList(directory=str(root), exists=False, fuses=[])
    files = sorted(root.rglob("*.fuse"))[: p.limit]
    return FuseList(
        directory=str(root),
        exists=True,
        fuses=[FuseFile(path=str(f.relative_to(root)), size=f.stat().st_size) for f in files],
    )


# =====================================================================================
# WRITE: comps on an item
# =====================================================================================


class CompResult(BaseModel):
    names: list[str]
    count: int
    comp: str | None = None


class AddCompParams(_Params):
    item: ItemRef


def _verify_added_comp(s: ResolveSession, p: Any, r: CompResult) -> Verification:
    names = _comp_names(resolve_item(s, p.item))
    ok = names == r.names and (r.comp is None or r.comp in names)
    return Verification(ok=ok, observed={"names": names})


def _new_comp_result(item: ResolveObject, before: list[str], comp: Any, what: str) -> CompResult:
    if comp is None:
        _fail(f"{what} returned nothing.")
    after = _comp_names(item)
    if len(after) <= len(before):
        _fail(f"{what} did not add a composition.")
    added = [n for n in after if n not in before]
    return CompResult(names=after, count=len(after), comp=added[0] if added else after[-1])


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AddCompParams,
    result=CompResult,
    verify=_verify_added_comp,
)
def add_comp(s: ResolveSession, p: AddCompParams) -> CompResult:
    """Add an empty Fusion composition to a timeline item."""
    item = resolve_item(s, p.item)
    s.caps.require(item, "AddFusionComp")
    before = _comp_names(item)
    return _new_comp_result(item, before, item.AddFusionComp(), "TimelineItem.AddFusionComp")


class ImportCompParams(_Params):
    item: ItemRef
    path: str = Field(description="A .comp file inside the allowed roots.")


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ImportCompParams,
    result=CompResult,
    verify=_verify_added_comp,
)
def import_comp(s: ResolveSession, p: ImportCompParams) -> CompResult:
    """Import a .comp file as a new Fusion composition of a timeline item."""
    _require_code_execution("import_comp (.comp files may embed scripts)")
    path = check_path(p.path, runtime().allowed_roots, must_exist=True)
    if path.suffix.lower() != ".comp":
        raise DavinciError(ErrorCode.INVALID_PARAMS, "Only .comp files can be imported.")
    item = resolve_item(s, p.item)
    s.caps.require(item, "ImportFusionComp")
    before = _comp_names(item)
    return _new_comp_result(
        item, before, item.ImportFusionComp(str(path)), "TimelineItem.ImportFusionComp"
    )


class ExportCompParams(_CompParams):
    path: str = Field(description="Destination .comp file inside the allowed roots.")


class PathResult(BaseModel):
    path: str


def _verify_file(_: ResolveSession, __: Any, r: PathResult) -> Verification:
    f = Path(r.path)
    return Verification(ok=f.is_file() and f.stat().st_size > 0, observed={"path": r.path})


@action(
    domain=DOMAIN, kind=Kind.WRITE, params=ExportCompParams, result=PathResult, verify=_verify_file
)
def export_comp(s: ResolveSession, p: ExportCompParams) -> PathResult:
    """Export a Fusion composition of an item to a new .comp file."""
    path = check_path(p.path, runtime().allowed_roots)
    if path.suffix.lower() != ".comp":
        raise DavinciError(ErrorCode.INVALID_PARAMS, "The destination must end in .comp.")
    if path.exists():
        raise DavinciError(ErrorCode.WRONG_STATE, f"{path} already exists; pick a new file name.")
    item, _, name = _open_comp(s, p)
    s.caps.require(item, "ExportFusionComp")
    index = _comp_names(item).index(name) + 1
    if not item.ExportFusionComp(str(path), index):
        _fail("TimelineItem.ExportFusionComp returned False.")
    return PathResult(path=str(path))


class LoadCompParams(_Params):
    item: ItemRef
    name: str


class LoadedComp(BaseModel):
    comp: str


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=LoadCompParams,
    result=LoadedComp,
    verify_reason="LoadFusionCompByName has no read-back of the active comp; only its return "
    "value is checked.",
    idempotent=True,
)
def load_comp(s: ResolveSession, p: LoadCompParams) -> LoadedComp:
    """Make a named Fusion composition the active one of a timeline item."""
    item = resolve_item(s, p.item)
    s.caps.require(item, "LoadFusionCompByName")
    if p.name not in _comp_names(item):
        _not_found(f"No comp named {p.name!r} on the item.")
    if item.LoadFusionCompByName(p.name) is None:
        _fail("TimelineItem.LoadFusionCompByName returned nothing.")
    return LoadedComp(comp=p.name)


class RenameCompParams(_Params):
    item: ItemRef
    old_name: str
    new_name: str = Field(min_length=1)


def _verify_renamed(s: ResolveSession, p: RenameCompParams, _result: LoadedComp) -> Verification:
    names = _comp_names(resolve_item(s, p.item))
    return Verification(
        ok=p.new_name in names and p.old_name not in names, observed={"names": names}
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RenameCompParams,
    result=LoadedComp,
    verify=_verify_renamed,
)
def rename_comp(s: ResolveSession, p: RenameCompParams) -> LoadedComp:
    """Rename a Fusion composition of a timeline item."""
    item = resolve_item(s, p.item)
    s.caps.require(item, "RenameFusionCompByName")
    names = _comp_names(item)
    if p.old_name not in names:
        _not_found(f"No comp named {p.old_name!r}; available: {names}.")
    if p.new_name in names:
        _fail(f"A comp named {p.new_name!r} already exists.", code=ErrorCode.WRONG_STATE)
    if not item.RenameFusionCompByName(p.old_name, p.new_name):
        _fail("TimelineItem.RenameFusionCompByName returned False.")
    return LoadedComp(comp=p.new_name)


# =====================================================================================
# WRITE: tools and inputs
# =====================================================================================


class ToolResult(BaseModel):
    tool: str
    id: str


class AddToolParams(_CompParams):
    tool_id: str = Field(description="Registry id, e.g. 'Background', 'Merge', 'TextPlus'.")
    name: str | None = Field(default=None, description="Tool name (default: Fusion's).")
    x: float | None = Field(default=None, description="Flow x position (default: automatic).")
    y: float | None = Field(default=None, description="Flow y position (default: automatic).")


def _verify_tool_added(s: ResolveSession, p: Any, r: ToolResult) -> Verification:
    _, comp, _ = _open_comp(s, p)
    tool = comp.FindTool(r.tool)
    ok = tool is not None and _tool_id(tool) == r.id
    return Verification(ok=ok, observed={"found": tool is not None})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=AddToolParams,
    result=ToolResult,
    verify=_verify_tool_added,
)
def add_tool(s: ResolveSession, p: AddToolParams) -> ToolResult:
    """Add a tool to a comp by registry id, with optional name and flow position."""
    _, comp, _ = _open_comp(s, p)
    if p.name is not None:
        check_tool_name(p.name)
    if p.name is not None and comp.FindTool(p.name) is not None:
        _fail(f"A tool named {p.name!r} already exists.", code=ErrorCode.WRONG_STATE)
    x = _AUTO_POS if p.x is None else p.x
    y = _AUTO_POS if p.y is None else p.y
    with _transaction(comp, f"MCP add {p.tool_id}", lock=True):
        tool = comp.AddTool(p.tool_id, x, y)
        if tool is None:
            _fail(
                f"Comp.AddTool({p.tool_id!r}) returned nothing.",
                "Check the id with fusion_read(action='registry_types').",
            )
        if p.name is not None and not tool.SetAttrs({"TOOLS_Name": p.name}):
            _fail("Could not apply the requested tool name.")
        name = _tool_name(tool)
    return ToolResult(tool=name, id=_tool_id(tool))


class InputSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    input: str = Field(description="Input id, e.g. 'Blend', 'StyledText', 'Center'.")
    value: Any = Field(description="Number, string, bool or list for point/color tables.")
    time: float | None = Field(default=None, description="Comp time (default: current time).")


class SetInputsParams(_CompParams):
    sets: list[InputSet] = Field(min_length=1, max_length=200)


class AppliedInput(BaseModel):
    tool: str
    input: str
    time: float | None
    value: Any


class SetInputsResult(BaseModel):
    applied: list[AppliedInput]


def _read_input(tool: ResolveObject, name: str, time: float | None) -> Any:
    return _json(tool.GetInput(name) if time is None else tool.GetInput(name, time))


def _verify_inputs(s: ResolveSession, p: SetInputsParams, _result: SetInputsResult) -> Verification:
    _, comp, _ = _open_comp(s, p)
    bad: list[dict[str, Any]] = []
    for e in p.sets:
        got = _read_input(_find_tool(comp, e.tool), e.input, e.time)
        if not _same(got, e.value):
            bad.append({"tool": e.tool, "input": e.input, "expected": e.value, "got": got})
    return Verification(ok=not bad, observed={"mismatches": bad})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetInputsParams,
    result=SetInputsResult,
    verify=_verify_inputs,
)
def set_inputs(s: ResolveSession, p: SetInputsParams) -> SetInputsResult:
    """Set many tool inputs in one undo step (the comp is not locked: renders need it)."""
    _, comp, _ = _open_comp(s, p)
    for e in p.sets:  # fail before touching anything
        _guard_input_id(e.input)
        _find_input(s, _find_tool(comp, e.tool), e.input)
    applied: list[AppliedInput] = []
    with _transaction(comp, "MCP set inputs"):
        for e in p.sets:
            tool = _find_tool(comp, e.tool)
            if e.time is None:
                tool.SetInput(e.input, e.value)
            else:
                tool.SetInput(e.input, e.value, e.time)
            applied.append(
                AppliedInput(
                    tool=e.tool,
                    input=e.input,
                    time=e.time,
                    value=_read_input(tool, e.input, e.time),
                )
            )
    return SetInputsResult(applied=applied)


class ConnectParams(_CompParams):
    tool: str = Field(description="Tool receiving the connection.")
    input: str = Field(description="Its input id, e.g. 'Background', 'Foreground', 'Input'.")
    source_tool: str
    source_output: str | None = Field(
        default=None, description="Output id on the source (default: its main output)."
    )


class ConnectResult(BaseModel):
    tool: str
    input: str
    source_tool: str | None


def _source_output(comp: ResolveObject, p: ConnectParams) -> ResolveObject:
    source = _find_tool(comp, p.source_tool)
    if p.source_output is None:
        out = source.FindMainOutput(1)
    else:
        out = next(
            (
                o
                for o in _listify(source.GetOutputList())
                if p.source_output in (str(_prop(o, "ID")), str(_prop(o, "Name")))
            ),
            None,
        )
    if out is None:
        _not_found(f"Tool {p.source_tool!r} has no output {p.source_output or '(main)'}.")
    return out


def _verify_connected(s: ResolveSession, p: ConnectParams, _result: ConnectResult) -> Verification:
    _, comp, _ = _open_comp(s, p)
    out = _find_input(s, _find_tool(comp, p.tool), p.input).GetConnectedOutput()
    owner = _output_tool_name(out) if out is not None else None
    return Verification(ok=owner == p.source_tool, observed={"connected_from": owner})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ConnectParams,
    result=ConnectResult,
    verify=_verify_connected,
)
def connect(s: ResolveSession, p: ConnectParams) -> ConnectResult:
    """Connect a tool input to the output of another tool."""
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    _find_input(s, tool, p.input)
    out = _source_output(comp, p)
    with _transaction(comp, f"MCP connect {p.source_tool} -> {p.tool}", lock=True):
        if not tool.ConnectInput(p.input, out):
            _fail("FusionTool.ConnectInput returned False.", "Check the input id and data types.")
    return ConnectResult(tool=p.tool, input=p.input, source_tool=p.source_tool)


class DisconnectParams(_CompParams):
    tool: str
    input: str


def _verify_disconnected(
    s: ResolveSession, p: DisconnectParams, _result: ConnectResult
) -> Verification:
    _, comp, _ = _open_comp(s, p)
    out = _find_input(s, _find_tool(comp, p.tool), p.input).GetConnectedOutput()
    return Verification(ok=out is None, observed={"still_connected": out is not None})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=DisconnectParams,
    result=ConnectResult,
    verify=_verify_disconnected,
)
def disconnect(s: ResolveSession, p: DisconnectParams) -> ConnectResult:
    """Disconnect a tool input (one undo step; undo restores the link)."""
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    inp = _find_input(s, tool, p.input)
    previous = inp.GetConnectedOutput()
    with _transaction(comp, f"MCP disconnect {p.tool}.{p.input}", lock=True):
        tool.ConnectInput(p.input, None)
    return ConnectResult(
        tool=p.tool,
        input=p.input,
        source_tool=_output_tool_name(previous) if previous is not None else None,
    )


class Key(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: float
    value: Any


class SetKeyframesParams(_CompParams):
    tool: str
    input: str
    keys: list[Key] = Field(min_length=1, max_length=500)


class KeyframesResult(BaseModel):
    tool: str
    input: str
    keyframes: dict[str, Any]


def _verify_keys(
    s: ResolveSession, p: SetKeyframesParams, _result: KeyframesResult
) -> Verification:
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    animated = _is_animated(_find_input(s, tool, p.input))
    bad = [
        {"time": k.time, "expected": k.value, "got": got}
        for k in p.keys
        if not _same(got := _read_input(tool, p.input, k.time), k.value)
    ]
    return Verification(ok=animated and not bad, observed={"animated": animated, "mismatches": bad})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetKeyframesParams,
    result=KeyframesResult,
    verify=_verify_keys,
)
def set_keyframes(s: ResolveSession, p: SetKeyframesParams) -> KeyframesResult:
    """Animate an input: add a BezierSpline when needed, then key each (time, value)."""
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    _guard_input_id(p.input)
    inp = _find_input(s, tool, p.input)
    with _transaction(comp, f"MCP keyframes {p.tool}.{p.input}"):
        if not _is_animated(inp):
            s.caps.require(tool, "AddModifier")
            if not tool.AddModifier(p.input, "BezierSpline"):
                _fail(
                    f"AddModifier({p.input!r}, 'BezierSpline') returned False.",
                    "Group headers (NestControl) and non-numeric inputs refuse modifiers.",
                )
        for k in p.keys:
            tool.SetInput(p.input, k.value, k.time)
    return KeyframesResult(tool=p.tool, input=p.input, keyframes=_keyframes(inp))


class SetExpressionParams(_CompParams):
    tool: str
    input: str
    expression: str = Field(
        description="Fusion expression, e.g. 'Merge1.Blend * 0.5'. Empty clears."
    )


class ExpressionResult(BaseModel):
    tool: str
    input: str
    expression: str | None


def _verify_expression(
    s: ResolveSession, p: SetExpressionParams, _result: ExpressionResult
) -> Verification:
    _, comp, _ = _open_comp(s, p)
    got = _find_input(s, _find_tool(comp, p.tool), p.input).GetExpression()
    ok = (got is None or got == "") if not p.expression else got == p.expression
    return Verification(ok=ok, observed={"expression": got})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SetExpressionParams,
    result=ExpressionResult,
    verify=_verify_expression,
)
def set_expression(s: ResolveSession, p: SetExpressionParams) -> ExpressionResult:
    """Set (or clear, with an empty string) the expression of a tool input."""
    _guard_input_id(p.input)
    _, comp, _ = _open_comp(s, p)
    if p.expression:
        _check_expression(
            p.expression,
            {_tool_name(t): {_input_label(e) for e in _input_objects(t)} for t in _tools(comp)},
        )
    inp = _find_input(s, _find_tool(comp, p.tool), p.input)
    with _transaction(comp, f"MCP expression {p.tool}.{p.input}"):
        inp.SetExpression(p.expression or None)
    return ExpressionResult(tool=p.tool, input=p.input, expression=p.expression or None)


class RenameToolParams(_CompParams):
    tool: str
    new_name: str = Field(min_length=1)


def _verify_tool_renamed(
    s: ResolveSession, p: RenameToolParams, _result: ToolResult
) -> Verification:
    _, comp, _ = _open_comp(s, p)
    ok = comp.FindTool(p.new_name) is not None and comp.FindTool(p.tool) is None
    return Verification(ok=ok)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=RenameToolParams,
    result=ToolResult,
    verify=_verify_tool_renamed,
)
def rename_tool(s: ResolveSession, p: RenameToolParams) -> ToolResult:
    """Rename a tool of a comp (expressions referring to the old name keep working in Fusion)."""
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    check_tool_name(p.new_name)
    if comp.FindTool(p.new_name) is not None:
        _fail(f"A tool named {p.new_name!r} already exists.", code=ErrorCode.WRONG_STATE)
    with _transaction(comp, f"MCP rename {p.tool}"):
        if not tool.SetAttrs({"TOOLS_Name": p.new_name}):
            _fail("FusionTool.SetAttrs(TOOLS_Name) returned False.")
    return ToolResult(tool=p.new_name, id=_tool_id(tool))


class ToolEnabledParams(_CompParams):
    tool: str
    enabled: bool = Field(description="False sets the tool to pass-through (bypassed).")


class ToolEnabledResult(BaseModel):
    tool: str
    enabled: bool


def _verify_enabled(
    s: ResolveSession, p: ToolEnabledParams, _result: ToolEnabledResult
) -> Verification:
    _, comp, _ = _open_comp(s, p)
    passthrough = _attrs(_find_tool(comp, p.tool)).get("TOOLB_PassThrough")
    return Verification(
        ok=passthrough is not None and bool(passthrough) is (not p.enabled),
        observed={"passthrough": passthrough},
    )


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=ToolEnabledParams,
    result=ToolEnabledResult,
    verify=_verify_enabled,
    idempotent=True,
)
def set_tool_enabled(s: ResolveSession, p: ToolEnabledParams) -> ToolEnabledResult:
    """Enable a tool or set it to pass-through (TOOLB_PassThrough)."""
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    with _transaction(comp, f"MCP enable {p.tool}"):
        if not tool.SetAttrs({"TOOLB_PassThrough": not p.enabled}):
            _fail("FusionTool.SetAttrs(TOOLB_PassThrough) returned False.")
    return ToolEnabledResult(tool=p.tool, enabled=p.enabled)


# -- settings files, macros, pasted snippets ---------------------------------------


class LoadSettingsParams(_CompParams):
    tool: str
    path: str = Field(description="A .setting file inside the allowed roots.")


class SaveSettingsParams(_CompParams):
    tool: str
    path: str = Field(
        description="Destination .setting file inside the allowed roots. "
        "Saving a Group tool produces a macro."
    )
    overwrite: bool = False


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=LoadSettingsParams,
    result=ToolResult,
    verify_reason="LoadSettings has no generic read-back; the return value is checked and the "
    "change is one undo step.",
)
def load_tool_settings(s: ResolveSession, p: LoadSettingsParams) -> ToolResult:
    """Load a .setting file onto an existing tool (replaces its parameters)."""
    _require_code_execution("load_tool_settings (.setting files may embed scripts)")
    path = check_path(p.path, runtime().allowed_roots, must_exist=True)
    if path.suffix.lower() != ".setting":
        raise DavinciError(ErrorCode.INVALID_PARAMS, "Only .setting files can be loaded.")
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    with _transaction(comp, f"MCP load settings {p.tool}", lock=True):
        if not tool.LoadSettings(str(path)):
            _fail(
                "FusionTool.LoadSettings returned False.", "The file may target another tool type."
            )
    return ToolResult(tool=_tool_name(tool), id=_tool_id(tool))


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=SaveSettingsParams,
    result=PathResult,
    verify=_verify_file,
)
def save_tool_settings(s: ResolveSession, p: SaveSettingsParams) -> PathResult:
    """Save a tool (or Group, as a macro) to a new .setting file."""
    path = check_path(p.path, runtime().allowed_roots)
    if path.suffix.lower() != ".setting":
        raise DavinciError(ErrorCode.INVALID_PARAMS, "The destination must end in .setting.")
    if path.exists() and not p.overwrite:
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"{path} already exists.",
            hint="Pass overwrite=true to replace it.",
        )
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not tool.SaveSettings(str(path)):
        _fail("FusionTool.SaveSettings returned False.")
    return PathResult(path=str(path))


class PasteSettingsParams(_CompParams):
    text: str = Field(max_length=1_000_000, description="Fusion .setting / .comp snippet text.")


class PasteResult(BaseModel):
    new_tools: list[str]


def _verify_pasted(s: ResolveSession, p: PasteSettingsParams, r: PasteResult) -> Verification:
    _, comp, _ = _open_comp(s, p)
    names = {_tool_name(t) for t in _tools(comp)}
    return Verification(ok=bool(r.new_tools) and set(r.new_tools) <= names)


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=PasteSettingsParams,
    result=PasteResult,
    verify=_verify_pasted,
)
def paste_settings(s: ResolveSession, p: PasteSettingsParams) -> PasteResult:
    """Paste a Fusion .setting snippet (text) into a comp; reports the tools it created."""
    _require_code_execution("paste_settings (settings text may embed scripts)")
    _, comp, _ = _open_comp(s, p)
    before = {_tool_name(t) for t in _tools(comp)}
    with _transaction(comp, "MCP paste settings", lock=True):
        comp.Paste(p.text)
    new = [n for n in (_tool_name(t) for t in _tools(comp)) if n not in before]
    if not new:
        _fail(
            "Comp.Paste created no tool.",
            "The snippet must be valid Fusion settings text ('{ Tools = ordered() { ... } }').",
        )
    return PasteResult(new_tools=new)


class TextTitleParams(_CompParams):
    text: str
    name: str | None = None
    font: str | None = Field(default=None, description="Font family, e.g. 'Open Sans'.")
    style: str | None = Field(default=None, description="Font style, e.g. 'Bold'.")
    size: float | None = Field(default=None, gt=0, description="Text+ Size (0-1 of frame height).")
    color: str | None = Field(default=None, description="Face color '#RRGGBB'.")
    x: float | None = None
    y: float | None = None


def _hex_rgb(color: str) -> tuple[float, float, float]:
    if not _HEX_COLOR.match(color):
        raise DavinciError(ErrorCode.INVALID_PARAMS, f"Invalid color {color!r}; use '#RRGGBB'.")
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _title_inputs(p: TextTitleParams) -> dict[str, Any]:
    values: dict[str, Any] = {"StyledText": p.text}
    if p.font is not None:
        values["Font"] = p.font
    if p.style is not None:
        values["Style"] = p.style
    if p.size is not None:
        values["Size"] = p.size
    if p.color is not None:
        values.update(zip(("Red1", "Green1", "Blue1"), _hex_rgb(p.color), strict=True))
    return values


def _verify_title(s: ResolveSession, p: TextTitleParams, r: ToolResult) -> Verification:
    _, comp, _ = _open_comp(s, p)
    tool = comp.FindTool(r.tool)
    if tool is None:
        return Verification(ok=False, observed={"found": False})
    bad = {
        k: got
        for k, v in _title_inputs(p).items()
        if not _same(got := _read_input(tool, k, None), v)
    }
    return Verification(ok=not bad, observed={"mismatches": bad})


@action(
    domain=DOMAIN,
    kind=Kind.WRITE,
    params=TextTitleParams,
    result=ToolResult,
    verify=_verify_title,
)
def create_text_title(s: ResolveSession, p: TextTitleParams) -> ToolResult:
    """Create a Text+ tool with text, font, size and color (connect it to MediaOut to render)."""
    values = _title_inputs(p)
    _, comp, _ = _open_comp(s, p)
    if p.name is not None and comp.FindTool(p.name) is not None:
        _fail(f"A tool named {p.name!r} already exists.", code=ErrorCode.WRONG_STATE)
    with _transaction(comp, "MCP Text+ title", lock=True):
        tool = comp.AddTool(
            "TextPlus", _AUTO_POS if p.x is None else p.x, _AUTO_POS if p.y is None else p.y
        )
        if tool is None:
            _fail("Comp.AddTool('TextPlus') returned nothing.")
        if p.name is not None:
            tool.SetAttrs({"TOOLS_Name": check_tool_name(p.name)})
    # Input edits happen unlocked: SetInput under Comp.Lock is not applied at render.
    with _transaction(comp, "MCP Text+ values"):
        for key, value in values.items():
            tool.SetInput(key, value)
    return ToolResult(tool=_tool_name(tool), id=_tool_id(tool))


# -- Fuses -----------------------------------------------------------------------


class InstallFuseParams(_Params):
    filename: str = Field(description="File name ending in .fuse (no directories).")
    source: str = Field(max_length=500_000, description="Lua source of the Fuse.")
    directory: str | None = Field(
        default=None,
        description="Destination folder. Default: the official user Fuses folder. Any other "
        "folder must be inside the allowed roots.",
    )


def _verify_fuse(_: ResolveSession, p: InstallFuseParams, r: PathResult) -> Verification:
    f = Path(r.path)
    ok = f.is_file() and f.read_text(encoding="utf-8") == p.source
    return Verification(ok=ok, note="Resolve loads Fuses at startup: restart it to use this one.")


def _plan_fuse(_: ResolveSession, p: InstallFuseParams) -> PlanDraft:
    _require_code_execution("install_fuse")
    target = _fuses_root(p.directory) / p.filename
    digest = hashlib.sha256(p.source.encode()).hexdigest()
    return PlanDraft(
        summary=f"Install Fuse {p.filename} (Lua code that Resolve runs at every start).",
        effects=[
            f"Writes {target} ({len(p.source)} chars, sha256 {digest[:16]}).",
            "The Fuse executes with Resolve's privileges once Resolve restarts.",
            *p.source.splitlines()[:20],
        ],
        targets=[str(target)],
        fingerprint=f"{digest}:{target.exists()}",
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=InstallFuseParams,
    result=PathResult,
    verify=_verify_fuse,
    plan=_plan_fuse,
)
def install_fuse(_: ResolveSession, p: InstallFuseParams) -> PathResult:
    """Install a .fuse (Lua run by Resolve): needs code execution enabled + confirmation."""
    _require_code_execution("install_fuse")
    if not _FUSE_NAME.match(p.filename):
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            "filename must be a plain name ending in .fuse (no path separators).",
        )
    root = _fuses_root(p.directory)
    target = root / p.filename
    if target.exists():
        raise DavinciError(
            ErrorCode.WRONG_STATE,
            f"{target} already exists.",
            hint="Choose another file name; installed Fuses are never overwritten.",
        )
    root.mkdir(parents=True, exist_ok=True)
    target.write_text(p.source, encoding="utf-8")
    return PathResult(path=str(target))


# =====================================================================================
# DESTRUCTIVE
# =====================================================================================


class DeleteToolsParams(_CompParams):
    tools: list[str] = Field(min_length=1, max_length=100)


class DeletedTools(BaseModel):
    deleted: list[str]


def _tools_state(comp: ResolveObject, names: list[str]) -> list[dict[str, Any]]:
    return [{"name": n, "id": _tool_id(_find_tool(comp, n))} for n in names]


def _plan_delete_tools(s: ResolveSession, p: DeleteToolsParams) -> PlanDraft:
    _, comp, name = _open_comp(s, p)
    state = _tools_state(comp, p.tools)
    return PlanDraft(
        summary=f"Delete {len(p.tools)} tool(s) from comp {name!r}: {p.tools}.",
        effects=["Removes the tools and their connections (undoable inside Fusion only)."],
        targets=[f"fusion:{name}:{t}" for t in p.tools],
        fingerprint=_fingerprint(state),
    )


def _verify_tools_deleted(
    s: ResolveSession, p: DeleteToolsParams, _result: DeletedTools
) -> Verification:
    _, comp, _ = _open_comp(s, p)
    left = [n for n in p.tools if comp.FindTool(n) is not None]
    return Verification(ok=not left, observed={"still_present": left})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteToolsParams,
    result=DeletedTools,
    plan=_plan_delete_tools,
    verify=_verify_tools_deleted,
)
def delete_tools(s: ResolveSession, p: DeleteToolsParams) -> DeletedTools:
    """Delete tools from a comp (one undo step)."""
    _, comp, _ = _open_comp(s, p)
    targets = [_find_tool(comp, n) for n in p.tools]
    with _transaction(comp, "MCP delete tools", lock=True):
        for tool in targets:
            tool.Delete()
    return DeletedTools(deleted=list(p.tools))


class DeleteCompParams(_Params):
    item: ItemRef
    name: str


class DeletedComp(BaseModel):
    deleted: str
    names: list[str]


def _plan_delete_comp(s: ResolveSession, p: DeleteCompParams) -> PlanDraft:
    item = resolve_item(s, p.item)
    if p.name not in _comp_names(item):
        _not_found(f"No comp named {p.name!r} on the item.")
    comp = item.GetFusionCompByName(p.name)
    names = sorted(_tool_name(t) for t in _tools(comp)) if comp is not None else []
    return PlanDraft(
        summary=f"Delete the Fusion comp {p.name!r} ({len(names)} tools) of {item.GetName()!r}.",
        effects=["The whole composition and its tools are removed."],
        targets=[f"fusion-comp:{item.GetUniqueId()}:{p.name}"],
        fingerprint=_fingerprint({"comp": p.name, "tools": names}),
    )


def _verify_comp_deleted(
    s: ResolveSession, p: DeleteCompParams, _result: DeletedComp
) -> Verification:
    names = _comp_names(resolve_item(s, p.item))
    return Verification(ok=p.name not in names, observed={"names": names})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=DeleteCompParams,
    result=DeletedComp,
    plan=_plan_delete_comp,
    verify=_verify_comp_deleted,
)
def delete_comp(s: ResolveSession, p: DeleteCompParams) -> DeletedComp:
    """Delete a named Fusion composition from a timeline item."""
    item = resolve_item(s, p.item)
    s.caps.require(item, "DeleteFusionCompByName")
    if p.name not in _comp_names(item):
        _not_found(f"No comp named {p.name!r} on the item.")
    if not item.DeleteFusionCompByName(p.name):
        _fail("TimelineItem.DeleteFusionCompByName returned False.")
    return DeletedComp(deleted=p.name, names=_comp_names(item))


class ClearKeyframesParams(_CompParams):
    tool: str
    input: str


class ClearedKeyframes(BaseModel):
    tool: str
    input: str
    kept_value: Any


def _plan_clear(s: ResolveSession, p: ClearKeyframesParams) -> PlanDraft:
    _, comp, name = _open_comp(s, p)
    inp = _find_input(s, _find_tool(comp, p.tool), p.input)
    if not _is_animated(inp):
        _fail(f"{p.tool}.{p.input} is not animated.", code=ErrorCode.WRONG_STATE)
    keys = _keyframes(inp)
    return PlanDraft(
        summary=f"Remove {len(keys)} keyframe(s) from {p.tool}.{p.input} in comp {name!r}.",
        effects=["Drops the animation; the input keeps its current value."],
        targets=[f"fusion:{name}:{p.tool}.{p.input}"],
        fingerprint=_fingerprint(keys),
    )


def _verify_cleared(
    s: ResolveSession, p: ClearKeyframesParams, r: ClearedKeyframes
) -> Verification:
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    animated = _is_animated(_find_input(s, tool, p.input))
    same = _same(_read_input(tool, p.input, None), r.kept_value)
    return Verification(ok=not animated and same, observed={"animated": animated})


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=ClearKeyframesParams,
    result=ClearedKeyframes,
    plan=_plan_clear,
    verify=_verify_cleared,
)
def clear_keyframes(s: ResolveSession, p: ClearKeyframesParams) -> ClearedKeyframes:
    """Remove the animation of an input, keeping its value at the current time."""
    _, comp, _ = _open_comp(s, p)
    tool = _find_tool(comp, p.tool)
    inp = _find_input(s, tool, p.input)
    kept = tool.GetInput(p.input)
    with _transaction(comp, f"MCP clear keyframes {p.tool}.{p.input}"):
        inp.ConnectTo(None)
        tool.SetInput(p.input, kept)
    return ClearedKeyframes(tool=p.tool, input=p.input, kept_value=_json(kept))


ACTIONS = [
    list_comps,
    comp_info,
    tool_inputs,
    registry_types,
    list_fuses,
    add_comp,
    import_comp,
    export_comp,
    load_comp,
    rename_comp,
    add_tool,
    set_inputs,
    connect,
    disconnect,
    set_keyframes,
    set_expression,
    rename_tool,
    set_tool_enabled,
    load_tool_settings,
    save_tool_settings,
    paste_settings,
    create_text_title,
    install_fuse,
    delete_tools,
    delete_comp,
    clear_keyframes,
]
