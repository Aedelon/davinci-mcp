# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Fakes for the color and gallery domains, layered on ``fake_resolve``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.fakes.fake_resolve import FakeObject, FakeResolve, FakeTimelineItem


class FakeGraph(FakeObject):
    live_class = "Graph"

    def __init__(self, root: FakeResolve, nodes: int = 3) -> None:
        super().__init__(root)
        self.labels = [f"Node {i}" for i in range(1, nodes + 1)]
        self.luts = [""] * nodes
        self.cache = [0] * nodes
        self.enabled = [True] * nodes
        self.lie = False  # return True without applying
        self.drx_ok = True
        self.reset_calls = 0

    def GetNumNodes(self) -> int:
        return len(self.labels)

    def GetNodeLabel(self, i: int) -> str:
        return self.labels[i - 1]

    def GetToolsInNode(self, i: int) -> list[str]:
        return ["Color Wheels"]

    def GetLUT(self, i: int) -> str:
        return self.luts[i - 1]

    def SetLUT(self, i: int, path: str) -> bool:
        if not self.lie:
            self.luts[i - 1] = path
        return True

    def GetNodeCacheMode(self, i: int) -> int:
        return self.cache[i - 1]

    def SetNodeCacheMode(self, i: int, mode: int) -> bool:
        if not self.lie:
            self.cache[i - 1] = mode
        return True

    def SetNodeEnabled(self, i: int, enabled: bool) -> bool:
        self.enabled[i - 1] = enabled
        return True

    def ApplyGradeFromDRX(self, path: str, mode: int, all_layers: bool = False) -> bool:
        return self.drx_ok

    def ResetAllGrades(self) -> bool:
        self.reset_calls += 1
        self.luts = [""] * len(self.luts)
        return True


class FakeColorGroup(FakeObject):
    live_class = "ColorGroup"

    def __init__(self, root: FakeResolve, name: str) -> None:
        super().__init__(root)
        self.name = name
        self.members: list[FakeTimelineItem] = []
        self.pre = FakeGraph(root, 1)
        self.post = FakeGraph(root, 1)

    def GetName(self) -> str:
        return self.name

    def SetName(self, name: str) -> bool:
        self.name = name
        return True

    def GetClipsInTimeline(self, timeline: Any = None) -> list[FakeTimelineItem]:
        return list(self.members)

    def GetPreClipNodeGraph(self) -> FakeGraph:
        return self.pre

    def GetPostClipNodeGraph(self) -> FakeGraph:
        return self.post


class FakeStill(FakeObject):
    live_class = "GalleryStill"


class FakeAlbum(FakeObject):
    live_class = "GalleryStillAlbum"

    def __init__(self, root: FakeResolve, name: str) -> None:
        super().__init__(root)
        self.name = name
        self.stills: list[FakeStill] = []
        self.labels: dict[int, str] = {}
        self.export_ok = True
        self.export_dir: Path | None = None

    def add(self, label: str) -> FakeStill:
        st = FakeStill(self._root)
        self.stills.append(st)
        self.labels[id(st)] = label
        return st

    def GetStills(self) -> list[FakeStill]:
        return list(self.stills)

    def GetLabel(self, st: FakeStill) -> str:
        return self.labels.get(id(st), "")

    def SetLabel(self, st: FakeStill, label: str) -> bool:
        self.labels[id(st)] = label
        return True

    def ImportStills(self, paths: list[str]) -> bool:
        for p in paths:
            self.add(Path(p).stem)
        return True

    def ExportStills(self, stills: list[FakeStill], folder: str, prefix: str, fmt: str) -> bool:
        if not self.export_ok:
            return False
        for i, _ in enumerate(stills, start=1):
            (Path(folder) / f"{prefix}_{i}.{fmt}").write_bytes(b"x")
        return True

    def DeleteStills(self, stills: list[FakeStill]) -> bool:
        self.stills = [s for s in self.stills if s not in stills]
        return True


class FakeGalleryFull(FakeObject):
    live_class = "Gallery"

    def __init__(self, root: FakeResolve) -> None:
        super().__init__(root)
        self.still_albums = [FakeAlbum(root, "Stills 1")]
        self.pg_albums = [FakeAlbum(root, "PowerGrade 1")]
        self.current = self.still_albums[0]

    def GetAlbumName(self, a: FakeAlbum) -> str:
        return a.name

    def SetAlbumName(self, a: FakeAlbum, name: str) -> bool:
        a.name = name
        return True

    def GetCurrentStillAlbum(self) -> FakeAlbum:
        return self.current

    def SetCurrentStillAlbum(self, a: FakeAlbum) -> bool:
        self.current = a
        return True

    def CreateGalleryStillAlbum(self) -> FakeAlbum:
        a = FakeAlbum(self._root, f"Stills {len(self.still_albums) + 1}")
        self.still_albums.append(a)
        return a

    def CreateGalleryPowerGradeAlbum(self) -> FakeAlbum:
        a = FakeAlbum(self._root, f"PowerGrade {len(self.pg_albums) + 1}")
        self.pg_albums.append(a)
        return a

    def GetGalleryStillAlbums(self) -> list[FakeAlbum]:
        return list(self.still_albums)

    def GetGalleryPowerGradeAlbums(self) -> list[FakeAlbum]:
        return list(self.pg_albums)


def install_color(resolve: FakeResolve) -> dict[str, Any]:
    """Wire color/gallery behaviour on the current project of ``resolve``."""
    for name, value in {
        "CACHE_AUTO_ENABLED": -1,
        "CACHE_DISABLED": 0,
        "CACHE_ENABLED": 1,
        "EXPORT_LUT_17PTCUBE": 17,
        "EXPORT_LUT_33PTCUBE": 33,
        "EXPORT_LUT_65PTCUBE": 65,
        "EXPORT_LUT_PANASONICVLUT": 99,
    }.items():
        setattr(resolve, name, value)

    state: dict[str, Any] = {"validate_error": None, "encrypt_ok": True}

    def validate(source: str) -> str | None:
        return state["validate_error"]

    def encrypt(path: str, options: dict[str, Any] | None = None) -> bool:
        options = options or {}
        if state["encrypt_ok"]:
            name = options.get("Name") or Path(path).name
            (Path(options["OutputFolder"]) / name).write_bytes(b"enc")
        return bool(state["encrypt_ok"])

    resolve.ValidateDCTL = validate  # type: ignore[attr-defined]
    resolve.EncryptDCTL = encrypt  # type: ignore[attr-defined]

    project = resolve.project_manager._current
    assert project is not None
    groups: list[FakeColorGroup] = []
    gallery = FakeGalleryFull(resolve)
    project.gallery = gallery  # type: ignore[assignment]
    project.groups = groups  # type: ignore[attr-defined]

    def add_group(name: str) -> FakeColorGroup:
        g = FakeColorGroup(resolve, name)
        groups.append(g)
        return g

    def delete_group(g: FakeColorGroup) -> bool:
        groups.remove(g)
        return True

    project.GetColorGroupsList = lambda: list(groups)  # type: ignore[attr-defined]
    project.AddColorGroup = add_group  # type: ignore[attr-defined]
    project.DeleteColorGroup = delete_group  # type: ignore[attr-defined]

    graphs: dict[str, FakeGraph] = {}
    for tl in project.timelines:
        tgraph = FakeGraph(resolve, 2)
        tl.GetNodeGraph = lambda g=tgraph: g  # type: ignore[attr-defined]
        graphs[f"tl:{tl._name}"] = tgraph
        grabbed: list[int] = []

        def grab(g: list[int] = grabbed) -> FakeStill:
            g.append(1)
            return gallery.current.add(f"grab{len(gallery.current.stills) + 1}")

        def grab_all(src: int) -> list[FakeStill]:
            return [gallery.current.add(f"all{i}") for i in range(3)]

        tl.GrabStill = grab  # type: ignore[attr-defined]
        tl.GrabAllStills = grab_all  # type: ignore[attr-defined]
        for track in tl.tracks:
            for item in track:
                _wire_item(resolve, item, groups, graphs)
    state.update(groups=groups, gallery=gallery, graphs=graphs, project=project)
    return state


def _wire_item(
    resolve: FakeResolve,
    item: FakeTimelineItem,
    groups: list[FakeColorGroup],
    graphs: dict[str, FakeGraph],
) -> None:
    graph = FakeGraph(resolve, 3)
    graphs[item._uid] = graph
    versions: dict[int, list[str]] = {0: ["Version 1"], 1: []}
    current = {"versionName": "Version 1", "versionType": 0}
    membership: dict[str, FakeColorGroup | None] = {"g": None}
    item.lie_versions = False  # type: ignore[attr-defined]
    item.cdl_calls = []  # type: ignore[attr-defined]
    item.copied_to = []  # type: ignore[attr-defined]
    item.exported = True  # type: ignore[attr-defined]

    def add_version(name: str, vt: int) -> bool:
        if resolve.page != "color":
            return False
        versions[vt].append(name)
        return True

    def load_version(name: str, vt: int) -> bool:
        current.update(versionName=name, versionType=vt)
        return True

    def rename_version(old: str, new: str, vt: int) -> bool:
        versions[vt] = [new if v == old else v for v in versions[vt]]
        return True

    def delete_version(name: str, vt: int) -> bool:
        if not item.lie_versions:  # type: ignore[attr-defined]
            versions[vt].remove(name)
        return True

    def export_lut(kind: int, path: str) -> bool:
        if resolve.page != "color":
            return False
        if item.exported:  # type: ignore[attr-defined]
            Path(path).write_text("LUT")
        return True

    def assign(group: FakeColorGroup) -> bool:
        membership["g"] = group
        group.members.append(item)
        return True

    def remove() -> bool:
        group = membership["g"]
        if group is not None:
            group.members.remove(item)
        membership["g"] = None
        return True

    def get_graph(layer: int | None = None) -> FakeGraph:
        return graph

    item.GetNodeGraph = get_graph  # type: ignore[attr-defined]
    item.AddVersion = add_version  # type: ignore[attr-defined]
    item.LoadVersionByName = load_version  # type: ignore[attr-defined]
    item.RenameVersionByName = rename_version  # type: ignore[attr-defined]
    item.DeleteVersionByName = delete_version  # type: ignore[attr-defined]
    item.GetVersionNameList = lambda vt: list(versions[vt])  # type: ignore[attr-defined]
    item.GetCurrentVersion = lambda: dict(current)  # type: ignore[attr-defined]
    item.SetCDL = lambda cdl: item.cdl_calls.append(cdl) or True  # type: ignore[attr-defined]
    item.ExportLUT = export_lut  # type: ignore[attr-defined]
    item.CopyGrades = lambda targets: item.copied_to.extend(targets) or True  # type: ignore[attr-defined]
    item.ResetAllNodeColors = lambda: True  # type: ignore[attr-defined]
    item.GetColorGroup = lambda: membership["g"]  # type: ignore[attr-defined]
    item.AssignToColorGroup = assign  # type: ignore[attr-defined]
    item.RemoveFromColorGroup = remove  # type: ignore[attr-defined]
