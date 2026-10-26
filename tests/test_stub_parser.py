# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the Resolve stub parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from davinci_mcp.api.stub_parser import StubModel, parse_stub

REAL_STUB = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Developer/Scripting/DaVinciResolveScript.pyi"
)

SAMPLE = '''\
from typing import TypedDict, Literal, TypeAlias

Color = Literal['Red', 'Blue']

Mode: TypeAlias = float
"""One of the resolve.* constants: MODE_A, MODE_B"""

class Opts(TypedDict, total=False):
\tname: str
\t"""The name"""
\tcolor: Color
\tmode: Mode
\t"""Default: resolve.MODE_A"""
\titem: 'Thing'

Props = TypedDict('Props', {
\t'Clip Name': str,  # User clip name
\t'in': int,
}, total=False)

Strict = TypedDict('Strict', {
\t'a': int,  # field a
})

class Thing:
\t"""A thing."""
\tMODE_A: Mode
\tMODE_B: Mode
\tdef Foo(self, a: str, b: int | None = None, c = False) -> bool:
\t\t"""Does foo"""
\t\t...
\tdef _private(self) -> None: ...
\tdef __dunder__(self) -> None: ...

class Empty: ...
'''


@pytest.fixture
def sample(tmp_path: Path) -> StubModel:
    p = tmp_path / "s.pyi"
    p.write_text(SAMPLE, encoding="utf-8")
    return parse_stub(p)


def test_literal_and_enum(sample: StubModel) -> None:
    assert sample.literal("Color") is not None
    assert sample.literal("Color").values == ("Red", "Blue")  # type: ignore[union-attr]
    enum = sample.constant_enum("Mode")
    assert enum is not None
    assert enum.constants == ("MODE_A", "MODE_B")


def test_class_form_typed_dict(sample: StubModel) -> None:
    td = sample.typed_dict("Opts")
    assert td is not None
    assert td.total is False
    assert [f.key for f in td.fields] == ["name", "color", "mode", "item"]
    assert td.fields[0].doc == "The name"
    assert td.fields[1].doc is None
    assert td.fields[3].annotation == "'Thing'"


def test_functional_typed_dict(sample: StubModel) -> None:
    td = sample.typed_dict("Props")
    assert td is not None
    assert td.total is False
    assert td.fields[0].key == "Clip Name"
    assert td.fields[0].doc == "User clip name"
    assert td.fields[1].doc is None
    strict = sample.typed_dict("Strict")
    assert strict is not None
    assert strict.total is True


def test_class_spec(sample: StubModel) -> None:
    thing = sample.cls("Thing")
    assert thing is not None
    assert thing.doc == "A thing."
    assert thing.constants == {"MODE_A": "Mode", "MODE_B": "Mode"}
    assert [m.name for m in thing.methods] == ["Foo"]
    foo = thing.methods[0]
    assert [(p.name, p.annotation, p.default) for p in foo.params] == [
        ("a", "str", None),
        ("b", "int | None", "None"),
        ("c", None, "False"),
    ]
    assert foo.returns == "bool"
    assert foo.doc == "Does foo"
    empty = sample.cls("Empty")
    assert empty is not None
    assert empty.methods == ()


@pytest.mark.skipif(not REAL_STUB.is_file(), reason="Resolve stub not installed")
def test_real_stub() -> None:
    model = parse_stub(REAL_STUB)
    assert len(model.classes) >= 16
    assert len(model.typed_dicts) >= 40
    item = model.cls("TimelineItem")
    assert item is not None
    assert any(m.name == "AddTransition" for m in item.methods)
    assert model.typed_dict("ProjectSettings") is not None
