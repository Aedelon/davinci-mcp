# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the schema generator (run on an inline sample stub)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import gen_schemas

from tests.test_stub_parser import SAMPLE


@pytest.fixture
def generated(tmp_path: Path) -> tuple[ModuleType, Path]:
    stub = tmp_path / "s.pyi"
    stub.write_text(SAMPLE, encoding="utf-8")
    out = tmp_path / "gen"
    gen_schemas.generate(stub, out, "9.9.9")
    path = out / "resolve_9_9.py"
    spec = importlib.util.spec_from_file_location("gen_sample_mod", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_sample_mod"] = mod
    spec.loader.exec_module(mod)
    return mod, out


def test_module_exports(generated: tuple[ModuleType, Path]) -> None:
    mod, _ = generated
    assert mod.RESOLVE_VERSION == "9.9.9"
    assert set(mod.MODELS) == {"Opts", "Props", "Strict"}
    assert mod.CONSTANT_ENUMS == {"Mode": ("MODE_A", "MODE_B")}


def test_alias_and_forbid(generated: tuple[ModuleType, Path]) -> None:
    mod, _ = generated
    props = mod.Props.model_validate({"Clip Name": "x", "in": 3})
    assert props.in_ == 3
    assert props.model_dump(exclude_none=True) == {"Clip Name": "x", "in": 3}
    with pytest.raises(ValidationError):
        mod.Props.model_validate({"bogus": 1})
    with pytest.raises(ValidationError):
        mod.Strict.model_validate({})


def test_object_field_marker(generated: tuple[ModuleType, Path]) -> None:
    mod, _ = generated
    schema = mod.Opts.model_json_schema()
    assert schema["properties"]["item"]["x-resolve-object"] == "Thing"


def test_surface_json(generated: tuple[ModuleType, Path]) -> None:
    _, out = generated
    data = json.loads((out / "api_surface_9_9.json").read_text())
    assert data["resolve_version"] == "9.9.9"
    assert "Foo" in data["classes"]["Thing"]["methods"]
    assert data["typed_dicts"]["Props"]["fields"]["Clip Name"]["doc"] == "User clip name"
