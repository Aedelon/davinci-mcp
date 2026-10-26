# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Snapshot policy: destructive-only by default, per-call override, dedicated bin."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.config import Settings
from davinci_mcp.receipts import Verification
from davinci_mcp.refs import TimelineRef
from davinci_mcp.safety.snapshots import SNAPSHOT_FOLDER
from tests.fakes.fake_resolve import FakeResolve, make_resolve
from tests.helpers import call, confirm, mcp_client


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeline: TimelineRef | None = None


class _Done(BaseModel):
    ok: bool = True


def _ok(*_: Any) -> Verification:
    return Verification(ok=True)


@action(
    domain="demo", kind=Kind.WRITE, params=_Target, result=_Done, snapshot="timeline", verify=_ok
)
def edit(_s: Any, _p: _Target) -> _Done:
    """Pretend to edit a timeline."""
    return _Done()


def _plan(_s: Any, _p: _Target) -> PlanDraft:
    return PlanDraft(summary="wipe", effects=[], targets=[], fingerprint="f")


@action(
    domain="demo",
    kind=Kind.DESTRUCTIVE,
    params=_Target,
    result=_Done,
    snapshot="timeline",
    verify=_ok,
    plan=_plan,
)
def wipe(_s: Any, _p: _Target) -> _Done:
    """Pretend to destroy something."""
    return _Done()


@action(domain="demo", kind=Kind.WRITE, result=_Done, verify=_ok)
def plain(_s: Any, _p: BaseModel) -> _Done:
    """A write that declares no snapshot."""
    return _Done()


ACTIONS = [edit, wipe, plain]


def _project(resolve: FakeResolve) -> Any:
    return resolve.GetProjectManager().GetCurrentProject()


def _snapshots(resolve: FakeResolve) -> list[str]:
    return [t.GetName() for t in _project(resolve).timelines if "[dmcp-snap" in t.GetName()]


def _with(settings: Settings, policy: str) -> Settings:
    return settings.model_copy(
        update={"safety": settings.safety.model_copy(update={"auto_snapshot": policy})}
    )


async def test_default_policy_is_destructive_only(settings: Settings) -> None:
    resolve = make_resolve(timelines=1)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "demo_write", {"op": {"action": "edit"}})
        assert not err and "snapshot_id" not in out["receipt"]
        assert _snapshots(resolve) == []
        err, out = await confirm(client, "demo_delete", {"op": {"action": "wipe"}})
        assert not err and out["receipt"]["snapshot_id"]
        assert len(_snapshots(resolve)) == 1


@pytest.mark.parametrize(("policy", "write_snaps", "delete_snaps"), [("off", 0, 0), ("all", 1, 1)])
async def test_policy_setting(
    settings: Settings, policy: str, write_snaps: int, delete_snaps: int
) -> None:
    resolve = make_resolve(timelines=1)
    async with mcp_client(resolve, _with(settings, policy), ACTIONS) as client:
        await call(client, "demo_write", {"op": {"action": "edit"}})
        assert len(_snapshots(resolve)) == write_snaps
        await confirm(client, "demo_delete", {"op": {"action": "wipe"}})
        assert len(_snapshots(resolve)) == write_snaps + delete_snaps


async def test_per_call_override(settings: Settings) -> None:
    resolve = make_resolve(timelines=1)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        await call(client, "demo_write", {"op": {"action": "edit"}, "snapshot": True})
        assert len(_snapshots(resolve)) == 1
        await call(client, "demo_write", {"op": {"action": "plain"}, "snapshot": True})
        assert len(_snapshots(resolve)) == 2  # even without a declared snapshot
        await confirm(client, "demo_delete", {"op": {"action": "wipe"}, "snapshot": False})
        assert len(_snapshots(resolve)) == 2


async def test_snapshot_targets_the_referenced_timeline(settings: Settings) -> None:
    resolve = make_resolve(timelines=2)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        op = {"op": {"action": "edit", "timeline": {"index": 2}}, "snapshot": True}
        await call(client, "demo_write", op)
    assert _snapshots(resolve)[0].startswith("Timeline 2 [dmcp-snap")


async def test_copies_are_filed_in_a_dedicated_bin(settings: Settings) -> None:
    resolve = make_resolve(timelines=1)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        await call(client, "demo_write", {"op": {"action": "edit"}, "snapshot": True})
        err, listed = await call(client, "snapshot_list", {})
    pool = _project(resolve).media_pool
    bins = {f.GetName(): f for f in pool.root_folder.subfolders}
    assert SNAPSHOT_FOLDER in bins
    assert [c.GetName() for c in bins[SNAPSHOT_FOLDER].clips] == _snapshots(resolve)
    assert not any("[dmcp-snap" in c.GetName() for c in pool.root_folder.clips)
    assert pool.GetCurrentFolder() is pool.root_folder  # current bin restored
    assert not err and listed["snapshots"][0]["folder"].endswith(SNAPSHOT_FOLDER)
