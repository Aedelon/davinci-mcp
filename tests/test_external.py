# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for davinci_mcp.connection.external."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from davinci_mcp.connection import external
from davinci_mcp.connection.external import (
    ExternalBackend,
    ResolvePaths,
    default_paths,
    find_resolve_process,
    is_headless,
    launch_resolve,
    resolve_paths,
)
from davinci_mcp.errors import DavinciError, ErrorCode


def test_default_paths_darwin() -> None:
    p = default_paths("darwin")
    assert p.app.name == "DaVinci Resolve.app"
    assert p.script_lib.name == "fusionscript.so"
    assert p.modules == p.script_api / "Modules"
    assert p.stub.name == "DaVinciResolveScript.pyi"


def test_default_paths_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", "/pd")
    p = default_paths("win32")
    assert p.script_lib.name == "fusionscript.dll"
    assert p.app.name == "Resolve.exe"
    assert "pd" in str(p.script_api)


def test_default_paths_linux() -> None:
    p = default_paths("linux")
    assert p.script_api == Path("/opt/resolve/Developer/Scripting")
    assert p.app == Path("/opt/resolve/bin/resolve")


def test_resolve_paths_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESOLVE_SCRIPT_API", "/x/api")
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", "/x/lib.so")
    p = resolve_paths()
    assert p.script_api == Path("/x/api")
    assert p.script_lib == Path("/x/lib.so")


def test_resolve_paths_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESOLVE_SCRIPT_API", "/x/api")
    p = resolve_paths(script_api=Path("/e/api"), script_lib=Path("/e/lib"))
    assert p.script_api == Path("/e/api")
    assert p.script_lib == Path("/e/lib")


def test_is_headless() -> None:
    assert is_headless(["Resolve", "-nogui"]) is True
    assert is_headless(["Resolve"]) is False
    assert is_headless(None) is False
    assert is_headless([]) is False


def _fake_run(stdout: str) -> Any:
    def run(*_a: Any, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(stdout=stdout)

    return run


def test_find_process_unix_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    app = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"
    out = f"/bin/zsh -l\n{app} -nogui\n"
    monkeypatch.setattr(subprocess, "run", _fake_run(out))
    argv = find_resolve_process()
    assert argv is not None
    assert "-nogui" in argv


def test_find_process_linux_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", _fake_run("/opt/resolve/bin/resolve\n"))
    assert find_resolve_process() == ["/opt/resolve/bin/resolve"]
    monkeypatch.setattr(subprocess, "run", _fake_run("/bin/zsh\n"))
    assert find_resolve_process() is None


def test_find_process_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", _fake_run('"Resolve.exe","123"'))
    assert find_resolve_process() == ["Resolve.exe"]
    monkeypatch.setattr(subprocess, "run", _fake_run("INFO: No tasks"))
    assert find_resolve_process() is None


@pytest.mark.parametrize(
    ("platform", "headless", "first"), [("darwin", True, "open"), ("linux", False, "/app")]
)
def test_launch_resolve(
    monkeypatch: pytest.MonkeyPatch, platform: str, headless: bool, first: str
) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **_k: captured.append(cmd))
    paths = ResolvePaths(Path("/api"), Path("/lib"), Path("/app"))
    launch_resolve(paths, headless=headless)
    assert captured[0][0] == first
    assert ("-nogui" in captured[0]) is headless


def test_launch_resolve_darwin_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **_k: captured.append(cmd))
    launch_resolve(ResolvePaths(Path("/api"), Path("/lib"), Path("/app")), headless=False)
    assert captured[0] == ["open", "-a", "/app"]


def test_backend_missing_lib(tmp_path: Path) -> None:
    backend = ExternalBackend(ResolvePaths(tmp_path, tmp_path / "missing.so", tmp_path))
    with pytest.raises(DavinciError) as info:
        backend.connect()
    assert info.value.code is ErrorCode.NOT_CONNECTED
    backend.close()


def test_backend_loads_module_and_connects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lib = tmp_path / "lib.so"
    lib.write_text("")
    module = SimpleNamespace(scriptapp=lambda name: f"app:{name}")
    monkeypatch.setattr(external.importlib, "import_module", lambda _n: module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("RESOLVE_SCRIPT_API", raising=False)
    monkeypatch.delenv("RESOLVE_SCRIPT_LIB", raising=False)
    backend = ExternalBackend(ResolvePaths(tmp_path, lib, tmp_path))
    assert backend.connect() == "app:Resolve"
    assert backend.connect() == "app:Resolve"  # cached module
    assert str(tmp_path / "Modules") in sys.path
    backend.close()
    monkeypatch.delenv("RESOLVE_SCRIPT_API", raising=False)
    monkeypatch.delenv("RESOLVE_SCRIPT_LIB", raising=False)
