# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Action registry and generation of one MCP tool per (domain, kind).

Each tool takes ``{"op": {"action": <name>, ...params}, "dry_run"?, "confirm_token"?}``.
``op`` is a discriminated union, so clients get a strict ``oneOf`` schema while
the top level stays a plain object (Claude rejects top-level ``oneOf``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, create_model

from davinci_mcp.actions.spec import ActionSpec, Kind
from davinci_mcp.receipts import Receipt

if TYPE_CHECKING:
    from collections.abc import Iterable


def _camel(text: str) -> str:
    return "".join(part.capitalize() for part in text.split("_"))


class WriteOutput(BaseModel):
    status: Literal["applied", "planned", "started"]
    receipt: Receipt | None = None
    job: dict[str, Any] | None = Field(
        default=None, description="Present when status='started': poll it with job_status."
    )
    plan: dict[str, Any] | None = Field(
        default=None, description="Present when status='planned' (dry run or consent needed)."
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
        return [_build_tool(name, groups[name], compact=compact) for name in sorted(groups)]


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


def _compact_input_schema(kind: Kind, specs: list[ActionSpec]) -> dict[str, Any]:
    props: dict[str, Any] = {
        "op": {
            "type": "object",
            "description": "{action, ...params}. Parameters per action: see the description "
            "or describe_action.",
            "properties": {"action": {"type": "string", "enum": [s.name for s in specs]}},
            "required": ["action"],
            "additionalProperties": True,
        }
    }
    if kind is not Kind.READ:
        props["dry_run"] = {"type": "boolean", "default": False}
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


def _build_tool(name: str, specs: list[ActionSpec], *, compact: bool = True) -> ToolDefinition:
    kind = specs[0].kind
    domain = specs[0].domain
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
        f"- {s.name}{signature(op_by_name[s.name]) if compact else ''}: {s.summary}"
        + (" [job]" if s.long_running else "")
        for s in specs
    ]
    if kind is Kind.DESTRUCTIVE:
        lines.append(
            "First call returns a plan + confirm_token; repeat it with confirm_token to apply."
        )
    return ToolDefinition(
        name=name,
        domain=domain,
        kind=kind,
        description="\n".join(lines),
        actions={s.name: s for s in specs},
        call_adapter=adapter,
        input_schema=_compact_input_schema(kind, specs) if compact else adapter.json_schema(),
        output_schema=(
            output_model.model_json_schema()
            if not compact
            else _COMPACT_READ_OUTPUT
            if kind is Kind.READ
            else _COMPACT_WRITE_OUTPUT
        ),
        op_models=op_by_name,
    )
