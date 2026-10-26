# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Generate Pydantic models and an API surface JSON from the official Resolve stub.

Usage: ``uv run python tools/gen_schemas.py [--stub PATH] [--resolve-version X.Y.Z] [--out DIR]``
"""

from __future__ import annotations

import argparse
import ast
import json
import keyword
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from davinci_mcp.api.stub_parser import StubModel, TypedDictSpec, parse_stub

DEFAULT_STUB = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Developer/Scripting/DaVinciResolveScript.pyi"
)
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/davinci_mcp/api/_generated"

_PASSTHROUGH = {"str", "int", "float", "bool", "None", "list", "dict", "tuple", "Any", "bytes"}
_RESERVED = set(dir(BaseModel)) | _PASSTHROUGH | {"Field", "Literal", "ConfigDict", "ApiModel"}
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)*$")


@dataclass(frozen=True)
class _Rendered:
    text: str
    object_class: str | None
    names: frozenset[str]


class _Renderer:
    """Map stub annotations to generated-module annotations."""

    def __init__(self, stub: StubModel) -> None:
        self.models = {t.name for t in stub.typed_dicts}
        self.aliases = {x.name for x in stub.literals} | {x.name for x in stub.constant_enums}
        self.objects = {c.name for c in stub.classes}
        self._objs: list[str] = []
        self._names: set[str] = set()

    def render(self, annotation: str) -> _Rendered:
        self._objs = []
        self._names = set()
        text = self._node(ast.parse(annotation, mode="eval").body)
        if self._objs:
            return _Rendered("Any", self._objs[0], frozenset({"Any"}))
        return _Rendered(text, None, frozenset(self._names))

    def _node(self, node: ast.expr) -> str:  # noqa: PLR0911
        if isinstance(node, ast.Constant):
            if node.value is None:
                return "None"
            if isinstance(node.value, str):
                return self._name(node.value)
            return repr(node.value)
        if isinstance(node, ast.Name):
            return self._name(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return f"{self._node(node.left)} | {self._node(node.right)}"
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            head = node.value.id
            if head == "Literal":
                self._names.add("Literal")
                return ast.unparse(node)
            sl = node.slice
            elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
            return f"{self._name(head)}[{', '.join(self._node(e) for e in elts)}]"
        if isinstance(node, ast.Tuple):
            return ", ".join(self._node(e) for e in node.elts)
        return "Any"

    def _name(self, name: str) -> str:
        if name in self.objects or name in ("Fusion", "FusionComp"):
            self._objs.append(name)
            return "Any"
        if name in self.models or name in self.aliases or name in _PASSTHROUGH:
            self._names.add(name)
            return name
        self._names.add("Any")
        return "Any"


def _py_name(key: str, taken: set[str], referenced: frozenset[str]) -> str:
    name = key
    if not name.isidentifier():
        name = re.sub(r"\W+", "_", name.strip()).strip("_").lower()
        if not name or name[0].isdigit():
            name = f"f_{name}"
    if (
        keyword.iskeyword(name)
        or name in _RESERVED
        or name in referenced
        or name.startswith(("model_", "_"))
    ):
        name = f"{name}_"
    while name in taken:
        name = f"{name}_"
    return name


def _toposort(stub: StubModel) -> list[TypedDictSpec]:
    by_name = {t.name: t for t in stub.typed_dicts}
    done: set[str] = set()
    out: list[TypedDictSpec] = []
    rend = _Renderer(stub)

    def visit(spec: TypedDictSpec, stack: frozenset[str]) -> None:
        if spec.name in done or spec.name in stack:
            return
        for f in spec.fields:
            for dep in rend.render(f.annotation).names:
                if dep in by_name and dep != spec.name:
                    visit(by_name[dep], stack | {spec.name})
        done.add(spec.name)
        out.append(spec)

    for spec in stub.typed_dicts:
        visit(spec, frozenset())
    return out


def _model_source(spec: TypedDictSpec, rend: _Renderer) -> str:
    lines = [f"class {spec.name}(ApiModel):"]
    if spec.doc:
        lines.append(f"    {json.dumps(spec.doc)}")
    taken: set[str] = set()
    for f in spec.fields:
        r = rend.render(f.annotation)
        pyname = _py_name(f.key, taken, r.names)
        taken.add(pyname)
        args: list[str] = []
        if not spec.total:
            args.append("default=None")
        if pyname != f.key:
            args.append(f"alias={json.dumps(f.key)}")
        if f.doc:
            args.append(f"description={json.dumps(f.doc)}")
        if r.object_class:
            args.append(f'json_schema_extra={{"x-resolve-object": {json.dumps(r.object_class)}}}')
        ann = r.text
        if not spec.total and not re.search(r"(^|\| )None( \||$)", ann) and ann != "Any":
            ann = f"{ann} | None"
        line = f"    {pyname}: {ann}"
        if args:
            line += f" = Field({', '.join(args)})"
        lines.append(line)
    if not spec.fields:
        lines.append("    pass")
    return "\n".join(lines)


def render_module(stub: StubModel, resolve_version: str) -> str:
    rend = _Renderer(stub)
    parts: list[str] = [
        f"# GENERATED by tools/gen_schemas.py from DaVinciResolveScript.pyi "
        f"(Resolve {resolve_version}). Do not edit.",
        "from __future__ import annotations\n",
        "from typing import Any, Literal\n",
        "from pydantic import BaseModel, ConfigDict, Field\n",
        f'RESOLVE_VERSION = "{resolve_version}"\n',
    ]
    for lit in stub.literals:
        parts.append(f"{lit.name} = Literal[{', '.join(repr(v) for v in lit.values)}]")
    parts.append("")
    for enum in stub.constant_enums:
        parts.append(f"{enum.name} = Literal[{', '.join(json.dumps(c) for c in enum.constants)}]")
    parts.append("")
    parts.append(
        "class ApiModel(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid", populate_by_name=True, '
        "serialize_by_alias=True)\n"
    )
    ordered = _toposort(stub)
    for spec in ordered:
        parts.append(_model_source(spec, rend) + "\n")
    items = ", ".join(f"{json.dumps(s.name)}: {s.name}" for s in ordered)
    parts.append(f"MODELS: dict[str, type[ApiModel]] = {{{items}}}\n")
    enum_items = ", ".join(
        f"{json.dumps(e.name)}: ({''.join(json.dumps(c) + ', ' for c in e.constants)})"
        for e in stub.constant_enums
    )
    parts.append(f"CONSTANT_ENUMS: dict[str, tuple[str, ...]] = {{{enum_items}}}\n")
    parts.append("for _model in MODELS.values():\n    _model.model_rebuild()\n")
    return "\n".join(parts)


def build_surface(stub: StubModel, resolve_version: str) -> dict[str, object]:
    return {
        "resolve_version": resolve_version,
        "classes": {
            c.name: {
                "doc": c.doc,
                "constants": dict(c.constants),
                "methods": {
                    m.name: {
                        "params": [
                            {"name": p.name, "annotation": p.annotation, "default": p.default}
                            for p in m.params
                        ],
                        "returns": m.returns,
                        "doc": m.doc,
                    }
                    for m in c.methods
                },
            }
            for c in stub.classes
        },
        "typed_dicts": {
            t.name: {
                "total": t.total,
                "fields": {f.key: {"annotation": f.annotation, "doc": f.doc} for f in t.fields},
            }
            for t in stub.typed_dicts
        },
        "literals": {x.name: list(x.values) for x in stub.literals},
        "constant_enums": {x.name: list(x.constants) for x in stub.constant_enums},
    }


def _stem(version: str) -> str:
    parts = version.split(".")
    return f"{parts[0]}_{parts[1] if len(parts) > 1 else '0'}"


def _latest_stem(out: Path) -> str:
    found: list[tuple[int, int]] = []
    for p in out.glob("resolve_*.py"):
        m = re.fullmatch(r"resolve_(\d+)_(\d+)", p.stem)
        if m:
            found.append((int(m.group(1)), int(m.group(2))))
    maj, mnr = max(found)
    return f"resolve_{maj}_{mnr}"


def _default_version(stub_path: Path) -> str:
    changelog = stub_path.parent / "CHANGELOG.md"
    if changelog.is_file():
        for line in changelog.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^##\s+(\S+)", line)
            if m and _VERSION_RE.match(m.group(1)):
                return m.group(1)
    msg = "Cannot infer the Resolve version; pass --resolve-version."
    raise SystemExit(msg)


def generate(stub_path: Path, out: Path, resolve_version: str) -> list[Path]:
    """Write the generated module, surface JSON and package ``__init__`` into ``out``."""
    stub = parse_stub(stub_path)
    out.mkdir(parents=True, exist_ok=True)
    stem = _stem(resolve_version)
    module = out / f"resolve_{stem}.py"
    surface = out / f"api_surface_{stem}.json"
    module.write_text(render_module(stub, resolve_version), encoding="utf-8")
    surface.write_text(
        json.dumps(build_surface(stub, resolve_version), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    latest = _latest_stem(out)
    init = out / "__init__.py"
    init.write_text(
        "# GENERATED by tools/gen_schemas.py. Do not edit.\n"
        '"""Generated Resolve API models (latest supported Resolve version)."""\n\n'
        "from __future__ import annotations\n\n"
        "from pathlib import Path\n\n"
        f"from davinci_mcp.api._generated.{latest} import *  # noqa: F403\n\n"
        "LATEST_SURFACE_PATH: Path = Path(__file__).parent / "
        f'"api_surface_{latest.removeprefix("resolve_")}.json"\n',
        encoding="utf-8",
    )
    written = [module, surface, init]
    ruff = shutil.which("ruff")
    if ruff:
        py_files = [str(p) for p in written if p.suffix == ".py"]
        subprocess.run([ruff, "format", "--quiet", *py_files], check=False)  # noqa: S603
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stub", type=Path, default=DEFAULT_STUB)
    ap.add_argument("--resolve-version", default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ns = ap.parse_args(argv)
    stub_path: Path = ns.stub
    version: str = ns.resolve_version or _default_version(stub_path)
    written = generate(stub_path, ns.out, version)
    sys.stdout.write("\n".join(str(p) for p in written) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
