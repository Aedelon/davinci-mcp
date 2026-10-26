# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Fake Fusion comp / tool / input objects with the live ``dir()`` lists of Resolve 21.1.1."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.fakes.fake_resolve import FakeObject, FakeResolve, FakeTimelineItem, recorded


class FakeOutput(FakeObject):
    live_class = "FusionOutput"

    def __init__(self, root: FakeResolve, tool: FakeTool) -> None:
        super().__init__(root)
        self.tool = tool
        self.Name = "Output"
        self.ID = "Output"

    @recorded
    def GetTool(self) -> FakeTool:
        return self.tool


class FakeInput(FakeObject):
    live_class = "FusionInput"

    def __init__(self, root: FakeResolve, tool: FakeTool, name: str, dtype: str = "Number") -> None:
        super().__init__(root)
        self.tool = tool
        self.ID = name
        self.Name = name
        self.dtype = dtype
        self.source: FakeOutput | None = None
        self.expression: str | None = None
        self.keys: dict[float, Any] = {}
        self.animated = False

    @recorded
    def GetConnectedOutput(self) -> FakeOutput | None:
        if self.animated:
            return FakeOutput(self._root, self.tool)  # stands for the spline modifier output
        return self.source

    @recorded
    def ConnectTo(self, output: FakeOutput | None) -> bool:
        self.source = output
        if output is None:
            self.animated = False
            self.keys = {}
        return True

    @recorded
    def GetExpression(self) -> str | None:
        return self.expression

    @recorded
    def SetExpression(self, expr: str | None) -> bool:
        if self.tool.ignore_writes:
            return True
        self.expression = expr
        return True

    @recorded
    def GetKeyFrames(self) -> dict[float, Any]:
        return dict(self.keys)

    @recorded
    def GetAttrs(self) -> dict[str, Any]:
        return {"INPS_ID": self.ID, "INPS_DataType": self.dtype}


class FakeTool(FakeObject):
    live_class = "FusionTool"

    def __init__(
        self, root: FakeResolve, comp: FakeComp, reg_id: str, name: str, inputs: dict[str, Any]
    ) -> None:
        super().__init__(root)
        self.comp = comp
        self.ID = reg_id
        self.Name = name
        self.passthrough = False
        self.ignore_writes = False  # simulate "True returned, nothing applied"
        self.inputs: dict[str, FakeInput] = {k: FakeInput(root, self, k) for k in inputs}
        self.values: dict[str, Any] = dict(inputs)
        self.saved: dict[str, str] = {}

    @recorded
    def GetInputList(self) -> dict[int, FakeInput]:
        return dict(enumerate(self.inputs.values(), start=1))

    @recorded
    def GetInput(self, name: str, time: float | None = None) -> Any:
        inp = self.inputs.get(name)
        if inp is not None and time is not None and time in inp.keys:
            return inp.keys[time]
        return self.values.get(name)

    @recorded
    def SetInput(self, name: str, value: Any, time: float | None = None) -> bool:
        inp = self.inputs.get(name)
        if inp is None or self.ignore_writes:
            return inp is not None
        if inp.animated and time is not None:
            inp.keys[time] = value
        else:
            self.values[name] = value
        return True

    @recorded
    def AddModifier(self, name: str, modifier: str) -> bool:
        inp = self.inputs.get(name)
        if inp is None or modifier != "BezierSpline" or self.ignore_writes:
            return False
        inp.animated = True
        return True

    @recorded
    def ConnectInput(self, name: str, output: FakeOutput | None) -> bool:
        inp = self.inputs.get(name)
        if inp is None or self.ignore_writes:
            return inp is not None
        inp.source = output
        return True

    @recorded
    def FindMainOutput(self, index: int) -> FakeOutput:
        return FakeOutput(self._root, self)

    @recorded
    def GetOutputList(self) -> dict[int, FakeOutput]:
        return {1: FakeOutput(self._root, self)}

    @recorded
    def GetAttrs(self) -> dict[str, Any]:
        return {
            "TOOLS_Name": self.Name,
            "TOOLS_RegID": self.ID,
            "TOOLB_PassThrough": self.passthrough,
        }

    @recorded
    def SetAttrs(self, attrs: dict[str, Any]) -> bool:
        if self.ignore_writes:
            return True
        if "TOOLS_Name" in attrs:
            self.Name = attrs["TOOLS_Name"]
        if "TOOLB_PassThrough" in attrs:
            self.passthrough = attrs["TOOLB_PassThrough"]
        return True

    @recorded
    def SaveSettings(self, path: str) -> bool:
        Path(path).write_text(f"{{ Tools = ordered() {{ {self.Name} = {self.ID} }} }}")
        return True

    @recorded
    def LoadSettings(self, path: str) -> bool:
        self.saved["loaded"] = Path(path).read_text()
        return True

    @recorded
    def Delete(self) -> bool:
        self.comp.tools.remove(self)
        return True


_DEFAULT_INPUTS: dict[str, dict[str, Any]] = {
    "Background": {"Blend": 1.0, "Center": [0.5, 0.5], "TopLeftRed": 0.0},
    "TextPlus": {
        "StyledText": "",
        "Font": "Open Sans",
        "Style": "Regular",
        "Size": 0.08,
        "Red1": 1.0,
        "Green1": 1.0,
        "Blue1": 1.0,
    },
    "Merge": {"Blend": 1.0, "Background": None, "Foreground": None},
    "MediaOut": {"Input": None},
}


class FakeComp(FakeObject):
    live_class = "FusionComposition"

    def __init__(self, root: FakeResolve, name: str) -> None:
        super().__init__(root)
        self.name = name
        self.tools: list[FakeTool] = []
        self.CurrentTime = 10.0
        self.ActiveTool: FakeTool | None = None
        self.lock_depth = 0
        self.undo_log: list[str] = []
        self.open_undo = 0
        self.paste_ok = True
        self.CurrentFrame = SimpleNamespace(FlowView=SimpleNamespace(GetPosTable=self._pos))

    def _pos(self, tool: FakeTool) -> dict[int, float]:
        return {1: float(self.tools.index(tool)), 2: 0.0}

    def make_tool(self, reg_id: str, name: str | None = None) -> FakeTool:
        n = sum(reg_id == t.ID for t in self.tools) + 1
        tool = FakeTool(
            self._root, self, reg_id, name or f"{reg_id}{n}", _DEFAULT_INPUTS.get(reg_id, {})
        )
        self.tools.append(tool)
        return tool

    @recorded
    def AddTool(self, reg_id: str, x: float = 0, y: float = 0) -> FakeTool | None:
        if reg_id not in _DEFAULT_INPUTS:
            return None
        return self.make_tool(reg_id)

    @recorded
    def FindTool(self, name: str) -> FakeTool | None:
        return next((t for t in self.tools if t.Name == name), None)

    @recorded
    def GetToolList(self, selected: bool = False) -> dict[int, FakeTool]:
        tools = [self.ActiveTool] if selected and self.ActiveTool else self.tools
        return dict(enumerate(tools, start=1))

    @recorded
    def Lock(self) -> None:
        self.lock_depth += 1

    @recorded
    def Unlock(self) -> None:
        self.lock_depth -= 1

    @recorded
    def StartUndo(self, name: str) -> None:
        self.open_undo += 1
        self.undo_log.append(name)

    @recorded
    def EndUndo(self, keep: bool) -> None:
        self.open_undo -= 1

    @recorded
    def GetUndoStack(self) -> dict[int, dict[str, str]]:
        return {i: {"Name": n} for i, n in enumerate(self.undo_log, start=1)}

    @recorded
    def GetAttrs(self) -> dict[str, Any]:
        return {
            "COMPN_RenderStart": 0,
            "COMPN_RenderEnd": 99,
            "COMPN_GlobalStart": 0,
            "COMPN_GlobalEnd": 120,
        }

    @recorded
    def Paste(self, text: str) -> bool:
        if self.paste_ok and "Tools" in text:
            self.make_tool("Background", "Pasted1")
        return True


def attach_fusion(item: FakeTimelineItem, names: list[str] | None = None) -> list[FakeComp]:
    """Give a fake timeline item working Fusion comp methods; returns its comps."""
    root = item._root
    comps = [FakeComp(root, n) for n in (names or [])]
    ctr = {"n": len(comps)}

    def GetFusionCompNameList() -> list[str]:
        return [c.name for c in comps]

    def GetFusionCompCount() -> int:
        return len(comps)

    def GetFusionCompByIndex(i: int) -> FakeComp | None:
        return comps[i - 1] if 1 <= i <= len(comps) else None

    def GetFusionCompByName(n: str) -> FakeComp | None:
        return next((c for c in comps if c.name == n), None)

    def AddFusionComp() -> FakeComp:
        ctr["n"] += 1
        comp = FakeComp(root, f"Composition {ctr['n']}")
        comps.append(comp)
        return comp

    def ImportFusionComp(path: str) -> FakeComp | None:
        if not Path(path).is_file():
            return None
        return AddFusionComp()

    def ExportFusionComp(path: str, index: int) -> bool:
        Path(path).write_text("{ Tools = ordered() {} }")
        return True

    def DeleteFusionCompByName(n: str) -> bool:
        comp = GetFusionCompByName(n)
        if comp is None:
            return False
        comps.remove(comp)
        return True

    def LoadFusionCompByName(n: str) -> FakeComp | None:
        return GetFusionCompByName(n)

    def RenameFusionCompByName(old: str, new: str) -> bool:
        comp = GetFusionCompByName(old)
        if comp is None:
            return False
        comp.name = new
        return True

    for fn in (
        GetFusionCompNameList,
        GetFusionCompCount,
        GetFusionCompByIndex,
        GetFusionCompByName,
        AddFusionComp,
        ImportFusionComp,
        ExportFusionComp,
        DeleteFusionCompByName,
        LoadFusionCompByName,
        RenameFusionCompByName,
    ):
        setattr(item, fn.__name__, fn)
    return comps


def attach_registry(resolve: FakeResolve, ids: list[str]) -> None:
    """Give ``resolve.fusion`` a GetRegSummary like the live one (index -> id)."""
    resolve.fusion.GetRegSummary = lambda: dict(enumerate(ids, start=1))
