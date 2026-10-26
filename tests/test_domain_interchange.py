# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Interchange domain: timecode math, golden snippets and write/read round-trips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from davinci_mcp.domains import interchange as ix
from davinci_mcp.domains.interchange import ACTIONS
from davinci_mcp.errors import DavinciError
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, mcp_client


async def _call(settings, tool: str, op: dict):
    async with mcp_client(make_resolve(), settings, ACTIONS) as client:
        err, out = await call(client, tool, {"op": op})
        return err, out.get("result", out)


# -- timecode math ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("frames", "fps", "drop", "expected"),
    [
        (0, 24, False, "00:00:00:00"),
        (86400, 24, False, "01:00:00:00"),
        (1799, 29.97, True, "00:00:59;29"),
        (1800, 29.97, True, "00:01:00;02"),
        (17982, 29.97, True, "00:10:00;00"),
        (1800, 29.97, False, "00:01:00:00"),
        (3600 * 24 * 24, 23.976, False, "24:00:00:00"),
        (3600, 59.94, True, "00:01:00;04"),
    ],
)
def test_frames_to_timecode(frames: int, fps: float, drop: bool, expected: str) -> None:
    assert ix.frames_to_timecode(frames, fps, drop) == expected


def test_drop_frame_roundtrip_exhaustive() -> None:
    for frames in range(0, 200_000, 7):
        tc = ix.frames_to_timecode(frames, 29.97, True)
        assert ";" in tc
        assert ix.timecode_to_frames(tc, 29.97, True) == frames


def test_drop_frame_rejects_invalid() -> None:
    with pytest.raises(DavinciError):
        ix.frames_to_timecode(10, 24, True)
    with pytest.raises(DavinciError):
        ix.timecode_to_frames("00:01:00;00", 29.97, True)  # dropped label
    with pytest.raises(DavinciError):
        ix.timecode_to_frames("00:00:00:30", 29.97, False)
    with pytest.raises(DavinciError):
        ix.timecode_to_frames("garbage", 24)


def test_frame_duration() -> None:
    assert ix.frame_duration(24) == (1, 24)
    assert ix.frame_duration(23.976) == (1001, 24000)
    assert ix.frame_duration(29.97) == (1001, 30000)
    assert ix.frame_duration(59.94) == (1001, 60000)


# -- EDL ----------------------------------------------------------------------------

EVENTS = [
    {
        "reel": "A001",
        "src_in": "01:00:00:00",
        "src_out": "01:00:05:00",
        "rec_in": "01:00:00:00",
        "rec_out": "01:00:05:00",
        "clip_name": "shot_a.mov",
    },
    {
        "reel": "A002",
        "track": "A",
        "transition": "D",
        "transition_frames": 12,
        "src_in": "02:00:00:00",
        "src_out": "02:00:02:00",
        "rec_in": "01:00:05:00",
        "rec_out": "01:00:07:00",
        "speed_fps": 48.0,
    },
]


async def test_edl_golden_and_roundtrip(settings, tmp_path: Path) -> None:
    path = tmp_path / "a.edl"
    err, out = await _call(
        settings,
        "interchange_write",
        {"action": "write_edl", "path": str(path), "title": "T", "events": EVENTS},
    )
    assert not err, out
    text = path.read_text()
    assert text.startswith("TITLE: T\nFCM: NON-DROP FRAME\n")
    assert "001  A001     V     C         01:00:00:00 01:00:05:00 01:00:00:00 01:00:05:00" in text
    assert "002  A002     A     D    012  " in text
    assert "* FROM CLIP NAME: shot_a.mov" in text
    err, read = await _call(settings, "interchange_read", {"action": "read_edl", "path": str(path)})
    assert not err, read
    assert [e["reel"] for e in read["events"]] == ["A001", "A002"]
    assert read["events"][1]["transition"] == "D"
    assert read["events"][1]["transition_frames"] == 12
    assert read["events"][1]["speed_fps"] == 48.0
    assert read["events"][0]["clip_name"] == "shot_a.mov"


async def test_edl_drop_frame_and_overwrite(settings, tmp_path: Path) -> None:
    path = tmp_path / "df.edl"
    op = {
        "action": "write_edl",
        "path": str(path),
        "fps": 29.97,
        "drop_frame": True,
        "events": [
            {
                "src_in": "00:00:00;00",
                "src_out": "00:01:00;02",
                "rec_in": "00:00:00;00",
                "rec_out": "00:01:00;02",
            }
        ],
    }
    err, _ = await _call(settings, "interchange_write", op)
    assert not err
    assert "FCM: DROP FRAME" in path.read_text()
    err, _ = await _call(settings, "interchange_write", op)
    assert err  # refuses to overwrite
    err, _ = await _call(settings, "interchange_write", {**op, "overwrite": True})
    assert not err


async def test_edl_rejects_bad_events_and_paths(settings, tmp_path: Path) -> None:
    bad = {**EVENTS[0], "src_out": "00:59:00:00"}
    err, _ = await _call(
        settings,
        "interchange_write",
        {"action": "write_edl", "path": str(tmp_path / "b.edl"), "events": [bad]},
    )
    assert err
    err, _ = await _call(
        settings,
        "interchange_write",
        {"action": "write_edl", "path": "/etc/x.edl", "events": EVENTS},
    )
    assert err
    err, _ = await _call(
        settings, "interchange_read", {"action": "read_edl", "path": str(tmp_path / "missing.edl")}
    )
    assert err


# -- FCPXML --------------------------------------------------------------------------

SEQ_ITEMS = [
    {
        "kind": "clip",
        "name": "A",
        "file": "/media/a b.mov",
        "src_in_frames": 24,
        "duration_frames": 48,
    },
    {"kind": "gap", "duration_frames": 24},
    {"kind": "title", "name": "Hello", "text": "Hello", "duration_frames": 24},
    {
        "kind": "clip",
        "name": "B",
        "file": "/media/a b.mov",
        "src_in_frames": 0,
        "duration_frames": 24,
    },
]


async def test_fcpxml_roundtrip(settings, tmp_path: Path) -> None:
    path = tmp_path / "t.fcpxml"
    op = {
        "action": "write_fcpxml",
        "path": str(path),
        "name": "Seq",
        "fps": 29.97,
        "items": SEQ_ITEMS,
        "markers": [{"frame": 10, "name": "m1", "note": "n"}, {"frame": 60, "name": "m2"}],
    }
    err, out = await _call(settings, "interchange_write", op)
    assert not err, out
    text = path.read_text()
    assert '<fcpxml version="1.10">' in text
    assert 'frameDuration="1001/30000s"' in text
    assert "file:///media/a%20b.mov" in text
    err, read = await _call(
        settings, "interchange_read", {"action": "read_fcpxml", "path": str(path)}
    )
    assert not err, read
    assert read["version"] == "1.10"
    assert round(read["fps"], 2) == 29.97
    assert len(read["assets"]) == 1
    assert read["assets"][0]["path"] == "/media/a b.mov"
    kinds = [i["kind"] for i in read["items"]]
    assert kinds == ["asset-clip", "gap", "title", "asset-clip"]
    assert read["items"][0]["duration_frames"] == 48
    assert [m["name"] for m in read["markers"]] == ["m1", "m2"]


def test_fcpxml_rejects_entities_and_marker_outside() -> None:
    with pytest.raises(DavinciError):
        ix.parse_fcpxml('<!DOCTYPE x [<!ENTITY a "b">]><fcpxml version="1.10">&a;</fcpxml>')
    with pytest.raises(DavinciError):
        ix.parse_fcpxml("<other/>")
    params = ix.WriteFcpxmlParams.model_validate(
        {"path": "x", "items": SEQ_ITEMS, "markers": [{"frame": 9999}]}
    )
    with pytest.raises(DavinciError):
        ix.render_fcpxml(params)


# -- OTIO ---------------------------------------------------------------------------


async def test_otio_roundtrip(settings, tmp_path: Path) -> None:
    path = tmp_path / "t.otio"
    op = {
        "action": "write_otio",
        "path": str(path),
        "name": "Seq",
        "start_timecode": "01:00:00:00",
        "tracks": [
            {"name": "V1", "kind": "Video", "items": SEQ_ITEMS},
            {"name": "A1", "kind": "Audio", "items": [SEQ_ITEMS[0]]},
        ],
        "markers": [{"frame": 30, "name": "mk", "color": "Red"}],
    }
    err, out = await _call(settings, "interchange_write", op)
    assert not err, out
    doc = json.loads(path.read_text())
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    assert doc["tracks"]["OTIO_SCHEMA"] == "Stack.1"
    assert doc["global_start_time"]["value"] == 86400.0
    clip = doc["tracks"]["children"][0]["children"][0]
    assert clip["OTIO_SCHEMA"] == "Clip.1"
    assert clip["media_reference"]["OTIO_SCHEMA"] == "ExternalReference.1"
    assert clip["media_reference"]["target_url"] == "file:///media/a%20b.mov"
    assert clip["source_range"]["start_time"]["value"] == 24.0
    assert doc["tracks"]["children"][0]["children"][1]["OTIO_SCHEMA"] == "Gap.1"
    err, read = await _call(
        settings, "interchange_read", {"action": "read_otio", "path": str(path)}
    )
    assert not err, read
    assert [t["name"] for t in read["tracks"]] == ["V1", "A1"]
    assert len(read["tracks"][0]["items"]) == 4
    assert read["markers"][0]["name"] == "mk"
    assert read["markers"][0]["color"] == "RED"


def test_otio_rejects_wrong_document() -> None:
    with pytest.raises(DavinciError):
        ix.parse_otio('{"OTIO_SCHEMA": "Clip.1"}')
    with pytest.raises(DavinciError):
        ix.parse_otio("[1]")


# -- marker CSV ---------------------------------------------------------------------


async def test_marker_csv_roundtrip_and_aliases(settings, tmp_path: Path) -> None:
    path = tmp_path / "m.csv"
    op = {
        "action": "write_marker_csv",
        "path": str(path),
        "markers": [
            {
                "timecode": "01:00:01:00",
                "name": "a, b",
                "color": "Red",
                "duration": 3,
                "notes": "x",
            },
            {"timecode": "01:00:02:00"},
        ],
    }
    err, _ = await _call(settings, "interchange_write", op)
    assert not err
    assert path.read_text().splitlines()[0] == "#,Timecode,Name,Color,Duration,Notes"
    err, read = await _call(
        settings, "interchange_read", {"action": "read_marker_csv", "path": str(path)}
    )
    assert not err
    assert read["markers"][0]["name"] == "a, b"
    assert read["markers"][0]["duration"] == 3
    other = tmp_path / "o.csv"
    other.write_text("Marker Name,Description,Record In\nfoo,bar,01:00:00:00\n")
    err, read = await _call(
        settings, "interchange_read", {"action": "read_marker_csv", "path": str(other)}
    )
    assert not err
    assert read["markers"][0] == {
        "timecode": "01:00:00:00",
        "name": "foo",
        "color": "Blue",
        "duration": 1,
        "notes": "bar",
    }
    noheader = tmp_path / "n.csv"
    noheader.write_text("a,b\n1,2\n")
    err, _ = await _call(
        settings, "interchange_read", {"action": "read_marker_csv", "path": str(noheader)}
    )
    assert err


# -- .cube -------------------------------------------------------------------------


async def test_cube_3d_transform_and_read(settings, tmp_path: Path) -> None:
    path = tmp_path / "g.cube"
    op = {
        "action": "write_cube_lut",
        "path": str(path),
        "size": 5,
        "transform": {"gain": [2.0, 1.0, 1.0], "saturation": 1.0},
    }
    err, out = await _call(settings, "interchange_write", op)
    assert not err, out
    lines = path.read_text().splitlines()
    assert lines[0] == 'TITLE "davinci-mcp LUT"'
    assert "LUT_3D_SIZE 5" in lines
    rows = [ln for ln in lines if ln and ln[0].isdigit()]
    assert len(rows) == 125
    assert rows[0] == "0.000000 0.000000 0.000000"
    assert rows[1] == "0.500000 0.000000 0.000000"  # red fastest, gain 2 on red
    assert rows[-1] == "1.000000 1.000000 1.000000"  # clamped
    err, read = await _call(
        settings, "interchange_read", {"action": "read_cube_lut", "path": str(path)}
    )
    assert not err
    assert read["dim"] == 3
    assert read["size"] == 5
    assert read["entries"] == 125
    assert read["complete"] is True


async def test_cube_1d_values_and_validation(settings, tmp_path: Path) -> None:
    path = tmp_path / "one.cube"
    values = [[0, 0, 0], [0.5, 0.5, 0.5], [1, 1, 1]]
    err, _ = await _call(
        settings,
        "interchange_write",
        {"action": "write_cube_lut", "path": str(path), "dim": 1, "size": 3, "values": values},
    )
    assert not err
    assert "LUT_1D_SIZE 3" in path.read_text()
    for op in (
        {"dim": 1, "size": 4, "values": values},  # wrong row count
        {"size": 3},  # neither values nor transform
        {"size": 3, "values": values, "transform": {}},  # both
        {"size": 3, "transform": {"gamma": 0}},
        {"size": 3, "transform": {"matrix": [[1, 0], [0, 1]]}},
    ):
        err, _ = await _call(
            settings,
            "interchange_write",
            {"action": "write_cube_lut", "path": str(tmp_path / "bad.cube"), **op},
        )
        assert err
    assert not (tmp_path / "bad.cube").exists()


def test_cube_gamma_lift_matrix_saturation() -> None:
    t = ix.CubeTransform(
        gamma=2.0, lift=0.1, saturation=0.0, matrix=[[0, 1, 0], [1, 0, 0], [0, 0, 1]]
    )
    r, g, b = ix._apply((0.25, 0.0, 0.0), t, (1, 1, 1), (0.1, 0.1, 0.1), (2.0, 2.0, 2.0))
    assert r == pytest.approx(g)
    assert g == pytest.approx(b)  # fully desaturated
