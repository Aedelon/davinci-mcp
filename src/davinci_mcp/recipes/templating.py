# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Minimal, non-executable templates for YAML recipes.

``"{{ path }}"`` alone in a string yields the raw value (list, dict, number...);
inside a longer string it is interpolated as text. A path is dotted names with
optional ``[n]`` indexes and ``[*]`` projections, rooted at ``params`` or ``steps``:
``steps.import.receipt.result.imported[*].id``. Nothing else is evaluated.
"""

from __future__ import annotations

import re
from typing import Any, cast

from davinci_mcp.errors import DavinciError, ErrorCode

_WHOLE = re.compile(r"^\{\{\s*([^{}]+?)\s*\}\}$")
_PART = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)((?:\[(?:\*|\d+)\])*)$")
_INDEX = re.compile(r"\[(\*|\d+)\]")
_ROOTS = frozenset({"params", "steps"})


def _fail(path: str, why: str) -> DavinciError:
    return DavinciError(
        ErrorCode.INVALID_PARAMS, f"Template {{{{ {path} }}}}: {why}.", retryable=False
    )


def _get(value: object, key: str, path: str) -> object:
    if isinstance(value, dict) and key in value:
        return cast("dict[str, object]", value)[key]
    raise _fail(path, f"no key {key!r}")


def resolve_path(path: str, context: dict[str, Any]) -> object:
    segments = path.split(".")
    if not segments or segments[0].split("[")[0] not in _ROOTS:
        raise _fail(path, "must start with params or steps")
    values: list[object] = [context]
    projected = False
    for raw in segments:
        match = _SEGMENT.match(raw)
        if match is None:
            raise _fail(path, f"invalid segment {raw!r}")
        name, indexes = match.groups()
        values = [_get(v, name, path) for v in values]
        for index in _INDEX.findall(indexes):
            nxt: list[object] = []
            for v in values:
                if not isinstance(v, list):
                    raise _fail(path, f"{name} is not a list")
                items = cast("list[object]", v)
                if index == "*":
                    nxt.extend(items)
                    projected = True
                else:
                    position = int(index)
                    if position >= len(items):
                        raise _fail(path, f"index {position} out of range")
                    nxt.append(items[position])
            values = nxt
    return values if projected else values[0]


def render(value: object, context: dict[str, Any]) -> object:
    """Render templates recursively in strings, lists and dicts."""
    if isinstance(value, str):
        whole = _WHOLE.match(value)
        if whole is not None:
            return resolve_path(whole.group(1), context)
        return _PART.sub(lambda m: str(resolve_path(m.group(1), context)), value)
    if isinstance(value, list):
        return [render(v, context) for v in cast("list[object]", value)]
    if isinstance(value, dict):
        return {str(k): render(v, context) for k, v in cast("dict[object, object]", value).items()}
    return value
