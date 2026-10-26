# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Repair near-miss arguments before refusing them.

Agents often send almost-right parameters: a bare string where an object selector is
expected, ``fade_out`` for ``FadeOut``, ``Volume`` for ``AudioVolume``, or an item id
where a wrapper object (``{"item": {...}}``) is expected. Each costs a round trip.
``repair`` rewrites such input only along the model's own fields and reports every
rewrite, so the caller can surface it. It runs only after strict validation failed:
valid calls are never touched.
"""

from __future__ import annotations

import difflib
import re
import types
from typing import Any, Union, cast, get_args, get_origin

from pydantic import BaseModel

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MIN_SUBSTRING = 3


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def field_models(annotation: object) -> list[type[BaseModel]]:
    """BaseModel classes a field accepts, directly, optionally or as list items."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    origin = get_origin(annotation)
    if origin is not None:  # Optional, Union, list, tuple...
        return [m for arg in get_args(annotation) for m in field_models(arg)]
    return []


def is_list_field(annotation: object) -> bool:
    origin = get_origin(annotation)
    if origin in (list, tuple):
        return True
    return origin in (Union, types.UnionType) and any(
        is_list_field(a) for a in get_args(annotation)
    )


def _match_field(key: str, model: type[BaseModel], taken: set[str]) -> str | None:
    """The single field ``key`` most plausibly means, or None when unsure."""
    fields = {name: _norm(name) for name in model.model_fields if name not in taken}
    wanted = _norm(key)
    if not wanted:
        return None
    exact = [name for name, n in fields.items() if n == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(wanted) >= _MIN_SUBSTRING:
        inside = [name for name, n in fields.items() if wanted in n or n in wanted]
        if len(inside) == 1:
            return inside[0]
    close = difflib.get_close_matches(wanted, list(fields.values()), n=2, cutoff=0.8)
    if len(close) == 1:
        return next(name for name, n in fields.items() if n == close[0])
    return None


def _selector(value: str, model: type[BaseModel]) -> dict[str, Any] | None:
    """A bare string for a selector model: a UUID is an id, anything else a name."""
    fields = model.model_fields
    if _UUID_RE.match(value) and "id" in fields:
        return {"id": value}
    if "name" in fields:
        return {"name": value}
    return None


def _wrap(data: dict[str, Any], model: type[BaseModel]) -> tuple[str, dict[str, Any]] | None:
    """``data`` belongs one level down: exactly one field's model knows all its keys."""
    owners = [
        name
        for name, info in model.model_fields.items()
        if any(set(data) <= set(sub.model_fields) for sub in field_models(info.annotation))
    ]
    return (owners[0], {owners[0]: data}) if len(owners) == 1 else None


def _repair_value(value: object, annotation: object, path: str, notes: list[str]) -> object:
    models = field_models(annotation)
    if not models:
        return value
    if isinstance(value, list) and is_list_field(annotation):
        items = cast("list[Any]", value)
        return [_repair_value(v, models[0], f"{path}[{i}]", notes) for i, v in enumerate(items)]
    if isinstance(value, str) and len(models) == 1:
        fixed = _selector(value, models[0])
        if fixed is not None:
            notes.append(f"{path}: read {value!r} as {fixed}")
            return fixed
        return value
    if isinstance(value, dict) and len(models) == 1:
        return repair(models[0], cast("dict[str, Any]", value), notes, path)
    return cast("object", value)


def repair(
    model: type[BaseModel], data: dict[str, Any], notes: list[str], path: str = "op"
) -> dict[str, Any]:
    """Return a repaired copy of ``data`` for ``model``; append one note per rewrite."""
    fields = model.model_fields
    unknown = [k for k in data if k not in fields]
    out = {k: v for k, v in data.items() if k in fields}
    if unknown and not out and (wrapped := _wrap(data, model)):
        name, data = wrapped
        notes.append(f"{path}: moved {sorted(data[name])} under {name!r}")
        out, unknown = data, []
    for key in unknown:
        target = _match_field(key, model, set(out))
        if target is None:
            out[key] = data[key]  # let validation report it
            continue
        notes.append(f"{path}: read {key!r} as {target!r}")
        out[target] = data[key]
    for key, value in list(out.items()):
        if key in fields:
            out[key] = _repair_value(value, fields[key].annotation, f"{path}.{key}", notes)
    return out
