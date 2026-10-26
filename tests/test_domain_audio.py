# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the audio domain (fake Resolve; ffmpeg tools run on tiny generated files)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.config import Settings
from davinci_mcp.domains.audio import ACTIONS, NormalizeFileParams, _NormalizeJob
from tests.fakes.ai import AIWorld, make_ai_world
from tests.helpers import call, mcp_client

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
ITEM = {"track": {"type": "audio", "index": 1}, "position": 1}


def _tone(path: Path) -> Path:
    """2 s sine tone with a silent gap between 0.5 s and 1.5 s."""
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-af", "volume=enable='between(t,0.5,1.5)':volume=0", str(path),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
    )  # fmt: skip
    return path


async def _write(client: Any, action: str, **params: Any) -> tuple[bool, dict[str, Any]]:
    return await call(client, "audio_write", {"op": {"action": action, **params}})


async def _read(client: Any, action: str, **params: Any) -> tuple[bool, dict[str, Any]]:
    return await call(client, "audio_read", {"op": {"action": action, **params}})


# -- voice isolation ------------------------------------------------------------------------


async def test_voice_isolation_item_and_track(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _read(client, "get_voice_isolation", item=ITEM)
        assert not err and out["result"]["enabled"] is False
        err, out = await _write(client, "set_voice_isolation", item=ITEM, enabled=True, amount=80)
        assert not err and out["receipt"]["verified"] is True
        assert world.item.voice == {"isEnabled": True, "amount": 80}
        err, out = await _write(client, "set_voice_isolation", track=1, enabled=True)
        assert not err and out["receipt"]["verified"] is True
        assert world.timeline.track_voice[1]["isEnabled"] is True
        _, out = await _read(client, "get_voice_isolation", track=1)
        assert out["result"]["scope"] == "track:1"


async def test_voice_isolation_verification_failure_and_selector(settings: Settings) -> None:
    world = make_ai_world()
    world.item.lie = True
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "set_voice_isolation", item=ITEM, enabled=True)
        assert not err and out["receipt"]["verified"] is False
        err, _ = await _read(client, "get_voice_isolation")
        assert err
        err, _ = await _read(client, "get_voice_isolation", item=ITEM, track=1)
        assert err
        err, _ = await _write(client, "set_voice_isolation", track=1, enabled=True, amount=101)
        assert err


# -- mapping -----------------------------------------------------------------------------------


async def test_audio_mapping_clip_and_item(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _read(client, "get_audio_mapping", clip={"name": "interview.wav"})
        assert not err and out["result"]["mapping"] == {"embedded_audio_channels": 2}
        err, out = await _write(
            client, "set_audio_mapping", clip={"name": "interview.wav"}, mapping={"a": [1]}
        )
        assert not err and out["receipt"]["verified"] is True
        assert json.loads(world.clip.audio_mapping) == {"a": [1]}
        err, out = await _write(client, "set_audio_mapping", item=ITEM, mapping={"channels": [2]})
        assert not err and out["receipt"]["verified"] is True
        _, out = await _read(client, "get_audio_mapping", item=ITEM)
        assert out["result"]["mapping"] == {"channels": [2]}


async def test_audio_mapping_verification_failure_and_gate(settings: Settings) -> None:
    world = make_ai_world()
    world.clip.lie = True
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(
            client, "set_audio_mapping", clip={"name": "interview.wav"}, mapping={"a": 1}
        )
        assert not err and out["receipt"]["verified"] is False
        err, _ = await _read(client, "get_audio_mapping", clip={"name": "nope"})
        assert err
    old = make_ai_world()
    old.resolve.GetVersion = lambda: [21, 0, 4, 1, ""]  # type: ignore[method-assign]
    async with mcp_client(old.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "set_audio_mapping", item=ITEM, mapping={})
        assert err and out["code"] == "CAPABILITY_UNAVAILABLE"


# -- normalize / presets ------------------------------------------------------------------------


async def test_normalize_modes_and_level(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        _, out = await _read(client, "list_normalize_modes")
        assert "Sample Peak Program" in out["result"]["modes"]
        err, out = await _write(
            client,
            "normalize_audio_level",
            items=[{"id": world.item.GetUniqueId()}],
            options={
                "normalizationMode": "True Peak Program",
                "targetLevel": -3.0,
                "setLevelMode": "NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT",
            },
        )
        assert not err and out["receipt"]["result"]["normalized"] == 1
        sent = world.timeline.calls[-1][1][1]
        assert sent["setLevelMode"] == 41 and sent["targetLevel"] == -3.0


async def test_normalize_errors(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(
            client,
            "normalize_audio_level",
            items=[{"id": world.item.GetUniqueId()}],
            options={"normalizationMode": "Bogus"},
        )
        assert err and out["code"] == "INVALID_PARAMS"
        err, _ = await _write(client, "normalize_audio_level", items=[])
        assert err
        world.timeline.lie = True
        err, out = await _write(
            client, "normalize_audio_level", items=[{"id": world.item.GetUniqueId()}]
        )
        assert err and out["code"] == "API_CALL_FAILED"


async def test_fairlight_presets(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        _, out = await _read(client, "list_fairlight_presets")
        assert out["result"]["presets"] == ["Podcast", "Cinema"]
        err, _ = await _write(client, "apply_fairlight_preset", name="Podcast")
        assert not err and world.applied_presets == ["Podcast"]
        err, out = await _write(client, "apply_fairlight_preset", name="Nope")
        assert err and out["code"] == "NOT_FOUND"


# -- audio properties --------------------------------------------------------------------------


async def test_audio_properties(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        _, out = await _read(client, "get_audio_properties", item=ITEM)
        assert out["result"]["properties"]["AudioVolume"] == 0.0
        err, out = await _write(
            client,
            "set_audio_properties",
            item=ITEM,
            properties={
                "AudioVolume": -6.0,
                "AudioDialogueLevelerEnabled": True,
                "AudioDialogueLevelerMode": "DIALOGUE_LEVELER_MODE_ALLOW_WIDER_DYNAMICS",
            },
        )
        assert not err and out["receipt"]["verified"] is True
        assert world.item.audio_props["AudioDialogueLevelerMode"] == 50
        err, _ = await _write(client, "set_audio_properties", item=ITEM, properties={})
        assert err
        err, _ = await _write(
            client, "set_audio_properties", item=ITEM, properties={"AudioVolume": 99}
        )
        assert err


async def test_audio_properties_verification_failure(settings: Settings) -> None:
    world = make_ai_world()
    world.item.lie = True
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(
            client, "set_audio_properties", item=ITEM, properties={"AudioPan": 10.0}
        )
        assert not err and out["receipt"]["verified"] is False


# -- ffmpeg tools -----------------------------------------------------------------------------


@needs_ffmpeg
async def test_analyze_loudness_and_silence(settings: Settings, tmp_path: Path) -> None:
    wav = _tone(tmp_path / "tone.wav")
    world = make_ai_world(str(wav))
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _read(client, "analyze_loudness", path=str(wav))
        assert not err
        assert out["result"]["integrated_lufs"] is not None
        assert out["result"]["true_peak_dbtp"] is not None
        err, out = await _read(client, "detect_silence", clip={"name": "interview.wav"})
        assert not err
        gaps = out["result"]["intervals"]
        assert len(gaps) == 1 and 0.4 < gaps[0]["start_s"] < 0.6 and gaps[0]["duration_s"] > 0.8


@needs_ffmpeg
async def test_ffmpeg_tools_path_errors(settings: Settings, tmp_path: Path) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _read(client, "analyze_loudness", path="/etc/hosts")
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        err, out = await _read(client, "analyze_loudness", path=str(tmp_path / "missing.wav"))
        assert err and out["code"] == "NOT_FOUND"
        err, _ = await _read(client, "analyze_loudness")
        assert err
        junk = tmp_path / "junk.wav"
        junk.write_bytes(b"not audio")
        err, out = await _read(client, "detect_silence", path=str(junk))
        assert err and out["code"] == "API_CALL_FAILED"


@needs_ffmpeg
async def test_normalize_file_refuses_overwrite(settings: Settings, tmp_path: Path) -> None:
    wav = _tone(tmp_path / "tone.wav")
    existing = tmp_path / "out.wav"
    existing.write_bytes(b"keep")
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "normalize_file", path=str(wav), output=str(existing))
        assert err and out["code"] == "WRONG_STATE"
        assert existing.read_bytes() == b"keep"
        err, out = await _write(client, "normalize_file", path=str(wav), output="/tmp/x/out.wav")
        assert err and out["code"] == "PATH_NOT_ALLOWED"


@needs_ffmpeg
async def test_normalize_file_starts_job(settings: Settings, tmp_path: Path) -> None:
    wav = _tone(tmp_path / "tone.wav")
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "normalize_file", path=str(wav))
        assert not err and out["status"] == "started"
        assert out["job"]["kind"] == "audio.normalize_file"


@needs_ffmpeg
def test_normalize_job_state_machine(tmp_path: Path) -> None:
    """Drive the two-pass job directly (no 2 s poll interval)."""
    wav = _tone(tmp_path / "tone.wav")
    dst = tmp_path / "tone.norm.wav"
    params = NormalizeFileParams(path=str(wav), target_lufs=-20.0)
    job = _NormalizeJob(wav.resolve(), dst, params)
    assert job.start() is True
    deadline = time.monotonic() + 15
    seen: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        status = dict(job.poll(None))  # type: ignore[arg-type]
        seen.append(status)
        if status.get("done") or status.get("Error"):
            break
        time.sleep(0.05)
    assert seen[-1].get("done") is True, seen[-1]
    assert dst.exists() and dst.stat().st_size > 0
    assert any(s.get("CompletionPercentage") == 50 for s in seen)


@needs_ffmpeg
def test_normalize_job_silent_source_fails(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1", str(silent),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
    )  # fmt: skip
    job = _NormalizeJob(silent, tmp_path / "silent.norm.wav", NormalizeFileParams(path=str(silent)))
    job.start()
    deadline = time.monotonic() + 15
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = dict(job.poll(None))  # type: ignore[arg-type]
        if status.get("done") or status.get("Error"):
            break
        time.sleep(0.05)
    assert "silent" in str(status.get("Error", "")).lower()
    assert not (tmp_path / "silent.norm.wav").exists()


def test_world_fixture_is_wired() -> None:
    world: AIWorld = make_ai_world()
    assert world.item.GetUniqueId() == world.timeline.audio_tracks[0][0].GetUniqueId()
