# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Action registry and generation of one MCP tool per (domain, kind).

Each tool takes ``{"op": {"action": <name>, ...params}, "dry_run"?, "confirm_token"?}``.
``op`` is a discriminated union, so clients get a strict ``oneOf`` schema while
the top level stays a plain object (Claude rejects top-level ``oneOf``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, cast

from pydantic import BaseModel, Field, TypeAdapter, create_model

from davinci_mcp.actions.repair import field_models, is_list_field
from davinci_mcp.actions.spec import ActionSpec, Kind
from davinci_mcp.receipts import Receipt

if TYPE_CHECKING:
    from collections.abc import Iterable


def _camel(text: str) -> str:
    return "".join(part.capitalize() for part in text.split("_"))


SNAPSHOT_HELP = (
    "true: copy the target timeline (or export the project) first; false: never; "
    "omitted: server policy (destructive actions only, by default)."
)


class WriteOutput(BaseModel):
    status: Literal["applied", "planned", "started"]
    receipt: Receipt | None = None
    job: dict[str, Any] | None = Field(
        default=None, description="Present when status='started': poll it with job_status."
    )
    plan: dict[str, Any] | None = Field(
        default=None, description="Present when status='planned' (dry run or consent needed)."
    )
    notes: list[str] | None = Field(
        default=None, description="How near-miss parameters were interpreted, if any."
    )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    domain: str
    kind: Kind
    description: str
    actions: dict[str, ActionSpec]
    call_adapter: TypeAdapter[Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    op_models: dict[str, type[BaseModel]]
    # Tools of composite actions: full schema, and clients load them up front.
    featured: bool = False

    def action_schema(self, action: str) -> dict[str, Any]:
        """Full JSON schema of one action's ``op`` object (used by describe_action)."""
        return self.op_models[action].model_json_schema()

    @property
    def annotations(self) -> dict[str, Any]:
        read = self.kind is Kind.READ
        return {
            "title": f"{_camel(self.domain)} ({self.kind.value})",
            "readOnlyHint": read,
            "destructiveHint": self.kind is Kind.DESTRUCTIVE,
            "idempotentHint": read or all(s.idempotent for s in self.actions.values()),
            "openWorldHint": False,
        }


class ActionRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ActionSpec] = {}

    def add(self, *specs: ActionSpec) -> None:
        for spec in specs:
            if spec.qualified_name in self._specs:
                raise ValueError(f"duplicate action {spec.qualified_name}")
            spec.validate_spec()
            self._specs[spec.qualified_name] = spec

    def extend(self, specs: Iterable[ActionSpec]) -> None:
        self.add(*specs)

    def get(self, qualified_name: str) -> ActionSpec:
        return self._specs[qualified_name]

    @property
    def specs(self) -> list[ActionSpec]:
        return sorted(self._specs.values(), key=lambda s: s.qualified_name)

    def tool_definitions(self, *, compact: bool = True) -> list[ToolDefinition]:
        """Deterministic order: tools sorted by name, actions sorted inside each tool."""
        groups: dict[str, list[ActionSpec]] = defaultdict(list)
        for spec in self.specs:
            groups[spec.tool_name].append(spec)
        shortcuts = _shortcuts(self.specs)
        return [
            _build_tool(name, groups[name], compact=compact, shortcuts=shortcuts.get(name, []))
            for name in sorted(groups)
        ]


def _shortcuts(specs: list[ActionSpec]) -> dict[str, list[str]]:
    """Per tool, one line per composite action that replaces several of its steps."""
    lines: dict[str, list[str]] = defaultdict(list)
    for spec in specs:
        for tool in dict.fromkeys(ref.split(".")[0] for ref in spec.shortcut_for):
            replaced = [r.split(".")[1] for r in spec.shortcut_for if r.startswith(f"{tool}.")]
            lines[tool].append(
                f"- {spec.tool_name}.{spec.name}: {spec.summary} "
                f"One call instead of {', '.join(replaced)}."
            )
    return lines


def _op_model(spec: ActionSpec) -> type[BaseModel]:
    return create_model(
        f"{_camel(spec.domain)}{_camel(spec.name)}Op",
        __base__=spec.params,
        __doc__=spec.summary,
        action=(Literal[spec.name], Field(description=spec.summary)),  # type: ignore[valid-type]
    )


# Discriminator, and the optional "current object" selectors explained in the server
# instructions: listing them on every action would only cost tokens.
_IMPLICIT_PARAMS = frozenset({"action", "timeline"})


def signature(model: type[BaseModel], limit: int = 8) -> str:
    """Compact parameter list: required names marked '*', extra optional ones elided."""
    names = [
        n + ("*" if f.is_required() else "")
        for n, f in model.model_fields.items()
        if n not in _IMPLICIT_PARAMS
    ]
    if len(names) > limit:
        required = [n for n in names if n.endswith("*")]
        optional = [n for n in names if not n.endswith("*")]
        keep = optional[: max(0, limit - len(required))]
        names = [*required, *keep, f"+{len(optional) - len(keep)} more"]
    return f"({', '.join(names)})"


def shape(model: type[BaseModel], limit: int = 8) -> str:
    """Like ``signature``, with the field names of nested objects: ``fades*: {FadeIn, FadeOut}``.

    Used in INVALID_PARAMS hints so a near-miss can be fixed without describe_action.
    """
    parts: list[str] = []
    for name, info in model.model_fields.items():
        if name in _IMPLICIT_PARAMS:
            continue
        label = name + ("*" if info.is_required() else "")
        nested = field_models(info.annotation)
        if len(nested) == 1:
            inner = signature(nested[0], limit)[1:-1].replace("*", "")
            inner = "{" + inner + "}"
            label += f": [{inner}]" if is_list_field(info.annotation) else f": {inner}"
        parts.append(label)
    return f"({', '.join(parts)})"


def _compact_input_schema(kind: Kind, specs: list[ActionSpec]) -> dict[str, Any]:
    props: dict[str, Any] = {
        "op": {
            "type": "object",
            "description": "{action, ...params}. Parameters per action: see the description "
            "or this server's describe_action tool.",
            "properties": {"action": {"type": "string", "enum": [s.name for s in specs]}},
            "required": ["action"],
            "additionalProperties": True,
        }
    }
    if kind is not Kind.READ:
        props["dry_run"] = {"type": "boolean", "default": False}
        props["snapshot"] = {"type": ["boolean", "null"], "default": None}
    if kind is Kind.DESTRUCTIVE:
        props["confirm_token"] = {"type": ["string", "null"], "default": None}
    return {"type": "object", "properties": props, "required": ["op"]}


_COMPACT_WRITE_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["applied", "planned", "started"]},
        "receipt": {"type": ["object", "null"]},
        "plan": {"type": ["object", "null"]},
        "job": {"type": ["object", "null"]},
    },
    "required": ["status"],
}

_COMPACT_READ_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "result": {"type": "object"},
        "channel": {"type": "string"},
    },
    "required": ["action", "result", "channel"],
}


def _build_tool(
    name: str,
    specs: list[ActionSpec],
    *,
    compact: bool = True,
    shortcuts: list[str] | None = None,
) -> ToolDefinition:
    kind = specs[0].kind
    domain = specs[0].domain
    featured = any(s.shortcut_for for s in specs)
    # Featured tools show their full input schema; outputs stay compact.
    compact_input = compact and not featured
    ops = [_op_model(s) for s in specs]
    op_type: Any = (
        ops[0] if len(ops) == 1 else Annotated[Union[tuple(ops)], Field(discriminator="action")]  # noqa: UP007
    )
    fields: dict[str, Any] = {"op": (op_type, Field(description="The action and its parameters."))}
    if kind is not Kind.READ:
        fields["dry_run"] = (
            bool,
            Field(default=False, description="Describe the effect without applying it."),
        )
        fields["snapshot"] = (
            bool | None,
            Field(default=None, description=SNAPSHOT_HELP),
        )
    if kind is Kind.DESTRUCTIVE:
        fields["confirm_token"] = (
            str | None,
            Field(default=None, description="Token from a previous planned call, to confirm it."),
        )
    call_model = create_model(f"{_camel(name)}Call", **fields)
    adapter: TypeAdapter[Any] = TypeAdapter(call_model)

    if kind is Kind.READ:
        results = tuple(dict.fromkeys(s.result for s in specs))
        result_type: Any = results[0] if len(results) == 1 else Union[results]  # noqa: UP007
        output_model = create_model(
            f"{_camel(name)}Output",
            action=(str, ...),
            result=(result_type, ...),
            channel=(str, ...),
        )
    else:
        output_model = WriteOutput

    op_by_name = {s.name: op for s, op in zip(specs, ops, strict=True)}
    lines = [f"{_camel(domain)} {kind.value}. op.action, (params; * = required):"]
    lines += [
        f"- {s.name}{signature(op_by_name[s.name]) if compact_input else ''}: {s.summary}"
        + (" [job]" if s.long_running else "")
        for s in specs
    ]
    if kind is Kind.DESTRUCTIVE:
        lines.append(
            "First call returns a plan + confirm_token; repeat it with confirm_token to apply."
        )
    if shortcuts:
        lines += ["Shortcuts (prefer them for whole tasks):", *shortcuts]
    if kind is not Kind.READ:
        lines.append(
            "Calls known in advance (a sequence, the same change on several items): ONE batch_run."
        )
    return ToolDefinition(
        name=name,
        domain=domain,
        kind=kind,
        description="\n".join(lines),
        actions={s.name: s for s in specs},
        call_adapter=adapter,
        input_schema=(
            _compact_input_schema(kind, specs) if compact_input else adapter.json_schema()
        ),
        output_schema=(
            output_model.model_json_schema()
            if not compact
            else _COMPACT_READ_OUTPUT
            if kind is Kind.READ
            else _COMPACT_WRITE_OUTPUT
        ),
        op_models=op_by_name,
        featured=featured,
    )


_MAX_REF_DEPTH = 8  # recursive models stop being inlined here


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema reduced to what an agent needs: types, required, enums, descriptions.

    Inlines ``$defs``, drops titles and null defaults, and turns ``anyOf: [X, null]`` into
    X: describe_action replies shrank from ~17k chars to a fraction while staying exact.
    """
    defs: dict[str, Any] = schema.get("$defs", {})

    def walk(node: object, depth: int = 0) -> object:
        if isinstance(node, list):
            return [walk(n, depth) for n in cast("list[Any]", node)]
        if not isinstance(node, dict):
            return node
        n = cast("dict[str, Any]", node)
        ref = n.get("$ref")
        if isinstance(ref, str) and depth < _MAX_REF_DEPTH:
            target = defs.get(ref.rsplit("/", 1)[-1], {})
            return walk({**target, **{k: v for k, v in n.items() if k != "$ref"}}, depth + 1)
        any_of = n.get("anyOf")
        if isinstance(any_of, list):
            kept = [a for a in cast("list[Any]", any_of) if a != {"type": "null"}]
            if len(kept) == 1:
                rest = {k: v for k, v in n.items() if k != "anyOf"}
                return walk({**cast("dict[str, Any]", kept[0]), **rest}, depth)
        out: dict[str, Any] = {}
        for k, v in n.items():
            if k in {"title", "$defs", "additionalProperties"} or (k == "default" and v is None):
                continue
            out[k] = walk(v, depth)
        return out

    return cast("dict[str, Any]", walk(schema))
