# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Code execution escape hatch (OFF by default).

Runs caller-supplied Python (or Fusion Lua) inside the Resolve executor thread.
Always destructive-gated (plan + confirm_token) and disabled unless
``safety.enable_code_execution`` is true.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import traceback
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.domains._runtime import runtime
from davinci_mcp.errors import DavinciError, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from davinci_mcp.connection.manager import ResolveSession

DOMAIN = "code"
STDOUT_LIMIT = 64 * 1024


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodeParams(_Params):
    code: str = Field(min_length=1, description="Source to run. Set `result` to return a value.")


class CodeResult(BaseModel):
    result: Any = None
    result_repr_fallback: bool = Field(
        default=False, description="True when the result was not JSON-serialisable (repr used)."
    )
    stdout: str = ""
    stdout_truncated: bool = False


def _require_enabled() -> None:
    if not runtime().settings.safety.enable_code_execution:
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "Code execution is disabled.",
            hint="set safety.enable_code_execution = true",
            retryable=False,
        )


def _plan(label: str) -> Callable[[ResolveSession, CodeParams], PlanDraft]:
    def plan(s: ResolveSession, p: CodeParams) -> PlanDraft:
        _require_enabled()
        digest = hashlib.sha256(p.code.encode()).hexdigest()
        return PlanDraft(
            summary=f"Execute arbitrary {label} inside DaVinci Resolve (sha256 {digest[:12]}).",
            effects=[
                f"Full script (sha256 {digest}):\n{p.code}",
                "The code has unrestricted access to Resolve and this machine.",
            ],
            targets=[f"code:{digest}"],
            fingerprint=digest[:16],
        )

    return plan


class _BoundedBuffer(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.truncated = False

    def write(self, s: str, /) -> int:
        room = STDOUT_LIMIT - self.tell()
        if room <= 0:
            self.truncated = self.truncated or bool(s)
            return len(s)
        if len(s) > room:
            self.truncated = True
            super().write(s[:room])
            return len(s)
        return super().write(s)


def _jsonable(value: object) -> tuple[Any, bool]:
    try:
        return json.loads(json.dumps(value)), False
    except (TypeError, ValueError):
        return repr(value), True


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=CodeParams,
    result=CodeResult,
    plan=_plan("Python"),
    verify_reason="Arbitrary code has no generic read-back",
    timeout_s=60.0,
)
def run_python(s: ResolveSession, p: CodeParams) -> CodeResult:
    """Run Python with resolve/project/fusion globals; set `result` (off by default)."""
    _require_enabled()
    scope: dict[str, Any] = {
        "__name__": "__resolve_code__",
        "resolve": s.resolve,
        "project": s.project,
        "fusion": s.resolve.Fusion(),
        "result": None,
    }
    buf = _BoundedBuffer()
    try:
        compiled = compile(p.code, "<run_python>", "exec")
        with contextlib.redirect_stdout(buf):
            # Deliberate escape hatch: gated by safety.enable_code_execution + consent token.
            exec(compiled, scope)  # noqa: S102
    except Exception as exc:
        raise DavinciError(
            ErrorCode.API_CALL_FAILED,
            f"{type(exc).__name__}: {exc}",
            details={
                "traceback": traceback.format_exc()[-2000:],
                "stdout": buf.getvalue()[-2000:],
            },
            retryable=False,
        ) from exc
    value, fallback = _jsonable(scope.get("result"))
    return CodeResult(
        result=value,
        result_repr_fallback=fallback,
        stdout=buf.getvalue(),
        stdout_truncated=buf.truncated,
    )


@action(
    domain=DOMAIN,
    kind=Kind.DESTRUCTIVE,
    params=CodeParams,
    result=CodeResult,
    plan=_plan("Fusion Lua"),
    verify_reason="Arbitrary code has no generic read-back",
    timeout_s=60.0,
)
def run_lua(s: ResolveSession, p: CodeParams) -> CodeResult:
    """Run Lua through Fusion.Execute (off by default)."""
    _require_enabled()
    fusion = s.resolve.Fusion()
    if fusion is None or not s.caps.has(fusion, "Execute"):
        raise DavinciError(
            ErrorCode.CAPABILITY_UNAVAILABLE, "Fusion.Execute is not available.", retryable=False
        )
    value, fallback = _jsonable(fusion.Execute(p.code))
    return CodeResult(result=value, result_repr_fallback=fallback)


ACTIONS = [run_python, run_lua]
