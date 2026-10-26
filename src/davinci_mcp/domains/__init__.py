# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Domain action modules. Each module exposes ``ACTIONS: list[ActionSpec]``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from davinci_mcp.domains import (
    ai,
    app,
    audio,
    code,
    color,
    fusion,
    gallery,
    interchange,
    item,
    media,
    project,
    render,
    snapshots,
    timeline,
    ui,
    vision,
    workflow,
)

if TYPE_CHECKING:
    from davinci_mcp.actions.spec import ActionSpec

DOMAIN_MODULES = (
    ai,
    app,
    audio,
    code,
    color,
    fusion,
    gallery,
    interchange,
    item,
    media,
    project,
    render,
    snapshots,
    timeline,
    ui,
    vision,
    workflow,
)


def all_actions() -> list[ActionSpec]:
    return [spec for module in DOMAIN_MODULES for spec in module.ACTIONS]
