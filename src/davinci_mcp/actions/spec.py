# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Action specification and the ``@action`` decorator.

An action is a plain function ``(session, params) -> result model`` that runs on
the resolve-api executor thread. Everything else (tool schema, annotations,
consent, snapshot, page handling, receipt, audit) is derived from its spec.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from davinci_mcp.capabilities import Version
    from davinci_mcp.connection.manager import ResolveSession
    from davinci_mcp.receipts import Verification
    from davinci_mcp.safety.pages import Page

SnapshotKind = Literal["timeline", "project"]


class Kind(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @property
    def tool_suffix(self) -> str:
        return {"read": "read", "write": "write", "destructive": "delete"}[self.value]


class PlanDraft(BaseModel):
    """What a destructive action is about to do; signed into a consent token."""

    summary: str
    effects: list[str]
    targets: list[str]
    fingerprint: str


type Handler = Callable[[ResolveSession, Any], Any]  # a result model, or a JobRequest
type JobStatusPoll = Callable[[ResolveSession], Mapping[str, Any]]


@dataclass(frozen=True)
class JobRequest:
    """Returned by ``long_running`` handlers: what to launch and how to poll it.

    ``launch`` runs once (never replayed) and must return a truthy value on
    success; ``poll`` returns a status dict (``CompletionPercentage``, ``Error``…).
    """

    kind: str
    label: str
    launch: Callable[[ResolveSession], Any]
    poll: JobStatusPoll
    cancel: Callable[[ResolveSession], Any] | None = None
    interpret: Callable[[Mapping[str, Any]], tuple[str, float | None, str | None]] | None = None


type Verifier = Callable[[ResolveSession, Any, Any], Verification]
type Capture = Callable[[ResolveSession, Any], dict[str, Any]]
type Planner = Callable[[ResolveSession, Any], PlanDraft]


class Empty(BaseModel):
    """Parameter model for actions without parameters."""


@dataclass(frozen=True)
class ActionSpec:
    domain: str
    name: str
    kind: Kind
    handler: Handler
    params: type[BaseModel]
    result: type[BaseModel]
    summary: str
    requires: tuple[str, ...] = ()
    since: Version | None = None
    studio_only: bool = False
    idempotent: bool = False
    snapshot: SnapshotKind | None = None
    page: Page | None = None
    verify: Verifier | None = None
    verify_reason: str | None = None
    capture: Capture | None = None
    plan: Planner | None = None
    timeout_s: float | None = None
    long_running: bool = False
    tags: frozenset[str] = field(default_factory=frozenset[str])

    @property
    def qualified_name(self) -> str:
        return f"{self.domain}.{self.name}"

    @property
    def tool_name(self) -> str:
        return f"{self.domain}_{self.kind.tool_suffix}"

    @property
    def retry_safe(self) -> bool:
        return self.kind is Kind.READ or self.idempotent

    def validate_spec(self) -> None:
        """Enforce the invariants that make the registry trustworthy."""
        if self.long_running and (self.kind is Kind.READ or self.snapshot or self.verify):
            raise ValueError(f"{self.qualified_name}: long-running actions are plain writes")
        if self.long_running:
            return
        if self.kind is not Kind.READ and self.verify is None and not self.verify_reason:
            raise ValueError(f"{self.qualified_name}: writes need verify= or verify_reason=")
        if self.kind is Kind.DESTRUCTIVE and self.plan is None:
            raise ValueError(f"{self.qualified_name}: destructive actions need plan=")
        if self.kind is Kind.READ and (self.snapshot or self.plan):
            raise ValueError(f"{self.qualified_name}: read actions cannot snapshot or plan")
        if not self.name.isidentifier() or not self.domain.isidentifier():
            raise ValueError(f"{self.qualified_name}: names must be identifiers")


def _job_result() -> type[BaseModel]:
    from davinci_mcp.jobs import JobInfo  # noqa: PLC0415 - avoid an import cycle

    return JobInfo


def action(
    *,
    domain: str,
    kind: Kind,
    params: type[BaseModel] = Empty,
    result: type[BaseModel] | None = None,
    name: str | None = None,
    requires: tuple[str, ...] = (),
    since: Version | None = None,
    studio_only: bool = False,
    idempotent: bool = False,
    snapshot: SnapshotKind | None = None,
    page: Page | None = None,
    verify: Verifier | None = None,
    verify_reason: str | None = None,
    capture: Capture | None = None,
    plan: Planner | None = None,
    timeout_s: float | None = None,
    long_running: bool = False,
    tags: frozenset[str] = frozenset(),
) -> Callable[[Handler], ActionSpec]:
    """Turn a handler into an ``ActionSpec``. Register it with ``ActionRegistry.add``."""

    def decorate(fn: Handler) -> ActionSpec:
        doc = (fn.__doc__ or "").strip()
        spec = ActionSpec(
            domain=domain,
            name=name or fn.__name__,
            kind=kind,
            handler=fn,
            params=params,
            result=result or _job_result(),
            summary=doc.splitlines()[0] if doc else (name or fn.__name__).replace("_", " "),
            requires=requires,
            since=since,
            studio_only=studio_only,
            idempotent=idempotent,
            snapshot=snapshot,
            page=page,
            verify=verify,
            verify_reason=verify_reason,
            capture=capture,
            plan=plan,
            timeout_s=timeout_s,
            long_running=long_running,
            tags=tags,
        )
        spec.validate_spec()
        return spec

    return decorate
