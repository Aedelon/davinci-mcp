# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Live smoke test of davinci-mcp against a running Resolve Studio, in a throwaway project.

Usage (the work dir is the only allowed root and also hosts the state dir):
    W=$(mktemp -d); XDG_STATE_HOME=$W/state DAVINCI_MCP_SAFETY__ALLOWED_ROOTS="[$W]" \
        uv run python tools/live_smoke.py $W

Creates project "zz_dmcp_smoke", exercises one action per domain, then reloads the
previous project and deletes the throwaway one.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client

from davinci_mcp.server import build_server

SCRATCH = Path(sys.argv[1])
PROJECT = "zz_dmcp_smoke"
results: list[tuple[str, bool, str]] = []


async def run(
    c: Client[Any], label: str, tool: str, args: dict[str, Any], *, confirm: bool = False
) -> dict[str, Any]:
    r = await c.call_tool(tool, args, raise_on_error=False)
    out = r.structured_content or {}
    if confirm and not r.is_error and out.get("status") == "planned":
        r = await c.call_tool(
            tool, {**args, "confirm_token": out["plan"]["token"]}, raise_on_error=False
        )
        out = r.structured_content or {}
    ok = not r.is_error
    receipt = out.get("receipt") or {}
    if receipt.get("verified") is False:
        ok = False
    note = "" if ok else json.dumps(out, default=str)[:300]
    images = sum(1 for b in r.content if getattr(b, "type", "") == "image")
    st = await c.call_tool("resolve_status", {}, raise_on_error=False)
    page = (st.structured_content or {}).get("page")
    note = f"[page={page}] " + note
    results.append((label, ok, note + (f" [{images} image(s)]" if images else "")))
    return out


def media() -> list[str]:
    clips = []
    for i, color in enumerate(["red", "blue"]):
        path = SCRATCH / f"smoke_{i}.mov"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=1920x1080:r=25:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-c:v",
                "prores_ks",
                "-c:a",
                "pcm_s16le",
                "-shortest",
                str(path),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
        )
        clips.append(str(path))
    return clips


async def main() -> None:
    paths = media()
    async with Client(build_server()) as c:
        status = await run(c, "status", "resolve_status", {})
        previous = status.get("project")
        await run(
            c,
            "create project",
            "project_write",
            {"op": {"action": "create_project", "name": PROJECT, "discard_untitled": True}},
        )
        await run(
            c,
            "import media",
            "media_write",
            {"op": {"action": "import_media", "items": [{"path": x} for x in paths]}},
        )
        await run(c, "list clips", "media_read", {"op": {"action": "list_clips"}})
        clips = await run(c, "list clips (ids)", "media_read", {"op": {"action": "list_clips"}})
        ids = [cl["id"] for cl in (clips.get("result", {}).get("clips") or [])]
        await run(
            c,
            "timeline from clips",
            "timeline_write",
            {
                "op": {
                    "action": "create_timeline_from_clips",
                    "name": "smoke_tl",
                    "clips": [{"clip": {"id": i}} for i in ids],
                }
            },
        )
        await run(c, "timeline info", "timeline_read", {"op": {"action": "timeline_info"}})
        await run(c, "list items", "timeline_read", {"op": {"action": "list_items"}})
        await run(
            c,
            "add marker",
            "timeline_write",
            {"op": {"action": "add_marker", "frame": 12, "color": "Green", "name": "beat"}},
        )
        await run(
            c,
            "delete marker (consent)",
            "timeline_delete",
            {"op": {"action": "delete_marker", "frame": 12}},
            confirm=True,
        )
        item = {"track": {"type": "video", "index": 1}, "position": 1}
        await run(
            c, "item properties", "item_read", {"op": {"action": "get_properties", "item": item}}
        )
        await run(
            c,
            "set zoom",
            "item_write",
            {"op": {"action": "set_properties", "items": [item], "properties": {"ZoomX": 1.2}}},
        )
        await run(
            c,
            "node graph",
            "color_read",
            {"op": {"action": "get_node_graph", "target": {"item": item}}},
        )
        await run(c, "render formats", "render_read", {"op": {"action": "list_render_formats"}})
        await run(
            c, "export frame", "vision_read", {"op": {"action": "export_frame", "max_width": 480}}
        )
        await run(
            c, "probe media", "vision_read", {"op": {"action": "probe_media", "path": paths[0]}}
        )
        await run(
            c, "fusion add comp", "fusion_write", {"op": {"action": "add_comp", "item": item}}
        )
        await run(
            c, "fusion comp info", "fusion_read", {"op": {"action": "comp_info", "item": item}}
        )
        await run(
            c,
            "snapshot timeline",
            "snapshot_write",
            {"op": {"action": "snapshot_timeline", "label": "smoke"}},
        )
        await run(
            c,
            "export fcpxml",
            "timeline_write",
            {
                "op": {
                    "action": "export_timeline",
                    "path": str(SCRATCH / "smoke.fcpxml"),
                    "format": "fcpxml_1_10",
                }
            },
        )
        await run(c, "app info", "app_read", {"op": {"action": "get_app_info"}})
        await run(
            c, "loudness", "audio_read", {"op": {"action": "analyze_loudness", "path": paths[0]}}
        )
        # cleanup
        if previous and previous != "Untitled Project":
            await run(
                c,
                "reload previous",
                "project_write",
                {"op": {"action": "load_project", "name": previous}},
            )
        else:
            await run(c, "close smoke", "project_write", {"op": {"action": "close_project"}})
        await run(
            c,
            "delete smoke project",
            "project_delete",
            {"op": {"action": "delete_project", "name": PROJECT}},
            confirm=True,
        )
    for label, ok, note in results:
        print(("OK  " if ok else "FAIL") + f"  {label}  {note}")
    print(f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")


asyncio.run(main())
