# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for client registration (no real client is touched)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from davinci_mcp import install as inst
from davinci_mcp.__main__ import main


def test_desktop_merge_keeps_other_servers_and_backs_up(tmp_path: Path) -> None:
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    report = inst.configure_desktop("davinci-mcp", {"command": "dm", "args": ["serve"]}, path=cfg)
    assert report.status == "installed"
    data = json.loads(cfg.read_text())
    assert data["theme"] == "dark"
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["davinci-mcp"]["args"] == ["serve"]
    assert len(list(tmp_path.glob("claude_desktop_config.json.bak-*"))) == 1
    removed = inst.configure_desktop("davinci-mcp", None, path=cfg)
    assert removed.status == "removed"
    assert "davinci-mcp" not in json.loads(cfg.read_text())["mcpServers"]


def test_desktop_dry_run_and_invalid_json(tmp_path: Path) -> None:
    cfg = tmp_path / "c.json"
    assert inst.configure_desktop("n", {"command": "c"}, path=cfg, dry_run=True).status == (
        "would-install"
    )
    assert not cfg.exists()
    cfg.write_text("{broken")
    assert inst.configure_desktop("n", {"command": "c"}, path=cfg).status == "failed"
    assert inst.configure_desktop("absent", None, path=tmp_path / "none.json").status == "skipped"


def test_claude_code_uses_cli_idempotently() -> None:
    calls: list[list[str]] = []

    def runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    report = inst.configure_claude_code("davinci-mcp", {"command": "dm"}, runner=runner)
    assert report.status == "installed"
    assert calls[0][:4] == ["claude", "mcp", "remove", "--scope"]
    assert calls[1][:5] == ["claude", "mcp", "add-json", "--scope", "user"]
    assert json.loads(calls[1][-1]) == {"command": "dm"}
    assert inst.configure_claude_code("n", None, runner=runner).status == "removed"


def test_claude_code_failure_is_reported() -> None:
    def runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(cmd), 1, "", "boom")

    report = inst.configure_claude_code("n", {"command": "c"}, runner=runner)
    assert report.status == "failed" and report.detail == "boom"


def test_server_entry_points_at_this_install() -> None:
    entry = inst.server_entry()
    assert entry["args"][-1] == "serve"
    assert Path(entry["command"]).exists()


def test_cli_dry_run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(inst, "desktop_config_path", lambda: Path("/nonexistent/c.json"))
    assert main(["install", "--dry-run", "--target", "claude-desktop"]) == 0
    assert "would-install" in capsys.readouterr().out
