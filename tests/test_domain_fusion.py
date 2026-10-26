# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Fusion domain tests over fake comps, tools and inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.config import Settings
from davinci_mcp.domains import fusion
from davinci_mcp.domains.fusion import ACTIONS
from tests.fakes.fake_resolve import FakeResolve, make_resolve
from tests.fakes.fusion import FakeComp, attach_fusion, attach_registry
from tests.helpers import call, confirm, mcp_client

ITEM = {"name": "item_0_0"}


def res(out: dict[str, Any]) -> Any:
    return out["result"] if "result" in out else out["receipt"]["result"]


def _setup(names: list[str] | None = None) -> tuple[FakeResolve, list[FakeComp]]:
    resolve = make_resolve()
    project = resolve.project_manager._current
    item = project.timelines[0].tracks[0][0]
    return resolve, attach_fusion(item, ["Composition 1"] if names is None else names)


def _op(action: str, **kw: Any) -> dict[str, Any]:
    return {"op": {"action": action, "item": ITEM, **kw}}


async def test_list_and_info(settings: Settings) -> None:
    resolve, comps = _setup()
    bg = comps[0].make_tool("Background")
    merge = comps[0].make_tool("Merge")
    merge.inputs["Background"].source = bg.FindMainOutput(1)
    comps[0].ActiveTool = bg
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "fusion_read", _op("list_comps"))
        assert not err and res(out)["names"] == ["Composition 1"]
        err, out = await call(client, "fusion_read", _op("comp_info"))
        assert not err
        r = res(out)
        assert [t["name"] for t in r["tools"]] == ["Background1", "Merge1"]
        assert r["connections"] == [
            {"from_tool": "Background1", "to_tool": "Merge1", "to_input": "Background"}
        ]
        assert r["active_tool"] == "Background1" and r["render_end"] == 99
        assert r["current_time"] == 10.0


async def test_no_comp_not_found(settings: Settings) -> None:
    resolve, _ = _setup([])
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _out = await call(client, "fusion_read", _op("comp_info"))
        assert err


async def test_tool_inputs_and_registry(settings: Settings) -> None:
    resolve, comps = _setup()
    comps[0].make_tool("Background")
    attach_registry(resolve, ["Background", "Merge", "TextPlus", "BezierSpline"])
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client, "fusion_read", _op("tool_inputs", tool="Background1", names=["Blend"])
        )
        assert not err
        inputs = res(out)["inputs"]
        assert inputs[0]["id"] == "Blend" and inputs[0]["value"] == 1.0
        assert inputs[0]["data_type"] == "Number" and not inputs[0]["animated"]
        err, out = await call(
            client, "fusion_read", {"op": {"action": "registry_types", "query": "text"}}
        )
        assert not err and [t["id"] for t in res(out)["types"]] == ["TextPlus"]


async def test_comp_lifecycle(settings: Settings, tmp_path: Path) -> None:
    resolve, _ = _setup([])
    src = tmp_path / "x.comp"
    src.write_text("{}")
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "fusion_write", _op("add_comp"))
        assert not err and res(out)["comp"] == "Composition 1"
        err, out = await call(client, "fusion_write", _op("import_comp", path=str(src)))
        assert not err and res(out)["count"] == 2
        err, _o = await call(
            client, "fusion_write", _op("import_comp", path=str(tmp_path.parent / "no.comp"))
        )
        assert err
        err, _o = await call(
            client, "fusion_write", _op("rename_comp", old_name="Composition 1", new_name="Main")
        )
        assert not err
        err, _o = await call(client, "fusion_write", _op("load_comp", name="Main"))
        assert not err
        err, _o = await call(client, "fusion_write", _op("load_comp", name="zzz"))
        assert err
        dest = tmp_path / "out.comp"
        err, _o = await call(client, "fusion_write", _op("export_comp", path=str(dest)))
        assert not err and dest.exists()
        err, out = await confirm(client, "fusion_delete", _op("delete_comp", name="Main"))
        assert not err and res(out)["names"] == ["Composition 2"]


async def test_add_tool_undo_and_unlock(settings: Settings) -> None:
    resolve, comps = _setup()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client, "fusion_write", _op("add_tool", tool_id="Background", name="Bg", x=1, y=2)
        )
        assert not err and res(out)["tool"] == "Bg"
        comp = comps[0]
        assert comp.lock_depth == 0 and comp.open_undo == 0 and comp.undo_log
        err, _o = await call(client, "fusion_write", _op("add_tool", tool_id="Nope"))
        assert err
        assert comp.lock_depth == 0 and comp.open_undo == 0
        err, _o = await call(
            client, "fusion_write", _op("add_tool", tool_id="Background", name="Bg")
        )
        assert err


async def test_set_inputs_and_verification_failure(settings: Settings) -> None:
    resolve, comps = _setup()
    bg = comps[0].make_tool("Background")
    sets = [
        {"tool": "Background1", "input": "Blend", "value": 0.5},
        {"tool": "Background1", "input": "Center", "value": [0.1, 0.9]},
    ]
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "fusion_write", _op("set_inputs", sets=sets))
        assert not err and bg.values["Blend"] == 0.5
        assert comps[0].lock_depth == 0
        bad = [{"tool": "Background1", "input": "Nope", "value": 1}]
        err, _o = await call(client, "fusion_write", _op("set_inputs", sets=bad))
        assert err
        bg.ignore_writes = True
        err, out = await call(
            client,
            "fusion_write",
            _op("set_inputs", sets=[{"tool": "Background1", "input": "Blend", "value": 0.2}]),
        )
        assert err or out["receipt"]["verified"] is False


async def test_connect_disconnect(settings: Settings) -> None:
    resolve, comps = _setup()
    comps[0].make_tool("Background")
    comps[0].make_tool("Merge")
    args = {"tool": "Merge1", "input": "Background", "source_tool": "Background1"}
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "fusion_write", _op("connect", **args))
        assert not err and res(out)["source_tool"] == "Background1"
        err, out = await call(
            client, "fusion_write", _op("disconnect", tool="Merge1", input="Background")
        )
        assert not err and res(out)["source_tool"] == "Background1"
        assert comps[0].FindTool("Merge1").inputs["Background"].source is None
        err, _o = await call(client, "fusion_write", _op("connect", **{**args, "source_tool": "x"}))
        assert err


async def test_keyframes_expression_clear(settings: Settings) -> None:
    resolve, comps = _setup()
    bg = comps[0].make_tool("Background")
    keys = [{"time": 0, "value": 0.0}, {"time": 24, "value": 1.0}]
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client,
            "fusion_write",
            _op("set_keyframes", tool="Background1", input="Blend", keys=keys),
        )
        assert not err and bg.inputs["Blend"].animated
        assert res(out)["keyframes"] == {"0": 0.0, "24": 1.0} or res(out)["keyframes"]
        err, out = await call(
            client,
            "fusion_write",
            _op("set_expression", tool="Background1", input="TopLeftRed", expression="Blend/2"),
        )
        assert not err and bg.inputs["TopLeftRed"].expression == "Blend/2"
        err, out = await confirm(
            client, "fusion_delete", _op("clear_keyframes", tool="Background1", input="Blend")
        )
        assert not err and not bg.inputs["Blend"].animated
        err, _o = await call(
            client, "fusion_delete", _op("clear_keyframes", tool="Background1", input="Blend")
        )
        assert err  # no longer animated -> plan refuses


async def test_rename_enable(settings: Settings) -> None:
    resolve, comps = _setup()
    bg = comps[0].make_tool("Background")
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _o = await call(
            client, "fusion_write", _op("rename_tool", tool="Background1", new_name="Sky")
        )
        assert not err and bg.Name == "Sky"
        err, _o = await call(
            client, "fusion_write", _op("set_tool_enabled", tool="Sky", enabled=False)
        )
        assert not err and bg.passthrough is True


async def test_settings_files_and_paste(settings: Settings, tmp_path: Path) -> None:
    resolve, comps = _setup()
    bg = comps[0].make_tool("Background")
    f = tmp_path / "bg.setting"
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _o = await call(
            client, "fusion_write", _op("save_tool_settings", tool="Background1", path=str(f))
        )
        assert not err and f.exists()
        err, _o = await call(
            client, "fusion_write", _op("save_tool_settings", tool="Background1", path=str(f))
        )
        assert err  # exists, no overwrite
        err, _o = await call(
            client, "fusion_write", _op("load_tool_settings", tool="Background1", path=str(f))
        )
        assert not err and "loaded" in bg.saved
        err, _o = await call(
            client,
            "fusion_write",
            _op("save_tool_settings", tool="Background1", path="/etc/evil.setting"),
        )
        assert err
        err, out = await call(
            client, "fusion_write", _op("paste_settings", text="{ Tools = ordered() {} }")
        )
        assert not err and res(out)["new_tools"] == ["Pasted1"]
        comps[0].paste_ok = False
        err, _o = await call(
            client, "fusion_write", _op("paste_settings", text="{ Tools = ordered() {} }")
        )
        assert err
        assert comps[0].lock_depth == 0


async def test_text_title(settings: Settings) -> None:
    resolve, comps = _setup()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client,
            "fusion_write",
            _op("create_text_title", text="Hello", size=0.1, font="Arial", color="#FF0000"),
        )
        assert not err
        tool = comps[0].FindTool(res(out)["tool"])
        assert tool.values["StyledText"] == "Hello" and tool.values["Red1"] == 1.0
        assert tool.values["Green1"] == 0.0
        err, _o = await call(
            client, "fusion_write", _op("create_text_title", text="x", color="red")
        )
        assert err


async def test_delete_tools(settings: Settings) -> None:
    resolve, comps = _setup()
    comps[0].make_tool("Background")
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _out = await confirm(
            client, "fusion_delete", _op("delete_tools", tools=["Background1"])
        )
        assert not err and comps[0].tools == []
        err, _o = await call(client, "fusion_delete", _op("delete_tools", tools=["Background1"]))
        assert err


async def test_fuses(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolve, _ = _setup()
    official = tmp_path / "official"
    monkeypatch.setattr(fusion, "_official_fuses_dir", lambda: official)
    install = {"op": {"action": "install_fuse", "filename": "A.fuse", "source": "x"}}
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "fusion_delete", install)
        assert err and out["code"] == "CAPABILITY_UNAVAILABLE"  # code execution is off
    enabled = settings.model_copy(
        update={"safety": settings.safety.model_copy(update={"enable_code_execution": True})}
    )
    async with mcp_client(resolve, enabled, ACTIONS) as client:
        err, out = await call(client, "fusion_delete", install)
        assert not err and out["status"] == "planned"
        assert any("sha256" in e for e in out["plan"]["effects"])
        err, out = await confirm(client, "fusion_delete", install)
        assert not err and (official / "A.fuse").read_text() == "x"
        err, _o = await confirm(
            client,
            "fusion_delete",
            {"op": {"action": "install_fuse", "filename": "A.fuse", "source": "y"}},
        )
        assert err  # never overwritten
        err, _o = await confirm(
            client,
            "fusion_delete",
            {"op": {"action": "install_fuse", "filename": "../b.fuse", "source": "y"}},
        )
        assert err
        err, _o = await confirm(
            client,
            "fusion_delete",
            {
                "op": {
                    "action": "install_fuse",
                    "filename": "B.fuse",
                    "source": "y",
                    "directory": "/etc",
                }
            },
        )
        assert err
        err, out = await call(client, "fusion_read", {"op": {"action": "list_fuses"}})
        assert not err and [f["path"] for f in res(out)["fuses"]] == ["A.fuse"]


async def test_script_content_is_gated(settings: Settings) -> None:
    resolve, _ = _setup()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(
            client,
            "fusion_write",
            _op("paste_settings", text='{ Tools = { A = { FrameRenderScript = "os.exit()" } } }'),
        )
        assert err and out["code"] == "CAPABILITY_UNAVAILABLE"
        err, out = await call(
            client,
            "fusion_write",
            _op(
                "set_expression", tool="Background1", input="TopLeftRed", expression="io.open('x')"
            ),
        )
        assert err and out["code"] == "CAPABILITY_UNAVAILABLE"
