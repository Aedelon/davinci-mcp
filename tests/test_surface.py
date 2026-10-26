# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the API surface loader and search."""

from __future__ import annotations

from davinci_mcp.api.surface import ApiSurface, load_surface, search

SURFACE = ApiSurface.model_validate(
    {
        "resolve_version": "1.0",
        "classes": {
            "Timeline": {
                "methods": {
                    "GetName": {"returns": "str", "doc": "Returns the timeline name"},
                    "AddMarker": {
                        "params": [{"name": "frameId", "annotation": "int", "default": "1"}],
                        "returns": "bool",
                        "doc": "Adds a marker; see GetName",
                    },
                }
            },
            "Project": {"methods": {"GetName": {"returns": "str", "doc": "Project name"}}},
        },
    }
)


def test_exact_name_first() -> None:
    hits = search(SURFACE, "getname")
    assert [h.qualified_name for h in hits[:2]] == ["Project.GetName", "Timeline.GetName"]


def test_doc_match_ranked_last_and_limit() -> None:
    hits = search(SURFACE, "name")
    assert hits[-1].qualified_name == "Timeline.AddMarker"
    assert len(search(SURFACE, "name", limit=1)) == 1


def test_signature() -> None:
    hit = search(SURFACE, "AddMarker")[0]
    assert hit.signature == "Timeline.AddMarker(frameId: int = 1) -> bool"


def test_load_real_surface() -> None:
    surface = load_surface()
    assert "TimelineItem" in surface.classes
    assert search(surface, "AddTransition")
