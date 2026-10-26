# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Recipe engine: templating, loading, and runs through the real pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from davinci_mcp.actions.spec import Kind, PlanDraft, action
from davinci_mcp.config import Settings, config_home, state_home
from davinci_mcp.errors import DavinciError, ErrorCode
from davinci_mcp.receipts import Verification
from davinci_mcp.recipes.loader import load_recipes
from davinci_mcp.recipes.templating import render
from davinci_mcp.safety.paths import check_path, protect
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, mcp_client

# -- templating ---------------------------------------------------------------------


def test_render_whole_part_projection_and_index() -> None:
    ctx = {
        "params": {"name": "Reel", "n": 3},
        "steps": {"a": {"items": [{"id": "x"}, {"id": "y"}]}},
    }
    assert render("{{ params.n }}", ctx) == 3
    assert render("v-{{ params.name }}-{{ params.n }}", ctx) == "v-Reel-3"
    assert render("{{ steps.a.items[*].id }}", ctx) == ["x", "y"]
    assert render({"k": ["{{ steps.a.items[1].id }}"]}, ctx) == {"k": ["y"]}


@pytest.mark.parametrize(
    "template",
    ["{{ os.system }}", "{{ params.missing }}", "{{ steps.a.items[9] }}", "{{ params.n() }}"],
)
def test_render_rejects_anything_else(template: str) -> None:
    ctx = {"params": {"n": 1}, "steps": {"a": {"items": []}}}
    with pytest.raises(DavinciError):
        render(template, ctx)


# -- loading --------------------------------------------------------------------------

PY_RECIPE = """
from pydantic import BaseModel
from davinci_mcp.recipes.model import PythonRecipe

class P(BaseModel):
    name: str

async def run(ctx, p):
    out = await ctx.call("demo_write", {"action": "add", "name": p.name})
    return {"added": out["receipt"]["result"]["id"]}

RECIPE = PythonRecipe(name="py_add", description="Add via Python.", params=P, run=run)
"""


def test_loader_builtin_yaml_and_python_gate(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("name: 1 2\n")
    (tmp_path / "py_add.py").write_text(PY_RECIPE)
    loaded = load_recipes([tmp_path], allow_python=False)
    assert "assemble_timeline" in loaded.recipes
    assert "py_add" not in loaded.recipes
    assert any("allow_python" in e for e in loaded.errors)
    assert any("bad.yaml" in e for e in loaded.errors)
    assert "py_add" in load_recipes([tmp_path], allow_python=True).recipes


# -- runs -----------------------------------------------------------------------------

STORE: list[str] = []


class AddParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class Added(BaseModel):
    id: str
    count: int


def _verify_add(_s: Any, p: AddParams, _r: Added) -> Verification:
    return Verification(ok=p.name in STORE and p.name != "lie")


@action(domain="demo", kind=Kind.WRITE, params=AddParams, result=Added, verify=_verify_add)
def add(_s: Any, p: AddParams) -> Added:
    """Add a name."""
    if p.name != "lie":
        STORE.append(p.name)
    return Added(id=f"id-{p.name}", count=len(STORE))


class RemoveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


def _plan_remove(_s: Any, p: RemoveParams) -> PlanDraft:
    return PlanDraft(summary=f"remove {p.id}", effects=[], targets=[p.id], fingerprint=p.id)


def _verify_removed(_s: Any, p: RemoveParams, _r: Added) -> Verification:
    return Verification(ok=p.id.removeprefix("id-") not in STORE)


@action(
    domain="demo",
    kind=Kind.DESTRUCTIVE,
    params=RemoveParams,
    result=Added,
    plan=_plan_remove,
    verify=_verify_removed,
)
def remove(_s: Any, p: RemoveParams) -> Added:
    """Remove a name."""
    STORE.remove(p.id.removeprefix("id-"))
    return Added(id=p.id, count=len(STORE))


CHAIN = """
name: chain
description: add then remove what was added
params:
  who: {type: string, required: true}
steps:
  - id: first
    tool: demo_write
    op: {action: add, name: "{{ params.who }}"}
  - id: second
    tool: demo_write
    op: {action: add, name: "{{ params.who }}-2"}
  - id: drop
    tool: demo_delete
    op: {action: remove, id: "{{ steps.first.receipt.result.id }}"}
"""

DESTRUCTIVE = CHAIN.replace("name: chain", "name: chain_confirmed").replace(
    "params:", "destructive: true\nparams:", 1
)


@pytest.fixture
def recipe_settings(settings: Settings, tmp_path: Path) -> Settings:
    STORE.clear()
    folder = tmp_path / "recipes"
    folder.mkdir()
    (folder / "chain.yaml").write_text(CHAIN)
    (folder / "chain_confirmed.yaml").write_text(DESTRUCTIVE)
    (folder / "fails.yaml").write_text(
        CHAIN.replace("name: chain", "name: fails").replace('"{{ params.who }}-2"', '"lie"')
    )
    (folder / "py_add.py").write_text(PY_RECIPE)
    return settings.model_copy(
        update={
            "recipes": settings.recipes.model_copy(update={"dirs": [folder], "allow_python": True})
        }
    )


async def test_chain_runs_in_one_call(recipe_settings: Settings) -> None:
    async with mcp_client(make_resolve(), recipe_settings, [add, remove]) as client:
        args = {"name": "chain", "params": {"who": "ana"}}
        err, planned = await call(client, "recipe_run", args)
        # it contains a *_delete step: treated as destructive although it does not say so
        assert not err and planned["status"] == "planned" and STORE == []
        err, out = await call(
            client, "recipe_run", {**args, "confirm_token": planned["plan"]["token"]}
        )
    assert not err and out["status"] == "completed"
    assert [s["status"] for s in out["steps"]] == ["ok", "ok", "ok"]
    assert STORE == ["ana-2"]  # inner destructive step ran pre-authorized


async def test_failed_or_unverified_step_stops_the_recipe(recipe_settings: Settings) -> None:
    async with mcp_client(make_resolve(), recipe_settings, [add, remove]) as client:
        args = {"name": "fails", "params": {"who": "bo"}}
        _, planned = await call(client, "recipe_run", args)
        err, out = await call(
            client, "recipe_run", {**args, "confirm_token": planned["plan"]["token"]}
        )
    assert not err and out["status"] == "failed"
    assert [s["status"] for s in out["steps"]] == ["ok", "failed"]
    assert out["steps"][1]["error"]["code"] == "VERIFICATION_FAILED"
    assert STORE == ["bo"]  # the destructive step never ran


async def test_destructive_recipe_needs_consent(recipe_settings: Settings) -> None:
    async with mcp_client(make_resolve(), recipe_settings, [add, remove]) as client:
        args = {"name": "chain_confirmed", "params": {"who": "cy"}}
        err, planned = await call(client, "recipe_run", args)
        assert not err and planned["status"] == "planned" and STORE == []
        assert "drop: demo_delete.remove" in planned["plan"]["effects"]
        err, out = await call(
            client, "recipe_run", {**args, "confirm_token": planned["plan"]["token"]}
        )
        assert not err and out["status"] == "completed"
        err, out = await call(
            client,
            "recipe_run",
            {
                "name": "chain_confirmed",
                "params": {"who": "zz"},
                "confirm_token": planned["plan"]["token"],
            },
        )
        assert err and out["code"] == "CONFIRMATION_INVALID"


async def test_dry_run_params_and_unknown(recipe_settings: Settings) -> None:
    async with mcp_client(make_resolve(), recipe_settings, [add, remove]) as client:
        err, out = await call(
            client, "recipe_run", {"name": "chain", "params": {"who": "d"}, "dry_run": True}
        )
        assert not err and out["status"] == "planned" and STORE == []
        err, out = await call(client, "recipe_run", {"name": "chain", "params": {}})
        assert err and out["code"] == "INVALID_PARAMS"
        err, out = await call(client, "recipe_run", {"name": "nope"})
        assert err and out["code"] == "NOT_FOUND"
        err, listed = await call(client, "recipe_list", {})
        names = {r["name"] for r in listed["recipes"]}
        assert {"chain", "assemble_timeline", "py_add"} <= names


async def test_python_recipe(recipe_settings: Settings) -> None:
    async with mcp_client(make_resolve(), recipe_settings, [add, remove]) as client:
        err, out = await call(client, "recipe_run", {"name": "py_add", "params": {"name": "eve"}})
    assert not err and out["status"] == "completed"
    assert out["outputs"] == {"added": "id-eve"} and STORE == ["eve"]


PY_DELETE = PY_RECIPE.replace('name="py_add"', 'name="py_del"').replace(
    'out = await ctx.call("demo_write", {"action": "add", "name": p.name})',
    'out = await ctx.call("demo_delete", {"action": "remove", "id": "id-" + p.name})',
)


async def test_python_recipe_cannot_run_unconfirmed_destructive_steps(
    recipe_settings: Settings,
) -> None:
    folder = recipe_settings.recipes.dirs[0]
    (folder / "py_del.py").write_text(PY_DELETE)
    STORE.append("joe")
    async with mcp_client(make_resolve(), recipe_settings, [add, remove]) as client:
        err, out = await call(client, "recipe_run", {"name": "py_del", "params": {"name": "joe"}})
    assert not err and out["status"] == "failed"
    assert out["steps"][0]["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert STORE == ["joe"]


def test_protected_paths_are_refused(tmp_path: Path) -> None:

    home = [Path.home()]
    for target in (
        config_home() / "config.toml",
        state_home() / "consent.key",
        Path.home() / ".claude.json",
    ):
        with pytest.raises(DavinciError) as exc:
            check_path(target, home)
        assert exc.value.code is ErrorCode.PATH_NOT_ALLOWED


def test_live_settings_locations_are_protected(tmp_path: Path) -> None:
    secret_state = tmp_path / "custom-state"
    protect([secret_state])
    try:
        with pytest.raises(DavinciError) as exc:
            check_path(secret_state / "consent.key", [tmp_path])
        assert exc.value.code is ErrorCode.PATH_NOT_ALLOWED
        assert check_path(tmp_path / "media.mov", [tmp_path]) == (tmp_path / "media.mov").resolve()
    finally:
        protect([])
