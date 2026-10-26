# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Load and search the generated Resolve API surface (classes, methods, typed dicts)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class ParamSurface(BaseModel):
    name: str
    annotation: str | None = None
    default: str | None = None


class MethodSurface(BaseModel):
    params: list[ParamSurface] = Field(default_factory=lambda: [])
    returns: str | None = None
    doc: str | None = None


class ClassSurface(BaseModel):
    doc: str | None = None
    constants: dict[str, str] = Field(default_factory=lambda: {})
    methods: dict[str, MethodSurface] = Field(default_factory=lambda: {})


class FieldSurface(BaseModel):
    annotation: str
    doc: str | None = None


class TypedDictSurface(BaseModel):
    total: bool = True
    fields: dict[str, FieldSurface] = Field(default_factory=lambda: {})


class ApiSurface(BaseModel):
    resolve_version: str
    classes: dict[str, ClassSurface] = Field(default_factory=lambda: {})
    typed_dicts: dict[str, TypedDictSurface] = Field(default_factory=lambda: {})
    literals: dict[str, list[str]] = Field(default_factory=lambda: {})
    constant_enums: dict[str, list[str]] = Field(default_factory=lambda: {})


class SearchHit(BaseModel):
    qualified_name: str
    signature: str
    doc: str | None = None


def load_surface(path: Path | None = None) -> ApiSurface:
    """Load the surface JSON (defaults to the latest generated one)."""
    if path is None:
        from davinci_mcp.api._generated import LATEST_SURFACE_PATH  # noqa: PLC0415

        path = LATEST_SURFACE_PATH
    return ApiSurface.model_validate_json(path.read_text(encoding="utf-8"))


def _signature(cls: str, name: str, method: MethodSurface) -> str:
    params: list[str] = []
    for p in method.params:
        text = p.name
        if p.annotation:
            text += f": {p.annotation}"
        if p.default is not None:
            text += f" = {p.default}" if p.annotation else f"={p.default}"
        params.append(text)
    sig = f"{cls}.{name}({', '.join(params)})"
    return f"{sig} -> {method.returns}" if method.returns else sig


def search(surface: ApiSurface, pattern: str, limit: int = 20) -> list[SearchHit]:
    """Case-insensitive search over ``Class.Method`` names and docs; exact names first."""
    needle = pattern.strip().lower()
    if not needle:
        return []
    ranked: list[tuple[int, str, SearchHit]] = []
    for cls_name, cls in surface.classes.items():
        for name, method in cls.methods.items():
            qualified = f"{cls_name}.{name}"
            q_low = qualified.lower()
            if needle in (q_low, name.lower()):
                rank = 0
            elif needle in q_low:
                rank = 1
            elif method.doc and needle in method.doc.lower():
                rank = 2
            else:
                continue
            hit = SearchHit(
                qualified_name=qualified,
                signature=_signature(cls_name, name, method),
                doc=method.doc,
            )
            ranked.append((rank, qualified, hit))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [h for _, _, h in ranked[:limit]]
