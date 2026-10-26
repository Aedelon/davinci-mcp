# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Register davinci-mcp in MCP clients (Claude Code, Claude Desktop) and optionally the bridge.

Idempotent: re-running replaces the entry. Claude Desktop's config is merged (other
servers and keys are kept) after a timestamped backup; Claude Code goes through its
own CLI (``claude mcp add-json --scope user``) rather than editing its state file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

Target = Literal["claude-code", "claude-desktop"]
TARGETS: tuple[Target, ...] = ("claude-code", "claude-desktop")
type Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class TargetReport(BaseModel):
    target: Target
    status: Literal["installed", "removed", "skipped", "would-install", "would-remove", "failed"]
    detail: str


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(cmd), capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False
    )


def server_entry() -> dict[str, Any]:
    """stdio entry launching this installation's ``davinci-mcp serve``."""
    exe_name = "davinci-mcp.exe" if sys.platform == "win32" else "davinci-mcp"
    # Resolve the venv's bin dir (not the interpreter, which links to the base Python) so a
    # symlinked .venv inside a synced folder does not end up in client configs.
    exe = Path(sys.executable).parent.resolve() / exe_name
    if exe.is_file():
        return {"command": str(exe), "args": ["serve"]}
    return {"command": sys.executable, "args": ["-m", "davinci_mcp", "serve"]}


def desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def configure_desktop(
    name: str, entry: dict[str, Any] | None, *, path: Path | None = None, dry_run: bool = False
) -> TargetReport:
    """Add (``entry``) or remove (``None``) ``name`` in Claude Desktop's mcpServers."""
    config_path = path or desktop_config_path()
    try:
        data: dict[str, Any] = (
            json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        )
    except json.JSONDecodeError as exc:
        return TargetReport(
            target="claude-desktop", status="failed", detail=f"{config_path}: invalid JSON ({exc})"
        )
    servers: dict[str, Any] = data.setdefault("mcpServers", {})
    if entry is None and name not in servers:
        return TargetReport(target="claude-desktop", status="skipped", detail="not registered")
    if dry_run:
        status: Literal["would-install", "would-remove"] = (
            "would-remove" if entry is None else "would-install"
        )
        return TargetReport(target="claude-desktop", status=status, detail=str(config_path))
    backup = _backup(config_path)
    if entry is None:
        del servers[name]
    else:
        servers[name] = entry
    _write_json(config_path, data)
    note = f" (backup {backup.name})" if backup else ""
    return TargetReport(
        target="claude-desktop",
        status="removed" if entry is None else "installed",
        detail=f"{config_path}{note}; restart Claude Desktop to load it",
    )


def configure_claude_code(
    name: str, entry: dict[str, Any] | None, *, runner: Runner = _run, dry_run: bool = False
) -> TargetReport:
    """Register through the ``claude`` CLI at user scope (all projects)."""
    if shutil.which("claude") is None and runner is _run:
        return TargetReport(target="claude-code", status="skipped", detail="claude CLI not found")
    remove = ["claude", "mcp", "remove", "--scope", "user", name]
    if dry_run:
        return TargetReport(
            target="claude-code",
            status="would-remove" if entry is None else "would-install",
            detail="claude mcp add-json --scope user " + name,
        )
    runner(remove)  # idempotent: fails harmlessly when absent
    if entry is None:
        return TargetReport(target="claude-code", status="removed", detail="user scope")
    proc = runner(["claude", "mcp", "add-json", "--scope", "user", name, json.dumps(entry)])
    if proc.returncode != 0:
        return TargetReport(
            target="claude-code", status="failed", detail=(proc.stderr or proc.stdout).strip()
        )
    return TargetReport(target="claude-code", status="installed", detail="user scope")


def install(
    targets: Sequence[Target] = TARGETS,
    *,
    name: str = "davinci-mcp",
    dry_run: bool = False,
    remove: bool = False,
) -> list[TargetReport]:
    entry = None if remove else server_entry()
    reports: list[TargetReport] = []
    for target in targets:
        if target == "claude-desktop":
            reports.append(configure_desktop(name, entry, dry_run=dry_run))
        else:
            reports.append(configure_claude_code(name, entry, dry_run=dry_run))
    return reports
