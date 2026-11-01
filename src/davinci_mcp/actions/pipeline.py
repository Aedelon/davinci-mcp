# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Execution pipeline shared by every generated tool.

validate → gate → (plan / consent) → [page guard → snapshot → capture → handler
→ verify] on the executor → receipt → cache bump → audit.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from mcp.types import ContentBlock, TextContent
from pydantic import BaseModel, ValidationError

from davinci_mcp.actions.registry import WriteOutput, compact_schema, shape
from davinci_mcp.actions.repair import repair, unknown_keys
from davinci_mcp.actions.spec import JobRequest, Kind, PlanDraft, SnapshotKind
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.images import image_blocks
from davinci_mcp.jobs import default_interpret
from davinci_mcp.receipts import Receipt, Verification
from davinci_mcp.refs import TimelineRef, resolve_timeline
from davinci_mcp.safety.pages import PageGuard

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult

    from davinci_mcp.actions.registry import ToolDefinition
    from davinci_mcp.actions.spec import ActionSpec
    from davinci_mcp.audit import AuditLog
    from davinci_mcp.cache import ReadCache
    from davinci_mcp.connection.manager import ConnectionManager, ResolveSession
    from davinci_mcp.jobs import Interpreter, JobManager
    from davinci_mcp.safety.consent import ConsentManager
    from davinci_mcp.safety.snapshots import SnapshotManager


def tool_result(
    structured: dict[str, Any],
    *,
    is_error: bool = False,
    extra: Sequence[ContentBlock] = (),
) -> ToolResult:
    from fastmcp.tools import ToolResult  # noqa: PLC0415 - keep import cost off module load

    text = json.dumps(structured, ensure_ascii=False, default=str)
    return ToolResult(
        content=[TextContent(type="text", text=text), *extra],
        structured_content=structured,
        is_error=is_error,
    )


def error_result(err: DavinciError) -> ToolResult:
    return tool_result(err.to_payload().model_dump(mode="json"), is_error=True)


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


@dataclass
class _Applied:
    result: BaseModel
    before: dict[str, Any] | None
    verification: Verification | None
    snapshot_id: str | None


@dataclass
class Pipeline:
    connection: ConnectionManager
    consent: ConsentManager
    snapshots: SnapshotManager
    audit: AuditLog
    cache: ReadCache
    jobs: JobManager | None = None
    auto_snapshot: Literal["destructive", "off", "all"] = "destructive"
    # action name -> tools that define it, to redirect a call made on the wrong tool.
    action_tools: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    async def execute(
        self, tool: ToolDefinition, arguments: dict[str, Any], *, preauthorized: bool = False
    ) -> ToolResult:
        """Run one call. ``preauthorized``: consent was already given (a confirmed recipe)."""
        try:
            return await self._execute(tool, arguments, preauthorized=preauthorized)
        except DavinciError as err:
            return error_result(err)

    def _parse(
        self, tool: ToolDefinition, arguments: dict[str, Any]
    ) -> tuple[ActionSpec, Any, Any, list[str]]:
        notes: list[str] = []
        try:
            call = tool.call_adapter.validate_python(arguments)
        except ValidationError as exc:
            repaired: Any = _repaired(tool, arguments, notes)
            if repaired is not None:
                call = repaired
                op = call.op
                spec = tool.actions[op.action]
                params = spec.params.model_validate(
                    op.model_dump(exclude={"action"}, by_alias=False)
                )
                return spec, call, params, notes
            raw_op: object = arguments.get("op")
            name = (
                cast("dict[str, object]", raw_op).get("action")
                if isinstance(raw_op, dict)
                else None
            )
            details: dict[str, Any] = {"errors": _dedupe(json.loads(exc.json(include_url=False)))}
            if isinstance(name, str) and name in tool.actions:
                details["schema"] = compact_schema(tool.action_schema(name))
                hint = f"{name}{shape(tool.op_models[name])}"
                unknown = unknown_keys(tool.op_models[name], cast("dict[str, Any]", raw_op))
                hint += "; " + "; ".join(unknown) if unknown else ""
                hint += "; full schema in details.schema."
            elif isinstance(name, str) and (elsewhere := self.action_tools.get(name)):
                hint = f"{name} is an action of {' / '.join(elsewhere)}, not {tool.name}."
            else:
                hint = f"op.action must be one of: {', '.join(tool.actions)}."
            raise DavinciError(
                ErrorCode.INVALID_PARAMS,
                f"Invalid arguments for {tool.name}.",
                hint=hint,
                retryable=False,
                details=details,
            ) from None
        op = call.op
        spec = tool.actions[op.action]
        params = spec.params.model_validate(op.model_dump(exclude={"action"}, by_alias=False))
        return spec, call, params, notes

    async def _execute(
        self, tool: ToolDefinition, arguments: dict[str, Any], *, preauthorized: bool
    ) -> ToolResult:
        spec, call, params, notes = self._parse(tool, arguments)
        session = await self.connection.session()
        self._gate(spec, session)
        started = time.monotonic()

        if spec.kind is Kind.READ:
            result = await self.connection.call(
                lambda s: spec.handler(s, params), retry_safe=True, timeout=spec.timeout_s
            )
            output: dict[str, Any] = {
                "action": spec.name,
                "result": _dump(result),
                "channel": session.caps.channel,
            }
            if notes:
                output["notes"] = notes
            return tool_result(output, extra=image_blocks(result))

        dry_run: bool = call.dry_run
        params_json = params.model_dump(mode="json")
        if dry_run or (spec.kind is Kind.DESTRUCTIVE and not preauthorized):
            draft = await self._plan(spec, params)
            token: str | None = getattr(call, "confirm_token", None)
            if dry_run or token is None:
                plan = self.consent.issue(
                    spec.qualified_name,
                    params_json,
                    draft.fingerprint,
                    summary=draft.summary,
                    effects=draft.effects,
                    targets=draft.targets,
                )
                return tool_result(
                    WriteOutput(
                        status="planned", plan=plan.model_dump(mode="json"), notes=notes or None
                    ).model_dump(mode="json", exclude_none=True)
                )
            self.consent.verify(token, spec.qualified_name, params_json, draft.fingerprint)

        if spec.long_running:
            return await self._start_job(spec, params, params_json, session, started, notes=notes)

        snapshot_kind = self._snapshot_kind(spec, getattr(call, "snapshot", None))
        try:
            applied = await self.connection.call(
                lambda s: self._apply(spec, s, params, snapshot_kind),
                retry_safe=spec.retry_safe,
                timeout=spec.timeout_s,
            )
        except DavinciError as err:
            self.cache.bump()
            self._audit(spec, params_json, session, started, error=err)
            raise
        self.cache.bump()
        receipt = self._receipt(spec, applied, session, started)
        self._audit(spec, params_json, session, started, receipt=receipt)
        return tool_result(
            WriteOutput(status="applied", receipt=receipt, notes=notes or None).model_dump(
                mode="json", exclude_none=True
            )
        )

    async def _start_job(
        self,
        spec: ActionSpec,
        params: BaseModel,
        params_json: dict[str, Any],
        session: ResolveSession,
        started: float,
        *,
        notes: list[str],
    ) -> ToolResult:
        if self.jobs is None:
            raise DavinciError(ErrorCode.INTERNAL, "No job manager configured.", retryable=False)
        request = await self.connection.call(lambda s: spec.handler(s, params), retry_safe=True)
        if not isinstance(request, JobRequest):
            raise DavinciError(
                ErrorCode.INTERNAL, f"{spec.qualified_name} did not return a JobRequest."
            )
        interpret = cast("Interpreter", request.interpret or default_interpret)
        try:
            job = await self.jobs.start(
                request.kind,
                request.label,
                request.launch,
                request.poll,
                cancel=request.cancel,
                interpret=interpret,
            )
        finally:
            self.cache.bump()
        self.audit.record(
            {
                "action": spec.qualified_name,
                "kind": spec.kind.value,
                "params": params_json,
                "channel": session.caps.channel,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "job_id": job.id,
            }
        )
        return tool_result(
            WriteOutput(
                status="started", job=job.model_dump(mode="json"), notes=notes or None
            ).model_dump(mode="json", exclude_none=True)
        )

    @staticmethod
    def _gate(spec: ActionSpec, session: ResolveSession) -> None:
        caps = session.caps
        if spec.since is not None and not caps.at_least(spec.since):
            raise DavinciError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                f"{spec.qualified_name} requires Resolve {'.'.join(map(str, spec.since))}+ "
                f"(connected: {caps.version_string}).",
                retryable=False,
            )
        if spec.studio_only:
            caps.require_studio(spec.qualified_name)

    async def _plan(self, spec: ActionSpec, params: BaseModel) -> PlanDraft:
        if spec.plan is not None:
            planner = spec.plan
            return await self.connection.call(lambda s: planner(s, params), retry_safe=True)
        return PlanDraft(
            summary=spec.summary,
            effects=[f"Would run {spec.qualified_name}."],
            targets=[],
            fingerprint="",
        )

    def _snapshot_kind(self, spec: ActionSpec, requested: bool | None) -> SnapshotKind | None:
        """Explicit request wins; otherwise the auto_snapshot policy decides."""
        if requested is False:
            return None
        if requested is True:
            return spec.snapshot or "timeline"
        if spec.snapshot is None or self.auto_snapshot == "off":
            return None
        if self.auto_snapshot == "destructive" and spec.kind is not Kind.DESTRUCTIVE:
            return None
        return spec.snapshot

    def _apply(
        self,
        spec: ActionSpec,
        session: ResolveSession,
        params: BaseModel,
        snapshot_kind: SnapshotKind | None,
    ) -> _Applied:
        with PageGuard(session.resolve, spec.page):
            snapshot_id: str | None = None
            if snapshot_kind == "timeline":
                # Copy the timeline the action targets, not merely the current one.
                ref = getattr(params, "timeline", None)
                target = resolve_timeline(session, ref if isinstance(ref, TimelineRef) else None)
                snapshot_id = self.snapshots.snapshot_timeline(
                    session, target, spec.qualified_name
                ).id
            elif snapshot_kind == "project":
                snapshot_id = self.snapshots.snapshot_project(session, spec.qualified_name).id
            before = spec.capture(session, params) if spec.capture else None
            result = spec.handler(session, params)
            verification = spec.verify(session, params, result) if spec.verify else None
        return _Applied(result, before, verification, snapshot_id)

    @staticmethod
    def _receipt(
        spec: ActionSpec, applied: _Applied, session: ResolveSession, started: float
    ) -> Receipt:
        verification = applied.verification
        warnings: list[str] = []
        if verification is not None and not verification.ok:
            warnings.append(
                f"{ErrorCode.VERIFICATION_FAILED}: Resolve reported success but the read-back "
                "disagrees." + (f" {verification.note}" if verification.note else "")
            )
        return Receipt(
            action=spec.qualified_name,
            kind=spec.kind.value,
            result=_dump(applied.result),
            before=applied.before,
            verified=None if verification is None else verification.ok,
            observed={} if verification is None else verification.observed,
            verify_note=spec.verify_reason if verification is None else verification.note,
            snapshot_id=applied.snapshot_id,
            channel=session.caps.channel,
            duration_ms=int((time.monotonic() - started) * 1000),
            warnings=warnings,
        )

    def _audit(
        self,
        spec: ActionSpec,
        params: dict[str, Any],
        session: ResolveSession,
        started: float,
        *,
        receipt: Receipt | None = None,
        error: DavinciError | None = None,
    ) -> None:
        self.audit.record(
            {
                "action": spec.qualified_name,
                "kind": spec.kind.value,
                "params": params,
                "channel": session.caps.channel,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "verified": None if receipt is None else receipt.verified,
                "snapshot_id": None if receipt is None else receipt.snapshot_id,
                "error": None if error is None else error.to_payload().model_dump(mode="json"),
            }
        )


def _repaired(
    tool: ToolDefinition, arguments: dict[str, Any], notes: list[str]
) -> BaseModel | None:
    """Validated call after repairing near-miss op parameters, or None (see repair.py)."""
    raw_op: object = arguments.get("op")
    if not isinstance(raw_op, dict):
        return None
    op = cast("dict[str, Any]", raw_op)
    name = op.get("action")
    if not isinstance(name, str) or name not in tool.op_models:
        return None
    found: list[str] = []
    fixed = repair(tool.op_models[name], op, found)
    if not found:
        return None
    try:
        call = tool.call_adapter.validate_python({**arguments, "op": fixed})
    except ValidationError:
        return None
    notes.extend(found)
    return call


def _dedupe(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per distinct error: the same wrong value on 8 markers is one message."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for err in errors:
        key = (str(err.get("type")), str(err.get("msg")))
        if key in seen:
            seen[key]["count"] = seen[key].get("count", 1) + 1
            continue
        seen[key] = {k: err[k] for k in ("loc", "msg", "input") if k in err}
    return list(seen.values())
