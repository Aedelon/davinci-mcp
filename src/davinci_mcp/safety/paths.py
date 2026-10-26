# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Filesystem sandbox: only paths inside configured roots are accepted."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from pydantic import Field

from davinci_mcp.config import config_home, state_home
from davinci_mcp.errors import DavinciError, ErrorCode


def protected_paths() -> list[Path]:
    """Never readable or writable through client paths, whatever the allowed roots.

    The server's config (code-execution switch, recipe dirs), its state (consent.key signs
    confirmation tokens) and the MCP client configs (which servers run) must not be
    reachable by an agent, even when the allowed roots cover the home directory.
    """
    home = Path.home()
    return [
        p.expanduser().resolve()
        for p in (
            config_home(),
            state_home(),
            home / ".claude",
            home / ".claude.json",
            home / "Library/Application Support/Claude",
            home / ".config/Claude",
            home / "AppData/Roaming/Claude",
        )
    ]


def check_path(path: str | Path, roots: Sequence[Path], *, must_exist: bool = False) -> Path:
    """Return the resolved path if it lies inside one of ``roots``, else raise."""
    allowed = [str(r) for r in roots]
    raw = str(path)
    if "\0" in raw:
        raise DavinciError(
            ErrorCode.PATH_NOT_ALLOWED,
            "Path contains a null byte.",
            details={"allowed_roots": allowed},
        )
    resolved = Path(raw).expanduser().resolve()
    resolved_roots = [Path(r).expanduser().resolve() for r in roots]
    if not any(resolved.is_relative_to(r) for r in resolved_roots):
        raise DavinciError(
            ErrorCode.PATH_NOT_ALLOWED,
            f"Path is outside the allowed directories: {raw}",
            hint="Use a path inside one of the allowed roots.",
            details={"allowed_roots": [str(r) for r in resolved_roots], "path": str(resolved)},
        )
    for protected in protected_paths():
        if resolved == protected or resolved.is_relative_to(protected):
            raise DavinciError(
                ErrorCode.PATH_NOT_ALLOWED,
                f"Path is inside a protected location: {protected}",
                hint="Server/client configuration and state are never reachable by tools.",
                retryable=False,
            )
    if must_exist and not resolved.exists():
        raise DavinciError(ErrorCode.NOT_FOUND, f"Path does not exist: {resolved}")
    return resolved


SAFE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,127}$"

# A bare file name or prefix: no separators, cannot start with '.', so no traversal.
type SafeFileName = Annotated[
    str,
    Field(pattern=SAFE_NAME_PATTERN, description="Plain file name (no path separators)."),
]
