# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Recipe definitions (YAML and Python) and run results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

if TYPE_CHECKING:
    from davinci_mcp.recipes.engine import RecipeContext

ParamType = Literal["string", "integer", "number", "boolean", "array", "object"]
_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list[Any],
    "object": dict[str, Any],
}


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ParamType = "string"
    description: str | None = None
    default: Any = None
    required: bool = False


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Step name; later steps read its output as steps.<id>.")
    tool: str = Field(description="Domain tool, e.g. timeline_write.")
    op: dict[str, Any] = Field(description="The op object; strings may use {{ ... }} templates.")
    snapshot: bool | None = None
    wait: bool = Field(default=True, description="Wait for jobs started by this step.")

    @field_validator("id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value.isidentifier():
            raise ValueError("step id must be an identifier")
        return value


class YamlRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    params: dict[str, ParamSpec] = Field(default_factory=dict[str, ParamSpec])
    destructive: bool = Field(
        default=False, description="Ask for a confirm_token before running (plan first)."
    )
    steps: list[StepSpec] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value.isidentifier():
            raise ValueError("recipe name must be an identifier")
        return value

    def params_model(self) -> type[BaseModel]:
        fields: dict[str, Any] = {}
        for key, spec in self.params.items():
            py_type = _PY_TYPES[spec.type]
            if spec.required:
                fields[key] = (py_type, Field(description=spec.description))
            else:
                fields[key] = (
                    py_type | None,
                    Field(default=spec.default, description=spec.description),
                )
        return create_model(f"{self.name}_params", __config__=ConfigDict(extra="forbid"), **fields)


type RecipeRunner = Callable[["RecipeContext", Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PythonRecipe:
    """Declared by a Python recipe module as ``RECIPE = PythonRecipe(...)``."""

    name: str
    description: str
    params: type[BaseModel]
    run: RecipeRunner
    destructive: bool = False


type Recipe = YamlRecipe | PythonRecipe


class RecipeInfo(BaseModel):
    name: str
    description: str
    kind: Literal["yaml", "python"]
    source: str
    destructive: bool
    params_schema: dict[str, Any]
    steps: list[str] = Field(default_factory=list[str], description="tool.action per step (YAML).")


class StepOutcome(BaseModel):
    id: str
    tool: str
    action: str
    status: Literal["ok", "failed", "skipped"]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class RecipeRun(BaseModel):
    recipe: str
    status: Literal["completed", "failed", "planned"]
    steps: list[StepOutcome] = Field(default_factory=list[StepOutcome])
    plan: dict[str, Any] | None = None
    outputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    duration_ms: int = 0
