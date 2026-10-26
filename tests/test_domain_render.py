# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the render domain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.config import Settings
from davinci_mcp.domains.render import ACTIONS, RenderCodec, RenderFormat, _find_codec
from davinci_mcp.errors import DavinciError
from tests.fakes.render import make_render_resolve
from tests.helpers import call, confirm, mcp_client


async def _do(client: Any, tool: str, action: str, **params: Any) -> tuple[bool, dict[str, Any]]:
    err, out = await call(client, tool, {"op": {"action": action, **params}})
    if tool == "render_read" and "result" in out:
        return err, out["result"]
    return err, out


async def test_read_formats_presets_config(settings: Settings) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await _do(client, "render_read", "list_render_formats", format="mov")
        assert not err
        fmt = out["formats"][0]
        assert fmt["extension"] == "mov"
        assert {"id": "ProRes422", "description": "Apple ProRes 422"} in fmt["codecs"]
        err, out = await _do(client, "render_read", "list_render_formats", audio=True)
        assert not err
        err, out = await _do(client, "render_read", "list_render_presets")
        assert not err and "H.264 Master" in str(out)
        resolve.page = "edit"
        err, out = await _do(client, "render_read", "get_render_config")
        assert not err and "single" in str(out)
        assert resolve.page == "edit"  # page restored
        err, out = await _do(
            client,
            "render_read",
            "get_render_resolutions",
            format="mov",
            codec="Apple ProRes 422 HQ",
        )
        assert not err and "1920" in str(out)
        err, _ = await _do(client, "render_read", "get_render_resolutions", format="nope")
        assert err
        assert project.rendering is False


async def test_set_format_accepts_id_or_description(settings: Settings) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await _do(
            client,
            "render_write",
            "set_render_format",
            format="QuickTime",
            codec="Apple ProRes 422",
        )
        assert not err and out["receipt"]["verified"] is True
        assert project.fmt_codec == {"format": "mov", "codec": "ProRes422"}
        err, _ = await _do(client, "render_write", "set_render_format", format="mov", codec="bogus")
        assert err
        err, _ = await _do(client, "render_write", "set_render_format", format="wav", codec="x")
        assert err


async def test_set_settings_payload(settings: Settings, tmp_path: Path) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await _do(
            client,
            "render_write",
            "set_render_settings",
            base_preset="H.264 Master",
            settings={"TargetDir": str(tmp_path), "CustomName": "", "MarkIn": 10, "MarkOut": 20},
        )
        assert not err, out
        sent = project.applied[-1]
        assert "CustomName" not in sent
        assert (sent["MarkIn"], sent["MarkOut"]) == (86410, 86420)
        assert sent["SelectAllFrames"] is False
        assert project.loaded == ["H.264 Master"]
        err, _ = await _do(
            client, "render_write", "set_render_settings", settings={"TargetDir": "/etc"}
        )
        assert err
        err, _ = await _do(client, "render_write", "set_render_settings", settings={})
        assert err
        err, _ = await _do(
            client,
            "render_write",
            "set_render_settings",
            base_preset="nope",
            settings={"ExportAudio": True},
        )
        assert err
        project.reject_settings = True
        err, _ = await _do(
            client, "render_write", "set_render_settings", settings={"ExportAudio": True}
        )
        assert err


async def test_mode_verification(settings: Settings) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await _do(client, "render_write", "set_render_mode", mode="individual")
        assert not err and out["receipt"]["verified"] is True and project.mode == 0
        project.ignore_mode = True
        err, out = await _do(client, "render_write", "set_render_mode", mode="single")
        assert not err and out["receipt"]["verified"] is False


async def test_presets_lifecycle(settings: Settings, tmp_path: Path) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await _do(client, "render_write", "load_render_preset", name="H.264 Master")
        assert not err
        err, _ = await _do(client, "render_write", "load_render_preset", name="nope")
        assert err
        err, out = await _do(client, "render_write", "save_render_preset", name="Mine")
        assert not err and out["receipt"]["verified"] is True
        err, _ = await _do(client, "render_write", "save_render_preset", name="Mine")
        assert err
        dest = tmp_path / "mine.preset"
        err, out = await _do(
            client, "render_write", "export_render_preset", name="Mine", path=str(dest)
        )
        assert not err and dest.exists()
        err, _ = await _do(
            client, "render_write", "export_render_preset", name="Mine", path=str(dest)
        )
        assert err
        err, out = await _do(client, "render_write", "import_render_preset", path=str(dest))
        assert not err and out["receipt"]["result"]["added"] == ["mine"]
        err, _ = await _do(
            client, "render_write", "import_render_preset", path=str(tmp_path / "missing")
        )
        assert err
        err, out = await confirm(
            client, "render_delete", {"op": {"action": "update_render_preset", "name": "Mine"}}
        )
        assert not err and out["status"] == "applied"
        err, out = await confirm(
            client, "render_delete", {"op": {"action": "delete_render_preset", "name": "Mine"}}
        )
        assert not err and out["receipt"]["verified"] is True
        assert "Mine" not in project.presets


async def test_quick_export_preset_toggle(settings: Settings) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await _do(
            client, "render_write", "set_quick_export_preset", name="ProRes 422 HQ"
        )
        assert not err and out["receipt"]["verified"] is True
        project.qe_enabled_noop = True
        err, out = await _do(client, "render_write", "set_quick_export_preset", name="Audio Only")
        assert not err and out["receipt"]["verified"] is False


async def test_queue_and_render(settings: Settings, tmp_path: Path) -> None:
    resolve, _project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _ = await _do(client, "render_write", "start_rendering")
        assert err  # empty queue
        await _do(client, "render_write", "set_render_settings", settings={"CustomName": "x"})
        resolve.page = "edit"
        err, out = await _do(client, "render_write", "add_render_job")
        assert not err and out["receipt"]["verified"] is True
        assert resolve.page == "edit"
        err, out = await _do(client, "render_read", "list_render_jobs")
        assert not err and out["jobs"][0]["state"] == "queued"
        err, out = await _do(client, "render_write", "start_rendering")
        assert not err and out["status"] == "started"
        err, _again = await _do(client, "render_write", "start_rendering")
        assert err  # WRONG_STATE: already rendering
        err, waited = await call(client, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 15})
        assert not err and waited["state"] == "succeeded"
        err, out = await _do(client, "render_read", "list_render_jobs")
        assert out["jobs"][0]["state"] == "complete"
        err, out = await _do(client, "render_write", "start_rendering", job_ids=["zzz"])
        assert err


async def test_failed_render_job(settings: Settings) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        await _do(client, "render_write", "set_render_settings", settings={"CustomName": "x"})
        await _do(client, "render_write", "add_render_job")
        project.errors["job1"] = "disk full"
        _e, out = await _do(client, "render_write", "start_rendering")
        _err, waited = await call(client, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 15})
        assert waited["state"] == "failed" and "disk full" in waited["error"]


async def test_stop_and_quick_export(settings: Settings, tmp_path: Path) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _ = await _do(client, "render_write", "quick_export", preset="Audio Only")
        assert err
        err, out = await _do(
            client,
            "render_write",
            "quick_export",
            preset="H.264 Master",
            target_dir=str(tmp_path),
            name="q",
        )
        assert not err and out["status"] == "started"
        err, waited = await call(client, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 15})
        assert waited["state"] == "succeeded"
        project.rendering = True
        err, out = await _do(client, "render_write", "stop_rendering")
        assert not err and out["receipt"]["verified"] is True and project.rendering is False


async def test_render_timeline_one_shot(settings: Settings, tmp_path: Path) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, _ = await _do(
            client,
            "render_write",
            "render_timeline",
            preset="nope",
            target_dir=str(tmp_path),
            name="a",
        )
        assert err
        err, out = await _do(
            client,
            "render_write",
            "render_timeline",
            preset="H.264 Master",
            target_dir=str(tmp_path),
            name="final",
        )
        assert not err and out["status"] == "started"
        err, waited = await call(client, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 15})
        assert waited["state"] == "succeeded"
        assert project.loaded == ["H.264 Master"]
        assert project.applied[-1]["CustomName"] == "final"
        assert len(project.jobs) == 1


@pytest.mark.parametrize("action", ["delete_render_jobs", "delete_all_render_jobs"])
async def test_delete_jobs(settings: Settings, action: str) -> None:
    resolve, project = make_render_resolve()
    async with mcp_client(resolve, settings, ACTIONS) as client:
        await _do(client, "render_write", "set_render_settings", settings={"CustomName": "x"})
        await _do(client, "render_write", "add_render_job")
        op: dict[str, Any] = {"action": action}
        if action == "delete_render_jobs":
            op["job_ids"] = ["job1"]
            err, _ = await confirm(client, "render_delete", {"op": {**op, "job_ids": ["nope"]}})
            assert err
        err, out = await confirm(client, "render_delete", {"op": op})
        assert not err and out["receipt"]["verified"] is True
        assert project.jobs == {}


def test_codec_matching_is_forgiving() -> None:
    fmt = RenderFormat(
        format="QuickTime",
        extension="mov",
        codecs=[
            RenderCodec(id="ProRes422", description="Apple ProRes 422"),
            RenderCodec(id="ProRes422HQ", description="Apple ProRes 422 HQ"),
            RenderCodec(id="H264", description="H.264"),
        ],
    )
    assert _find_codec(fmt, "ProRes 422 HQ").id == "ProRes422HQ"
    assert _find_codec(fmt, "prores 422").id == "ProRes422"
    assert _find_codec(fmt, "H.264").id == "H264"
    assert _find_codec(fmt, "h264").id == "H264"
    with pytest.raises(DavinciError):
        _find_codec(fmt, "ProRes")  # ambiguous
