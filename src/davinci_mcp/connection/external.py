# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""External scripting backend (Studio): loads DaVinciResolveScript / fusionscript."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from davinci_mcp.errors import DavinciError, ErrorCode

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import Channel, ResolveObject


@dataclass(frozen=True)
class ResolvePaths:
    script_api: Path
    script_lib: Path
    app: Path

    @property
    def modules(self) -> Path:
        return self.script_api / "Modules"

    @property
    def stub(self) -> Path:
        return self.script_api / "DaVinciResolveScript.pyi"


def default_paths(platform: str = sys.platform) -> ResolvePaths:
    """Install locations documented in the Resolve 21.1 scripting README."""
    if platform == "darwin":
        app = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")
        return ResolvePaths(
            script_api=Path(
                "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
            ),
            script_lib=app / "Contents/Libraries/Fusion/fusionscript.so",
            app=app,
        )
    if platform == "win32":
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        install = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve")
        return ResolvePaths(
            script_api=program_data
            / "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting",
            script_lib=install / "fusionscript.dll",
            app=install / "Resolve.exe",
        )
    return ResolvePaths(
        script_api=Path("/opt/resolve/Developer/Scripting"),
        script_lib=Path("/opt/resolve/libs/Fusion/fusionscript.so"),
        app=Path("/opt/resolve/bin/resolve"),
    )


def resolve_paths(script_api: Path | None = None, script_lib: Path | None = None) -> ResolvePaths:
    base = default_paths()
    api = script_api or Path(os.environ.get("RESOLVE_SCRIPT_API", base.script_api))
    lib = script_lib or Path(os.environ.get("RESOLVE_SCRIPT_LIB", base.script_lib))
    return ResolvePaths(script_api=api, script_lib=lib, app=base.app)


def find_resolve_process() -> list[str] | None:
    """Return the argv of the running Resolve process, or None. Never launches anything."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/FO", "CSV", "/NH"],  # noqa: S607
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        ).stdout
        return ["Resolve.exe"] if "Resolve.exe" in out else None
    pattern = (
        "DaVinci Resolve.app/Contents/MacOS/Resolve"
        if sys.platform == "darwin"
        else "/opt/resolve/bin/resolve"
    )
    out = subprocess.run(
        ["ps", "-axo", "args="],  # noqa: S607
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    ).stdout
    for line in out.splitlines():
        if pattern in line:
            return line.split()
    return None


def is_headless(argv: list[str] | None) -> bool:
    """The API cannot tell headless from GUI; only the process argv can."""
    return bool(argv) and "-nogui" in (argv or [])


def launch_resolve(paths: ResolvePaths, *, headless: bool) -> None:
    """Start Resolve detached. Callers must check ``find_resolve_process`` first."""
    args = ["-nogui"] if headless else []
    if sys.platform == "darwin":
        cmd = ["open", "-a", str(paths.app), *(["--args", *args] if args else [])]
    else:
        cmd = [str(paths.app), *args]
    subprocess.Popen(  # noqa: S603
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class ExternalBackend:
    channel: Channel = "external"

    def __init__(self, paths: ResolvePaths) -> None:
        self._paths = paths
        self._module: ModuleType | None = None

    def _load_module(self) -> ModuleType:
        if self._module is not None:
            return self._module
        if not self._paths.script_lib.exists():
            raise DavinciError(
                ErrorCode.NOT_CONNECTED,
                f"fusionscript library not found at {self._paths.script_lib}.",
                hint="Install DaVinci Resolve or set RESOLVE_SCRIPT_LIB.",
                retryable=False,
            )
        os.environ.setdefault("RESOLVE_SCRIPT_API", str(self._paths.script_api))
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", str(self._paths.script_lib))
        modules = str(self._paths.modules)
        if modules not in sys.path:
            sys.path.append(modules)
        self._module = importlib.import_module("DaVinciResolveScript")
        return self._module

    def connect(self) -> ResolveObject:
        module = self._load_module()
        return module.scriptapp("Resolve")

    def close(self) -> None:
        # fusionscript keeps no closable handle; dropping references is enough.
        self._module = None
