# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Parse the official ``DaVinciResolveScript.pyi`` stub into frozen dataclasses."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

_ENUM_DOC_RE = re.compile(r"^One of the resolve\.\* constants:\s*(.+)$", re.DOTALL)


@dataclass(frozen=True)
class LiteralAlias:
    name: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class ConstantEnum:
    name: str
    constants: tuple[str, ...]
    doc: str | None = None


@dataclass(frozen=True)
class FieldSpec:
    key: str
    annotation: str
    doc: str | None = None


@dataclass(frozen=True)
class TypedDictSpec:
    name: str
    total: bool
    fields: tuple[FieldSpec, ...]
    doc: str | None = None


@dataclass(frozen=True)
class ParamSpec:
    name: str
    annotation: str | None = None
    default: str | None = None


@dataclass(frozen=True)
class MethodSpec:
    name: str
    params: tuple[ParamSpec, ...] = ()
    returns: str | None = None
    doc: str | None = None


@dataclass(frozen=True)
class ClassSpec:
    name: str
    doc: str | None = None
    constants: dict[str, str] = field(default_factory=lambda: {})
    methods: tuple[MethodSpec, ...] = ()


@dataclass(frozen=True)
class StubModel:
    literals: tuple[LiteralAlias, ...] = ()
    constant_enums: tuple[ConstantEnum, ...] = ()
    typed_dicts: tuple[TypedDictSpec, ...] = ()
    classes: tuple[ClassSpec, ...] = ()

    def literal(self, name: str) -> LiteralAlias | None:
        return next((x for x in self.literals if x.name == name), None)

    def constant_enum(self, name: str) -> ConstantEnum | None:
        return next((x for x in self.constant_enums if x.name == name), None)

    def typed_dict(self, name: str) -> TypedDictSpec | None:
        return next((x for x in self.typed_dicts if x.name == name), None)

    def cls(self, name: str) -> ClassSpec | None:
        return next((x for x in self.classes if x.name == name), None)


def _comments(source: str) -> dict[int, str]:
    """Map source line number -> trailing ``#`` comment text."""
    out: dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            out[tok.start[0]] = tok.string.lstrip("#").strip()
    return out


def _str_const(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        value = node.value.value
        if isinstance(value, str):
            return value.strip()
    return None


def _literal_values(node: ast.expr) -> tuple[str, ...] | None:
    if not (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Literal"
    ):
        return None
    sl = node.slice
    elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
    return tuple(str(e.value) for e in elts if isinstance(e, ast.Constant))


def _is_typed_dict_class(node: ast.ClassDef) -> bool:
    return any(isinstance(b, ast.Name) and b.id == "TypedDict" for b in node.bases)


def _total(keywords: list[ast.keyword]) -> bool:
    for kw in keywords:
        if kw.arg == "total" and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return True


def _typed_dict_class(node: ast.ClassDef) -> TypedDictSpec:
    fields: list[FieldSpec] = []
    body = node.body
    for i, stmt in enumerate(body):
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            doc = _str_const(body[i + 1]) if i + 1 < len(body) else None
            fields.append(FieldSpec(stmt.target.id, ast.unparse(stmt.annotation), doc))
    return TypedDictSpec(node.name, _total(node.keywords), tuple(fields), ast.get_docstring(node))


def _typed_dict_call(name: str, call: ast.Call, comments: dict[int, str]) -> TypedDictSpec | None:
    if len(call.args) < 2 or not isinstance(call.args[1], ast.Dict):  # noqa: PLR2004
        return None
    fields: list[FieldSpec] = []
    for key, value in zip(call.args[1].keys, call.args[1].values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            fields.append(
                FieldSpec(key.value, ast.unparse(value), comments.get(value.end_lineno or 0))
            )
    return TypedDictSpec(name, _total(call.keywords), tuple(fields), None)


def _method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> MethodSpec:
    a = node.args
    positional = [*a.posonlyargs, *a.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(a.defaults))
    defaults.extend(a.defaults)
    params: list[ParamSpec] = []
    for arg, default in zip(positional, defaults, strict=True):
        if arg.arg in ("self", "cls"):
            continue
        params.append(
            ParamSpec(
                arg.arg,
                ast.unparse(arg.annotation) if arg.annotation else None,
                ast.unparse(default) if default else None,
            )
        )
    if a.vararg:
        ann = ast.unparse(a.vararg.annotation) if a.vararg.annotation else None
        params.append(ParamSpec(f"*{a.vararg.arg}", ann))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        params.append(
            ParamSpec(
                arg.arg,
                ast.unparse(arg.annotation) if arg.annotation else None,
                ast.unparse(default) if default else None,
            )
        )
    if a.kwarg:
        ann = ast.unparse(a.kwarg.annotation) if a.kwarg.annotation else None
        params.append(ParamSpec(f"**{a.kwarg.arg}", ann))
    returns = ast.unparse(node.returns) if node.returns else None
    return MethodSpec(node.name, tuple(params), returns, ast.get_docstring(node))


def _class(node: ast.ClassDef) -> ClassSpec:
    constants: dict[str, str] = {}
    methods: list[MethodSpec] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if not stmt.target.id.startswith("_"):
                constants[stmt.target.id] = ast.unparse(stmt.annotation)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and not stmt.name.startswith(
            "_"
        ):
            methods.append(_method(stmt))
    return ClassSpec(node.name, ast.get_docstring(node), constants, tuple(methods))


def parse_stub(path: Path) -> StubModel:
    """Parse a ``.pyi`` stub file into a :class:`StubModel`."""
    source = path.read_text(encoding="utf-8")
    comments = _comments(source)
    tree = ast.parse(source)
    literals: list[LiteralAlias] = []
    enums: list[ConstantEnum] = []
    tds: list[TypedDictSpec] = []
    classes: list[ClassSpec] = []
    body = tree.body
    for i, stmt in enumerate(body):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if not isinstance(target, ast.Name):
                continue
            values = _literal_values(stmt.value)
            if values is not None:
                literals.append(LiteralAlias(target.id, values))
            elif (
                isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "TypedDict"
            ):
                spec = _typed_dict_call(target.id, stmt.value, comments)
                if spec:
                    tds.append(spec)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and isinstance(stmt.annotation, ast.Name)
            and stmt.annotation.id == "TypeAlias"
        ):
            doc = _str_const(body[i + 1]) if i + 1 < len(body) else None
            m = _ENUM_DOC_RE.match(doc) if doc else None
            if m:
                consts = tuple(c.strip() for c in m.group(1).split(",") if c.strip())
                enums.append(ConstantEnum(stmt.target.id, consts, doc))
        elif isinstance(stmt, ast.ClassDef):
            if _is_typed_dict_class(stmt):
                tds.append(_typed_dict_class(stmt))
            else:
                classes.append(_class(stmt))
    return StubModel(tuple(literals), tuple(enums), tuple(tds), tuple(classes))
