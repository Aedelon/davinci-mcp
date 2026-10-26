# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Domain action modules. Each module exposes ``ACTIONS: list[ActionSpec]``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from davinci_mcp.domains import snapshots, timeline

if TYPE_CHECKING:
    from davinci_mcp.actions.spec import ActionSpec

DOMAIN_MODULES = (snapshots, timeline)


def all_actions() -> list[ActionSpec]:
    return [spec for module in DOMAIN_MODULES for spec in module.ACTIONS]
