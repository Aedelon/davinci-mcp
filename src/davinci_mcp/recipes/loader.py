# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Discover recipes: built-in YAML, user YAML, and (opt-in) user Python modules."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from davinci_mcp.recipes.model import PythonRecipe, Recipe, YamlRecipe


@dataclass
class LoadedRecipes:
    recipes: dict[str, Recipe] = field(default_factory=dict[str, Recipe])
    sources: dict[str, str] = field(default_factory=dict[str, str])
    errors: list[str] = field(default_factory=list[str])

    def add(self, recipe: Recipe, source: str) -> None:
        if recipe.name in self.recipes:
            self.errors.append(f"{source}: duplicate recipe name {recipe.name!r} ignored")
            return
        self.recipes[recipe.name] = recipe
        self.sources[recipe.name] = source


def _load_yaml(text: str, source: str, out: LoadedRecipes) -> None:
    try:
        data: Any = yaml.safe_load(text)
        out.add(YamlRecipe.model_validate(data), source)
    except (yaml.YAMLError, ValidationError) as exc:
        out.errors.append(f"{source}: {exc}".splitlines()[0])


def _load_python(path: Path, out: LoadedRecipes) -> None:
    spec = importlib.util.spec_from_file_location(f"davinci_mcp_recipe_{path.stem}", path)
    if spec is None or spec.loader is None:
        out.errors.append(f"{path}: cannot import")
        return
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken user recipe must not take the server down
        out.errors.append(f"{path}: {type(exc).__name__}: {exc}")
        return
    recipe = getattr(module, "RECIPE", None)
    if not isinstance(recipe, PythonRecipe):
        out.errors.append(f"{path}: no RECIPE = PythonRecipe(...)")
        return
    out.add(recipe, str(path))


def load_recipes(dirs: list[Path], *, allow_python: bool) -> LoadedRecipes:
    out = LoadedRecipes()
    builtin = resources.files("davinci_mcp.recipes") / "builtin"
    for entry in sorted(builtin.iterdir(), key=lambda e: e.name):
        if entry.name.endswith((".yaml", ".yml")):
            _load_yaml(entry.read_text(encoding="utf-8"), f"builtin:{entry.name}", out)
    for directory in dirs:
        folder = directory.expanduser()
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix in {".yaml", ".yml"}:
                _load_yaml(path.read_text(encoding="utf-8"), str(path), out)
            elif path.suffix == ".py" and allow_python:
                _load_python(path, out)
            elif path.suffix == ".py":
                out.errors.append(f"{path}: Python recipes need recipes.allow_python = true")
    return out
