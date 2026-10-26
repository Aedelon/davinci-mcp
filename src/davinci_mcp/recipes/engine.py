# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Run recipes server-side: every step goes through the normal action pipeline.

One MCP call executes the whole chain; each step still gets parameter validation,
read-back verification, snapshots per policy and an audit entry. A destructive
recipe is confirmed once as a whole (plan → confirm_token); its inner destructive
steps then run pre-authorized.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from davinci_mcp.actions.spec import Kind
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.recipes.model import (
    Recipe,
    RecipeInfo,
    RecipeRun,
    StepOutcome,
    StepSpec,
    YamlRecipe,
)
from davinci_mcp.recipes.templating import render

if TYPE_CHECKING:
    from davinci_mcp.actions.pipeline import Pipeline
    from davinci_mcp.actions.registry import ToolDefinition
    from davinci_mcp.jobs import JobManager
    from davinci_mcp.safety.consent import ConsentManager

DEFAULT_JOB_WAIT_S = 1800.0


def compact_output(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """What an agent needs from a step: status, verified, result, plus warnings/notes/job."""
    if not data:
        return data
    out: dict[str, Any] = {k: data[k] for k in ("status", "notes", "job", "plan") if data.get(k)}
    receipt = data.get("receipt")
    if isinstance(receipt, dict):
        r = cast("dict[str, Any]", receipt)
        out["verified"] = r.get("verified")
        out["result"] = r.get("result")
        out.update({k: r[k] for k in ("warnings", "snapshot_id") if r.get(k)})
    elif "result" in data:
        out["result"] = data["result"]
    return out


class RecipeStepError(Exception):
    def __init__(self, outcome: StepOutcome) -> None:
        super().__init__(outcome.error)
        self.outcome = outcome


@dataclass
class RecipeContext:
    """Handed to Python recipes: ``await ctx.call("timeline_write", {"action": ...})``."""

    engine: RecipeEngine
    outcomes: list[StepOutcome]
    confirmed: bool = False

    async def call(
        self,
        tool: str,
        op: dict[str, Any],
        *,
        snapshot: bool | None = None,
        wait: bool = True,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        outcome = await self.engine.run_step(
            step_id or f"step{len(self.outcomes) + 1}",
            tool,
            op,
            snapshot=snapshot,
            wait=wait,
            preauthorized=self.confirmed,
        )
        self.outcomes.append(outcome)
        if outcome.status != "ok":
            raise RecipeStepError(outcome)
        return outcome.output or {}


class RecipeEngine:
    def __init__(
        self,
        pipeline: Pipeline,
        *,
        tools: dict[str, ToolDefinition],
        consent: ConsentManager,
        jobs: JobManager | None,
        recipes: dict[str, Recipe],
        sources: dict[str, str],
    ) -> None:
        self._pipeline = pipeline
        self._tools = tools
        self._consent = consent
        self._jobs = jobs
        self._recipes = recipes
        self._sources = sources

    def is_destructive(self, recipe: Recipe) -> bool:
        """A YAML recipe with any destructive step is destructive, whatever it declares."""
        if recipe.destructive or not isinstance(recipe, YamlRecipe):
            return recipe.destructive
        return any(
            (d := self._tools.get(step.tool)) is not None and d.kind is Kind.DESTRUCTIVE
            for step in recipe.steps
        )

    # -- discovery ----------------------------------------------------------------

    def infos(self) -> list[RecipeInfo]:
        return [self.info(name) for name in sorted(self._recipes)]

    def info(self, name: str) -> RecipeInfo:
        recipe = self._get(name)
        if isinstance(recipe, YamlRecipe):
            return RecipeInfo(
                name=recipe.name,
                description=recipe.description,
                kind="yaml",
                source=self._sources[name],
                destructive=self.is_destructive(recipe),
                params_schema=recipe.params_model().model_json_schema(),
                steps=[f"{s.tool}.{s.op.get('action', '?')}" for s in recipe.steps],
            )
        return RecipeInfo(
            name=recipe.name,
            description=recipe.description,
            kind="python",
            source=self._sources[name],
            destructive=recipe.destructive,
            params_schema=recipe.params.model_json_schema(),
        )

    def _get(self, name: str) -> Recipe:
        recipe = self._recipes.get(name)
        if recipe is None:
            raise DavinciError(
                ErrorCode.NOT_FOUND,
                f"No recipe named {name!r}.",
                hint=f"Known recipes: {', '.join(sorted(self._recipes)) or 'none'}.",
                retryable=False,
            )
        return recipe

    # -- running ------------------------------------------------------------------

    async def run(
        self,
        name: str,
        params: dict[str, Any],
        *,
        dry_run: bool = False,
        confirm_token: str | None = None,
        verbose: bool = False,
    ) -> RecipeRun:
        return await self._run(self._get(name), params, dry_run, confirm_token, verbose=verbose)

    async def run_batch(
        self,
        steps: list[StepSpec],
        *,
        dry_run: bool = False,
        confirm_token: str | None = None,
        verbose: bool = False,
    ) -> RecipeRun:
        """Run ad-hoc steps like an inline recipe (same validation, consent and reports)."""
        recipe = YamlRecipe(
            name="batch",
            description=f"Batch of {len(steps)} steps.",
            steps=steps,
        )
        return await self._run(recipe, {}, dry_run, confirm_token, verbose=verbose)

    async def _run(
        self,
        recipe: Recipe,
        params: dict[str, Any],
        dry_run: bool,
        confirm_token: str | None,
        *,
        verbose: bool,
    ) -> RecipeRun:
        name = recipe.name
        model = recipe.params_model() if isinstance(recipe, YamlRecipe) else recipe.params
        try:
            validated = model.model_validate(params)
        except ValidationError as exc:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS,
                f"Invalid params for recipe {name!r}.",
                retryable=False,
                details={
                    "errors": json.loads(exc.json(include_url=False)),
                    "schema": model.model_json_schema(),
                },
            ) from None
        values = validated.model_dump(mode="json")

        destructive = self.is_destructive(recipe)
        if dry_run or (destructive and confirm_token is None):
            return self._plan(recipe, values)
        confirmed = False
        if destructive:
            assert confirm_token is not None
            self._consent.verify(confirm_token, f"recipe.{name}", values, self._fingerprint(recipe))
            confirmed = True

        started = time.monotonic()
        outcomes: list[StepOutcome] = []
        status: str = "completed"
        outputs: dict[str, Any] = {}
        try:
            if isinstance(recipe, YamlRecipe):
                context: dict[str, Any] = {"params": values, "steps": {}}
                for step in recipe.steps:
                    op = cast("dict[str, Any]", render(step.op, context))
                    outcome = await self.run_step(
                        step.id,
                        step.tool,
                        op,
                        snapshot=step.snapshot,
                        wait=step.wait,
                        preauthorized=confirmed,
                    )
                    outcomes.append(outcome)
                    if outcome.status != "ok":
                        status = "failed"
                        break
                    context["steps"][step.id] = outcome.output or {}
                outputs = context["steps"]
            else:
                ctx = RecipeContext(self, outcomes, confirmed=confirmed)
                outputs = await recipe.run(ctx, validated)
        except RecipeStepError:
            status = "failed"
        except DavinciError as err:  # template or parameter errors inside a step
            status = "failed"
            outcomes.append(
                StepOutcome(
                    id=f"step{len(outcomes) + 1}",
                    tool="recipe",
                    action="render",
                    status="failed",
                    error=err.to_payload().model_dump(mode="json"),
                )
            )
        if not verbose:
            # Each step already carries its result: drop full receipts and the YAML copy
            # of every step output (they doubled the reply and cost tokens on every turn).
            outcomes = [o.model_copy(update={"output": compact_output(o.output)}) for o in outcomes]
            if isinstance(recipe, YamlRecipe):
                outputs = {}
        return RecipeRun(
            recipe=name,
            status="completed" if status == "completed" else "failed",
            steps=outcomes,
            outputs=outputs if status == "completed" else {},
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def run_step(
        self,
        step_id: str,
        tool: str,
        op: dict[str, Any],
        *,
        snapshot: bool | None,
        wait: bool,
        preauthorized: bool,
    ) -> StepOutcome:
        action = str(op.get("action", "?"))
        definition = self._tools.get(tool)
        if definition is None:
            return StepOutcome(
                id=step_id,
                tool=tool,
                action=action,
                status="failed",
                error={"code": "NOT_FOUND", "message": f"No tool named {tool!r}."},
            )
        arguments: dict[str, Any] = {"op": op}
        if snapshot is not None and definition.kind.value != "read":
            arguments["snapshot"] = snapshot
        # Destructive steps run pre-authorized only inside a recipe the user confirmed.
        result = await self._pipeline.execute(definition, arguments, preauthorized=preauthorized)
        data = dict(result.structured_content or {})
        if result.is_error:
            return StepOutcome(id=step_id, tool=tool, action=action, status="failed", error=data)
        if data.get("status") == "planned":
            return StepOutcome(
                id=step_id,
                tool=tool,
                action=action,
                status="failed",
                error={
                    "code": "CONFIRMATION_REQUIRED",
                    "message": "Destructive step in a recipe that was not confirmed.",
                    "hint": "Declare the recipe destructive: true, then confirm the recipe.",
                },
            )
        receipt = data.get("receipt")
        if isinstance(receipt, dict) and cast("dict[str, Any]", receipt).get("verified") is False:
            return StepOutcome(
                id=step_id,
                tool=tool,
                action=action,
                status="failed",
                output=data,
                error={"code": "VERIFICATION_FAILED", "message": "Read-back disagrees."},
            )
        if data.get("status") == "started" and wait and self._jobs is not None:
            job = cast("dict[str, Any]", data.get("job") or {})
            info = await self._jobs.wait(str(job.get("id")), DEFAULT_JOB_WAIT_S, None)
            data["job"] = info.model_dump(mode="json")
            if info.state != "succeeded":
                return StepOutcome(
                    id=step_id,
                    tool=tool,
                    action=action,
                    status="failed",
                    output=data,
                    error={"code": "API_CALL_FAILED", "message": f"Job ended {info.state}."},
                )
        return StepOutcome(id=step_id, tool=tool, action=action, status="ok", output=data)

    # -- consent ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(recipe: Recipe) -> str:
        if isinstance(recipe, YamlRecipe):
            body = json.dumps(recipe.model_dump(mode="json"), sort_keys=True)
        else:
            body = f"python:{recipe.name}"
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def _plan(self, recipe: Recipe, values: dict[str, Any]) -> RecipeRun:
        if isinstance(recipe, YamlRecipe):
            effects = [
                f"{s.id}: {s.tool}.{s.op.get('action', '?')}"
                + (" (snapshot)" if s.snapshot else "")
                for s in recipe.steps
            ]
        else:
            effects = [f"Python recipe {recipe.name}: {recipe.description}"]
        plan = self._consent.issue(
            f"recipe.{recipe.name}",
            values,
            self._fingerprint(recipe),
            summary=recipe.description,
            effects=effects,
            targets=[],
        )
        return RecipeRun(recipe=recipe.name, status="planned", plan=plan.model_dump(mode="json"))
