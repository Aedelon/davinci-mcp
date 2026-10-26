# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""batch_run: ad-hoc steps chained server-side through the normal pipeline."""

from __future__ import annotations

from typing import Any

from davinci_mcp.config import Settings
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, mcp_client
from tests.test_recipes import STORE, add, remove


def _steps(*ops: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"tool": tool, "op": op} for tool, op in ops]


async def test_batch_chains_steps_with_templates(settings: Settings) -> None:
    STORE.clear()
    steps = [
        {"id": "a", "tool": "demo_write", "op": {"action": "add", "name": "x"}},
        {"tool": "demo_write", "op": {"action": "add", "name": "{{ steps.a.receipt.result.id }}"}},
    ]
    async with mcp_client(make_resolve(), settings, [add, remove]) as client:
        err, out = await call(client, "batch_run", {"steps": steps})
    assert not err and out["status"] == "completed"
    assert [s["id"] for s in out["steps"]] == ["a", "s2"]
    step = out["steps"][0]["output"]  # compact: no full receipt, no duplicated outputs
    assert "receipt" not in step and step["verified"] is True and step["result"]["id"] == "id-x"
    assert out["outputs"] == {}
    assert STORE == ["x", "id-x"]


async def test_batch_with_delete_needs_one_confirmation(settings: Settings) -> None:
    STORE.clear()
    STORE.append("y")
    steps = _steps(("demo_delete", {"action": "remove", "id": "id-y"}))
    async with mcp_client(make_resolve(), settings, [add, remove]) as client:
        err, planned = await call(client, "batch_run", {"steps": steps})
        assert not err and planned["status"] == "planned" and STORE == ["y"]
        token = planned["plan"]["token"]
        other = _steps(("demo_delete", {"action": "remove", "id": "id-z"}))
        err, out = await call(client, "batch_run", {"steps": other, "confirm_token": token})
        assert err and out["code"] in {"CONFIRMATION_INVALID", "CONFIRMATION_STALE"}
        err, out = await call(client, "batch_run", {"steps": steps, "confirm_token": token})
    assert not err and out["status"] == "completed" and STORE == []


async def test_batch_stops_at_first_failure(settings: Settings) -> None:
    STORE.clear()
    steps = _steps(
        ("demo_write", {"action": "add", "name": "lie"}),
        ("demo_write", {"action": "add", "name": "never"}),
    )
    async with mcp_client(make_resolve(), settings, [add, remove]) as client:
        err, out = await call(client, "batch_run", {"steps": steps})
        assert not err and out["status"] == "failed"
        assert [s["status"] for s in out["steps"]] == ["failed"]
        assert "never" not in STORE
        bad = [{"id": "not valid", "tool": "demo_write", "op": {"action": "add", "name": "z"}}]
        err, out = await call(client, "batch_run", {"steps": bad})
        assert err and out["code"] == "INVALID_PARAMS"
