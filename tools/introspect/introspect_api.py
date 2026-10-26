# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Introspect the live DaVinci Resolve scripting API and diff it against README.txt.

Creates a throwaway project, populates it with a generated test clip so every
object type is reachable, records dir() of each object type plus reference
dumps (settings, properties, render formats), then deletes the throwaway
project and reloads the previously current one.

Usage:
    python introspect_api.py --out <dir> [--keep-project]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_API = Path(
    os.environ.get(
        "RESOLVE_SCRIPT_API",
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
    )
)
sys.path.append(str(SCRIPT_API / "Modules"))

import DaVinciResolveScript as dvr  # noqa: E402

SCRATCH_PROJECT = "zz_davinci_mcp_introspect"
README_CLASS_RE = re.compile(r"^([A-Z][A-Za-z]+)\s*$")
README_METHOD_RE = re.compile(r"^ {2}([A-Z][A-Za-z0-9]+)\(")


def parse_readme(path: Path) -> dict[str, list[str]]:
    """Return {ClassName: [documented method names]} from the official README."""
    classes: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if m := README_CLASS_RE.match(line):
            current = m.group(1)
            continue
        if current and (m := README_METHOD_RE.match(line)):
            classes.setdefault(current, [])
            if m.group(1) not in classes[current]:
                classes[current].append(m.group(1))
    return {k: v for k, v in classes.items() if v}


def parse_stub(path: Path) -> dict[str, list[str]]:
    """Return {ClassName: [method names]} from DaVinciResolveScript.pyi (21.1+)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                n.name
                for n in node.body
                if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
            ]
            if methods:
                classes[node.name] = sorted(set(methods))
    return classes


def documented_methods() -> dict[str, list[str]]:
    stub = SCRIPT_API / "DaVinciResolveScript.pyi"
    if stub.exists():
        return parse_stub(stub)
    return parse_readme(SCRIPT_API / "README.txt")


def public_dir(obj: Any) -> list[str]:
    return sorted(n for n in dir(obj) if not n.startswith("_"))


def safe(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:  # Resolve proxies raise arbitrary errors
        return f"<error: {exc!r}>"


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def make_test_media(workdir: Path) -> list[Path]:
    clip = workdir / "introspect_clip.mov"
    still = workdir / "introspect_still.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "1",
            "-c:a",
            "pcm_s16le",
            "-shortest",
            str(clip),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1080x1920",
            "-frames:v",
            "1",
            str(still),
        ],
        check=True,
    )
    return [clip, still]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep-project", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        print(
            "Cannot connect to Resolve (Studio + external scripting required).",
            file=sys.stderr,
        )
        return 1

    version = resolve.GetVersionString()
    objects: dict[str, Any] = {"Resolve": resolve}
    dumps: dict[str, Any] = {"product": resolve.GetProductName(), "version": version}

    pm = resolve.GetProjectManager()
    objects["ProjectManager"] = pm
    previous = pm.GetCurrentProject()
    previous_name = previous.GetName() if previous else None

    if SCRATCH_PROJECT in (pm.GetProjectListInCurrentFolder() or []):
        pm.DeleteProject(SCRATCH_PROJECT)
    project = pm.CreateProject(SCRATCH_PROJECT)
    if project is None:
        print("Failed to create scratch project.", file=sys.stderr)
        return 1

    try:
        objects["Project"] = project
        storage = resolve.GetMediaStorage()
        objects["MediaStorage"] = storage
        pool = project.GetMediaPool()
        objects["MediaPool"] = pool
        root = pool.GetRootFolder()
        objects["Folder"] = root

        with tempfile.TemporaryDirectory() as tmp:
            media = make_test_media(Path(tmp))
            items = storage.AddItemListToMediaPool([str(p) for p in media]) or []
            clip_item = next((i for i in items if i.GetName().endswith(".mov")), None)
            if clip_item:
                objects["MediaPoolItem"] = clip_item
                dumps["MediaPoolItem.GetClipProperty"] = safe(clip_item.GetClipProperty)
                dumps["MediaPoolItem.GetMetadata"] = safe(clip_item.GetMetadata)

                timeline = pool.CreateTimelineFromClips("introspect_tl", [clip_item])
                if timeline:
                    objects["Timeline"] = timeline
                    dumps["Timeline.GetSetting"] = safe(timeline.GetSetting)
                    video = timeline.GetItemListInTrack("video", 1) or []
                    audio = timeline.GetItemListInTrack("audio", 1) or []
                    if video:
                        item = video[0]
                        objects["TimelineItem"] = item
                        dumps["TimelineItem.GetProperty"] = safe(item.GetProperty)
                        graph = safe(item.GetNodeGraph)
                        if not isinstance(graph, str) and graph:
                            objects["Graph"] = graph
                        comp = safe(item.AddFusionComp)
                        if not isinstance(comp, str) and comp:
                            objects["FusionComposition"] = comp
                            tool = safe(comp.AddTool, "Background", -32768, -32768)
                            if not isinstance(tool, str) and tool:
                                objects["FusionTool"] = tool
                                dumps["FusionTool.GetInputList"] = safe(
                                    lambda: [
                                        i.GetAttrs().get("INPS_ID")
                                        for i in tool.GetInputList().values()
                                    ]
                                )
                                inputs = safe(lambda: list(tool.GetInputList().values()))
                                if isinstance(inputs, list) and inputs:
                                    objects["FusionInput"] = inputs[0]
                    if audio:
                        objects["TimelineItem(audio)"] = audio[0]

                    resolve.OpenPage("color")
                    still = safe(timeline.GrabStill)
                    if not isinstance(still, str) and still:
                        objects["GalleryStill"] = still
                    tl_graph = safe(timeline.GetNodeGraph)
                    if not isinstance(tl_graph, str) and tl_graph:
                        objects["Graph(timeline)"] = tl_graph
                    resolve.OpenPage("edit")

        gallery = project.GetGallery()
        objects["Gallery"] = gallery
        album = safe(gallery.GetCurrentStillAlbum)
        if not isinstance(album, str) and album:
            objects["GalleryStillAlbum"] = album
        group = safe(project.AddColorGroup, "introspect_group")
        if not isinstance(group, str) and group:
            objects["ColorGroup"] = group

        fusion = resolve.Fusion()
        objects["Fusion"] = fusion
        dumps["Fusion.GetRegSummary"] = safe(
            lambda: {k: v.get("REGS_Name") for k, v in fusion.GetRegSummary().items()}
        )

        dumps["Project.GetSetting"] = safe(project.GetSetting)
        dumps["Project.GetRenderFormats"] = safe(project.GetRenderFormats)
        formats = dumps["Project.GetRenderFormats"]
        if isinstance(formats, dict):
            dumps["Project.GetRenderCodecs"] = {
                ext: safe(project.GetRenderCodecs, ext) for ext in formats.values()
            }
        dumps["Project.GetRenderPresetList"] = safe(project.GetRenderPresetList)
        dumps["Project.GetRenderSettings"] = (
            safe(project.GetRenderSettings) if "GetRenderSettings" in public_dir(project) else None
        )
        dumps["Resolve.GetFairlightPresets"] = safe(resolve.GetFairlightPresets)
        dumps["MediaStorage.GetMountedVolumeList"] = safe(storage.GetMountedVolumeList)

        live = {name: public_dir(obj) for name, obj in objects.items()}
    finally:
        if not args.keep_project:
            # "Untitled Project" is transient and cannot be reloaded by name.
            if not (previous_name and pm.LoadProject(previous_name)):
                pm.CloseProject(project)
            if not pm.DeleteProject(SCRATCH_PROJECT):
                print(f"Warning: could not delete {SCRATCH_PROJECT}", file=sys.stderr)

    readme = documented_methods()
    diff: dict[str, Any] = {}
    for name, methods in live.items():
        cls = name.split("(")[0]
        documented = set(readme.get(cls, []))
        diff[name] = {
            "live_count": len(methods),
            "documented_count": len(documented),
            "undocumented": sorted(set(methods) - documented) if documented else None,
            "documented_missing_live": sorted(documented - set(methods)),
        }
    diff["_classes_documented_not_reached"] = sorted(set(readme) - {n.split("(")[0] for n in live})

    tag = version.replace(" ", "_")
    (args.out / f"live-dir-{tag}.json").write_text(json.dumps(live, indent=2))
    (args.out / f"readme-methods-{tag}.json").write_text(json.dumps(readme, indent=2))
    (args.out / f"diff-{tag}.json").write_text(json.dumps(diff, indent=2))
    (args.out / f"dumps-{tag}.json").write_text(json.dumps(jsonable(dumps), indent=2))
    print(
        json.dumps(
            {
                k: {kk: vv for kk, vv in v.items() if kk != "undocumented"}
                if isinstance(v, dict)
                else v
                for k, v in diff.items()
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
