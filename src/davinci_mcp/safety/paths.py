# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Filesystem sandbox: only paths inside configured roots are accepted."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from davinci_mcp.errors import DavinciError, ErrorCode


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
    if must_exist and not resolved.exists():
        raise DavinciError(ErrorCode.NOT_FOUND, f"Path does not exist: {resolved}")
    return resolved
