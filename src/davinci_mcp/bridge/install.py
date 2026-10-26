# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Install the in-app bridge script into Resolve's per-user scripts folder."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SCRIPT_NAME = "davinci_mcp_bridge.py"


def default_scripts_dir(platform: str = sys.platform) -> Path:
    if platform == "darwin":
        return (
            Path.home()
            / "Library/Application Support/Blackmagic Design/DaVinci Resolve"
            / "Fusion/Scripts/Utility"
        )
    if platform == "win32":
        appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        return appdata / "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility"
    return Path.home() / ".local/share/DaVinciResolve/Fusion/Scripts/Utility"


def install_bridge_script(target_dir: Path | None = None) -> Path:
    """Copy ``resolve_bridge.py`` as ``davinci_mcp_bridge.py``; return the installed path."""
    directory = target_dir or default_scripts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / SCRIPT_NAME
    shutil.copyfile(Path(__file__).with_name("resolve_bridge.py"), dest)
    return dest
