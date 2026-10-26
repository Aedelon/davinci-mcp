# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Code domain tests: disabled by default, gated by plan + confirm, fake Resolve only."""

from __future__ import annotations

from typing import Any

from davinci_mcp.config import Settings
from davinci_mcp.domains import code
from davinci_mcp.domains.code import ACTIONS
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, confirm, mcp_client


def _args(src: str, action: str = "run_python") -> dict[str, Any]:
    return {"op": {"action": action, "code": src}}


async def test_disabled_by_default(settings: Settings) -> None:
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, out = await call(client, "code_delete", _args("result = 1"))
    assert err
    assert "enable_code_execution" in str(out)


async def test_plan_then_run(settings: Settings) -> None:
    settings.safety.enable_code_execution = True
    resolve = make_resolve()
    src = "print('hi')\nresult = {'v': resolve.GetVersionString(), 'p': project.GetName()}"
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, planned = await call(client, "code_delete", _args(src))
        assert not err
        assert planned["status"] == "planned"
        assert "sha256" in str(planned["plan"])
        assert "GetVersionString" in str(planned["plan"])
        err, out = await confirm(client, "code_delete", _args(src))
    assert not err
    assert "21.1.1.10" in str(out)
    assert "Test Project" in str(out)
    assert "hi" in str(out)


async def test_non_serialisable_result(settings: Settings) -> None:
    settings.safety.enable_code_execution = True
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, out = await confirm(client, "code_delete", _args("result = object()"))
    assert not err
    assert "result_repr_fallback" in str(out)
    assert "object object" in str(out)


async def test_stdout_bounded_and_errors(settings: Settings) -> None:
    settings.safety.enable_code_execution = True
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, out = await confirm(
            client, "code_delete", _args(f"print('x' * {code.STDOUT_LIMIT * 2})")
        )
        assert not err
        assert "stdout_truncated" in str(out)
        assert len(str(out)) < code.STDOUT_LIMIT * 2
        err, out = await confirm(client, "code_delete", _args("raise ValueError('bad')"))
    assert err
    assert "ValueError" in str(out)


async def test_run_lua(settings: Settings) -> None:
    settings.safety.enable_code_execution = True
    resolve = make_resolve()
    seen: list[str] = []

    def execute(script: str) -> str:
        seen.append(script)
        return "ok"

    resolve.fusion.Execute = execute  # type: ignore[attr-defined]
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _ = await confirm(client, "code_delete", _args("return 1", "run_lua"))
    assert not err
    assert seen == ["return 1"]
