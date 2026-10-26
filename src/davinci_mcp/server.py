# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""FastMCP server assembled from the action registry plus a few pivot tools."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, PrivateAttr

from davinci_mcp import __version__
from davinci_mcp.actions.pipeline import Pipeline, error_result, tool_result
from davinci_mcp.actions.registry import ActionRegistry, ToolDefinition
from davinci_mcp.actions.spec import ActionSpec
from davinci_mcp.api.surface import SearchHit, load_surface, search
from davinci_mcp.audit import AuditLog
from davinci_mcp.cache import ReadCache
from davinci_mcp.capabilities import CapabilityInfo
from davinci_mcp.config import Settings, load_settings
from davinci_mcp.connection.backend import Backend
from davinci_mcp.connection.bridge import BridgeBackend
from davinci_mcp.connection.external import ExternalBackend, find_resolve_process, resolve_paths
from davinci_mcp.connection.manager import ConnectionManager, ProcessProbe, ResolveSession
from davinci_mcp.domains import all_actions
from davinci_mcp.domains._runtime import Runtime
from davinci_mcp.domains._runtime import bind as bind_runtime
from davinci_mcp.errors import DavinciError, ErrorCode, ErrorPayload
from davinci_mcp.executor import ResolveExecutor
from davinci_mcp.jobs import JobInfo, JobManager
from davinci_mcp.recipes.engine import RecipeEngine
from davinci_mcp.recipes.loader import load_recipes
from davinci_mcp.recipes.model import RecipeInfo, RecipeRun
from davinci_mcp.safety.consent import ConsentManager
from davinci_mcp.safety.snapshots import SnapshotInfo, SnapshotManager

INSTRUCTIONS = """\
davinci-mcp controls DaVinci Resolve (Studio, or the free edition through the in-app bridge).
Start with resolve_status. Domain tools are named <domain>_read, <domain>_write and
<domain>_delete; pass {"op": {"action": ..., ...params}}. Each tool description lists its
actions with parameter names (* = required); call describe_action(tool, action) for the full
schema before using an unfamiliar action. Most actions also take an optional `timeline`
selector (default: the current timeline). Read tools are side-effect free.
Write tools return a receipt with verified=true|false from a read-back: treat
verified=false as a failure. Use dry_run=true to preview any write, and snapshot=true to
keep a safety copy of the timeline first (destructive actions take one automatically).
<domain>_delete tools first return a plan with a confirm_token; repeat the identical call
with confirm_token to apply. Timelines, items and clips are addressed by id (preferred),
exact name or 1-based index. Long operations return a job id: poll with job_status or
job_wait. search_api finds Resolve scripting API methods when you need the exact name.
"""

ALWAYS_LOAD = {"anthropic/alwaysLoad": True}
_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


@dataclass
class Services:
    settings: Settings
    executor: ResolveExecutor
    connection: ConnectionManager
    consent: ConsentManager
    snapshots: SnapshotManager
    audit: AuditLog
    cache: ReadCache
    jobs: JobManager
    pipeline: Pipeline

    @classmethod
    def create(
        cls,
        settings: Settings,
        backends: Sequence[Backend] | None = None,
        process_probe: ProcessProbe | None = None,
    ) -> Services:
        state = settings.state_dir
        executor = ResolveExecutor(settings.connection.call_timeout_s)
        paths = resolve_paths(settings.connection.script_api, settings.connection.script_lib)
        default_backends: list[Backend] = [
            ExternalBackend(paths),
            BridgeBackend(timeout_s=settings.connection.call_timeout_s),
        ]
        connection = ConnectionManager(
            backends or default_backends,
            executor,
            settings.connection,
            paths,
            process_probe=process_probe or find_resolve_process,
        )
        consent = ConsentManager.from_state_dir(state, ttl_s=settings.safety.consent_ttl_s)
        snapshots = SnapshotManager(
            settings.safety.snapshot_dir, settings.safety.snapshot_retention
        )
        audit = AuditLog(state / "audit.jsonl")
        cache = ReadCache()
        jobs = JobManager(connection, state / "jobs.json")
        pipeline = Pipeline(
            connection, consent, snapshots, audit, cache, jobs, settings.safety.auto_snapshot
        )
        return cls(settings, executor, connection, consent, snapshots, audit, cache, jobs, pipeline)

    async def aclose(self) -> None:
        await self.jobs.aclose()
        self.executor.shutdown()


class RegistryTool(Tool):
    """A tool generated from one (domain, kind) group of actions."""

    _definition: ToolDefinition = PrivateAttr()
    _pipeline: Pipeline = PrivateAttr()

    @classmethod
    def create(cls, definition: ToolDefinition, pipeline: Pipeline) -> RegistryTool:
        tool = cls(
            name=definition.name,
            description=definition.description,
            parameters=definition.input_schema,
            output_schema=definition.output_schema,
            annotations=ToolAnnotations.model_validate(definition.annotations),
        )
        tool._definition = definition
        tool._pipeline = pipeline
        return tool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return await self._pipeline.execute(self._definition, arguments)


class StatusOutput(BaseModel):
    connected: bool
    capabilities: CapabilityInfo | None = None
    project: str | None = None
    timeline: str | None = None
    page: str | None = None
    error: ErrorPayload | None = None


class ActionInfo(BaseModel):
    tool: str
    action: str
    summary: str
    kind: str
    params_schema: dict[str, Any]
    result_schema: dict[str, Any]
    since: str | None = None
    studio_only: bool = False
    long_running: bool = False
    page: str | None = None
    snapshot: str | None = None


class SearchOutput(BaseModel):
    hits: list[SearchHit]


class JobListOutput(BaseModel):
    jobs: list[JobInfo]


class SnapshotListOutput(BaseModel):
    snapshots: list[SnapshotInfo]


def _ok(model: BaseModel) -> ToolResult:
    return tool_result(model.model_dump(mode="json"))


def build_registry(actions: Sequence[ActionSpec] | None = None) -> ActionRegistry:
    registry = ActionRegistry()
    registry.extend(all_actions() if actions is None else actions)
    return registry


def build_server(
    settings: Settings | None = None,
    *,
    backends: Sequence[Backend] | None = None,
    process_probe: ProcessProbe | None = None,
    actions: Sequence[ActionSpec] | None = None,
) -> FastMCP:
    """Build the server. ``actions`` restricts the registry (tests); default: all domains."""
    settings = settings or load_settings()
    services = Services.create(settings, backends, process_probe)
    bind_runtime(Runtime(settings, services.cache, services.snapshots))
    surface = load_surface()

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncGenerator[dict[str, Any]]:
        try:
            yield {}
        finally:
            await services.aclose()

    auth = None
    token = os.environ.get(settings.http.token_env)
    if token:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier  # noqa: PLC0415

        auth = StaticTokenVerifier({token: {"client_id": "davinci-mcp", "scopes": []}})

    mcp = FastMCP(
        "davinci-mcp", version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan, auth=auth
    )

    definitions = build_registry(actions).tool_definitions(
        compact=settings.server.schema_mode == "compact"
    )
    by_name = {d.name: d for d in definitions}
    for definition in definitions:
        mcp.add_tool(RegistryTool.create(definition, services.pipeline))

    @mcp.tool(
        annotations=_READ_ONLY, meta=ALWAYS_LOAD, output_schema=ActionInfo.model_json_schema()
    )
    async def describe_action(
        tool: str = Field(description="Tool name, e.g. 'timeline_write'."),
        action: str = Field(description="op.action value, e.g. 'append_clips'."),
    ) -> ToolResult:
        """Full parameter and result schema of one action, with its flags."""
        definition = by_name.get(tool)
        if definition is None or action not in definition.actions:
            known = sorted(definition.actions) if definition else sorted(by_name)
            return error_result(
                DavinciError(
                    ErrorCode.NOT_FOUND,
                    f"Unknown {'action' if definition else 'tool'}: {tool}.{action}.",
                    hint=f"Known: {', '.join(known)}",
                    retryable=False,
                )
            )
        spec = definition.actions[action]
        return _ok(
            ActionInfo(
                tool=tool,
                action=action,
                summary=spec.summary,
                kind=spec.kind.value,
                params_schema=definition.action_schema(action),
                result_schema=spec.result.model_json_schema(),
                since=".".join(map(str, spec.since)) if spec.since else None,
                studio_only=spec.studio_only,
                long_running=spec.long_running,
                page=spec.page,
                snapshot=spec.snapshot,
            )
        )

    @mcp.tool(
        annotations=_READ_ONLY,
        meta=ALWAYS_LOAD,
        output_schema=StatusOutput.model_json_schema(),
    )
    async def resolve_status() -> ToolResult:
        """Connection state, Resolve version/edition/channel, current project, timeline and page."""
        try:

            def read(s: ResolveSession) -> StatusOutput:
                project = s.project_manager.GetCurrentProject()
                timeline = project.GetCurrentTimeline() if project is not None else None
                return StatusOutput(
                    connected=True,
                    capabilities=s.caps.info(),
                    project=project.GetName() if project is not None else None,
                    timeline=timeline.GetName() if timeline is not None else None,
                    page=s.resolve.GetCurrentPage(),
                )

            return _ok(await services.connection.call(read, retry_safe=True))
        except DavinciError as err:
            return _ok(StatusOutput(connected=False, error=err.to_payload()))

    @mcp.tool(
        annotations=_READ_ONLY, meta=ALWAYS_LOAD, output_schema=SearchOutput.model_json_schema()
    )
    async def search_api(
        pattern: str = Field(description="Substring of a class, method or description."),
        limit: int = Field(default=20, ge=1, le=100),
    ) -> ToolResult:
        """Search the Resolve scripting API (official 21.1 stub) by name or description."""
        return _ok(SearchOutput(hits=search(surface, pattern, limit)))

    _add_job_and_snapshot_tools(mcp, services)
    _add_recipe_tools(mcp, services, settings, by_name)
    return mcp


class RecipeListOutput(BaseModel):
    recipes: list[RecipeInfo]
    load_errors: list[str]


def _add_recipe_tools(
    mcp: FastMCP, services: Services, settings: Settings, tools: dict[str, ToolDefinition]
) -> None:
    loaded = load_recipes(settings.recipes.dirs, allow_python=settings.recipes.allow_python)
    engine = RecipeEngine(
        services.pipeline,
        tools=tools,
        consent=services.consent,
        jobs=services.jobs,
        recipes=loaded.recipes,
        sources=loaded.sources,
    )

    @mcp.tool(annotations=_READ_ONLY, output_schema=RecipeListOutput.model_json_schema())
    async def recipe_list() -> ToolResult:
        """Workflows that run several actions in one call (built-in and user recipes)."""
        return _ok(RecipeListOutput(recipes=engine.infos(), load_errors=loaded.errors))

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False),
        output_schema=RecipeRun.model_json_schema(),
    )
    async def recipe_run(
        name: str = Field(description="Recipe name from recipe_list."),
        params: Annotated[dict[str, Any] | None, Field(description="Recipe parameters.")] = None,
        dry_run: bool = Field(default=False, description="Plan only."),
        confirm_token: str | None = Field(
            default=None, description="For destructive recipes: token from the planned call."
        ),
    ) -> ToolResult:
        """Run a recipe server-side: every step is validated, verified and audited.

        Stops at the first failed or unverified step and reports each step's outcome.
        Destructive recipes first return a plan with a confirm_token.
        """
        try:
            run = await engine.run(name, params or {}, dry_run=dry_run, confirm_token=confirm_token)
        except DavinciError as err:
            return error_result(err)
        return _ok(run)


def _add_job_and_snapshot_tools(mcp: FastMCP, services: Services) -> None:
    @mcp.tool(annotations=_READ_ONLY, output_schema=JobInfo.model_json_schema())
    async def job_status(job_id: str) -> ToolResult:
        """Current state and progress of a long-running job."""
        try:
            return _ok(services.jobs.status(job_id))
        except DavinciError as err:
            return error_result(err)

    @mcp.tool(annotations=_READ_ONLY, output_schema=JobInfo.model_json_schema())
    async def job_wait(
        job_id: str, timeout_s: float = Field(default=60.0, gt=0, le=600)
    ) -> ToolResult:
        """Wait for a job to finish (returns it still running when the timeout elapses)."""
        try:
            return _ok(await services.jobs.wait(job_id, timeout_s, None))
        except DavinciError as err:
            return error_result(err)

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False),
        output_schema=JobInfo.model_json_schema(),
    )
    async def job_cancel(job_id: str) -> ToolResult:
        """Cancel a running job when the operation supports it."""
        try:
            return _ok(await services.jobs.cancel(job_id))
        except DavinciError as err:
            return error_result(err)

    @mcp.tool(annotations=_READ_ONLY, output_schema=JobListOutput.model_json_schema())
    async def job_list() -> ToolResult:
        """All jobs known to this server, including finished ones."""
        return _ok(JobListOutput(jobs=services.jobs.list()))

    @mcp.tool(annotations=_READ_ONLY, output_schema=SnapshotListOutput.model_json_schema())
    async def snapshot_list(project: str | None = None) -> ToolResult:
        """Safety snapshots taken by this server (optionally for one project)."""
        return _ok(SnapshotListOutput(snapshots=services.snapshots.list(project)))


def check_http_exposure(settings: Settings) -> None:
    """Refuse to listen beyond loopback without a bearer token."""
    loopback = settings.http.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not os.environ.get(settings.http.token_env):
        raise DavinciError(
            ErrorCode.INVALID_PARAMS,
            f"Refusing to listen on {settings.http.host} without ${settings.http.token_env}.",
            retryable=False,
        )
