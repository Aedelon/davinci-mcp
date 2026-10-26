# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Execution pipeline shared by every generated tool.

validate → gate → (plan / consent) → [page guard → snapshot → capture → handler
→ verify] on the executor → receipt → cache bump → audit.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp.types import TextContent
from pydantic import BaseModel, ValidationError

from davinci_mcp.actions.registry import WriteOutput
from davinci_mcp.actions.spec import Kind, PlanDraft
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Receipt, Verification
from davinci_mcp.safety.pages import PageGuard

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult

    from davinci_mcp.actions.registry import ToolDefinition
    from davinci_mcp.actions.spec import ActionSpec
    from davinci_mcp.audit import AuditLog
    from davinci_mcp.cache import ReadCache
    from davinci_mcp.connection.manager import ConnectionManager, ResolveSession
    from davinci_mcp.safety.consent import ConsentManager
    from davinci_mcp.safety.snapshots import SnapshotManager


def tool_result(structured: dict[str, Any], *, is_error: bool = False) -> ToolResult:
    from fastmcp.tools import ToolResult  # noqa: PLC0415 - keep import cost off module load

    text = json.dumps(structured, ensure_ascii=False, default=str)
    return ToolResult(
        content=[TextContent(type="text", text=text)],
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

    async def execute(self, tool: ToolDefinition, arguments: dict[str, Any]) -> ToolResult:
        try:
            return await self._execute(tool, arguments)
        except DavinciError as err:
            return error_result(err)

    def _parse(
        self, tool: ToolDefinition, arguments: dict[str, Any]
    ) -> tuple[ActionSpec, Any, Any]:
        try:
            call = tool.call_adapter.validate_python(arguments)
        except ValidationError as exc:
            raise DavinciError(
                ErrorCode.INVALID_PARAMS,
                f"Invalid arguments for {tool.name}.",
                hint=f"op.action must be one of: {', '.join(tool.actions)}.",
                retryable=False,
                details={"errors": json.loads(exc.json(include_url=False))},
            ) from None
        op = call.op
        spec = tool.actions[op.action]
        params = spec.params.model_validate(op.model_dump(exclude={"action"}, by_alias=False))
        return spec, call, params

    async def _execute(self, tool: ToolDefinition, arguments: dict[str, Any]) -> ToolResult:
        spec, call, params = self._parse(tool, arguments)
        session = await self.connection.session()
        self._gate(spec, session)
        started = time.monotonic()

        if spec.kind is Kind.READ:
            result = await self.connection.call(
                lambda s: spec.handler(s, params), retry_safe=True, timeout=spec.timeout_s
            )
            return tool_result(
                {"action": spec.name, "result": _dump(result), "channel": session.caps.channel}
            )

        dry_run: bool = call.dry_run
        params_json = params.model_dump(mode="json")
        if dry_run or spec.kind is Kind.DESTRUCTIVE:
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
                    WriteOutput(status="planned", plan=plan.model_dump(mode="json")).model_dump(
                        mode="json"
                    )
                )
            self.consent.verify(token, spec.qualified_name, params_json, draft.fingerprint)

        try:
            applied = await self.connection.call(
                lambda s: self._apply(spec, s, params),
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
            WriteOutput(status="applied", receipt=receipt).model_dump(
                mode="json", exclude_none=True
            )
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

    def _apply(self, spec: ActionSpec, session: ResolveSession, params: BaseModel) -> _Applied:
        with PageGuard(session.resolve, spec.page):
            snapshot_id: str | None = None
            if spec.snapshot == "timeline":
                snapshot_id = self.snapshots.snapshot_timeline(
                    session, session.timeline, spec.qualified_name
                ).id
            elif spec.snapshot == "project":
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
