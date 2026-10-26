# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for davinci_mcp.config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from davinci_mcp.config import load_settings


def test_defaults(tmp_path: Path) -> None:
    s = load_settings(path=tmp_path / "none.toml", environ={})
    assert s.connection.auto_launch is True
    assert s.connection.call_timeout_s == 30.0
    assert s.http.port == 8765
    assert s.safety.enable_code_execution is False
    assert s.safety.allowed_roots == [Path.home().resolve()]


def test_toml_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[connection]\nauto_launch = false\ncall_timeout_s = 5\n[http]\nport = 9000\n")
    s = load_settings(path=cfg, environ={})
    assert s.connection.auto_launch is False
    assert s.connection.call_timeout_s == 5
    assert s.http.port == 9000


def test_env_overrides_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[connection]\nauto_launch = true\n")
    s = load_settings(path=cfg, environ={"DAVINCI_MCP_CONNECTION__AUTO_LAUNCH": "0"})
    assert s.connection.auto_launch is False


def test_env_config_path(tmp_path: Path) -> None:
    cfg = tmp_path / "other.toml"
    cfg.write_text("[http]\nport = 1234\n")
    s = load_settings(environ={"DAVINCI_MCP_CONFIG": str(cfg)})
    assert s.http.port == 1234


def test_env_list_parsing(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    s = load_settings(
        path=tmp_path / "none.toml",
        environ={"DAVINCI_MCP_SAFETY__ALLOWED_ROOTS": f"[{a}, {b}]"},
    )
    assert s.safety.allowed_roots == [a.resolve(), b.resolve()]


def test_env_ignores_unrelated_and_flat_keys(tmp_path: Path) -> None:
    s = load_settings(
        path=tmp_path / "none.toml",
        environ={"PATH": "/bin", "DAVINCI_MCP_TOKEN": "x", "DAVINCI_MCP_HTTP__HOST": "0.0.0.0"},
    )
    assert s.http.host == "0.0.0.0"


def test_extra_keys_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[connection]\nbogus = 1\n")
    with pytest.raises(ValidationError):
        load_settings(path=cfg, environ={})
    with pytest.raises(ValidationError):
        load_settings(path=tmp_path / "none.toml", environ={"DAVINCI_MCP_NOPE__X": "1"})


def test_invalid_value_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_settings(path=tmp_path / "none.toml", environ={"DAVINCI_MCP_HTTP__PORT": "0"})


def test_default_state_paths_use_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    s = load_settings(environ={})
    assert s.state_dir == tmp_path / "davinci-mcp"
    assert s.safety.snapshot_dir == tmp_path / "davinci-mcp" / "snapshots"
