# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Tests for the ai domain (fake Resolve, no real AI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from davinci_mcp.config import Settings
from davinci_mcp.domains.ai import ACTIONS
from tests.fakes.ai import EXTRAS_ERROR, AIWorld, make_ai_world
from tests.helpers import call, confirm, mcp_client

CLIP = {"clip": {"name": "interview.wav"}}


async def _write(client: Any, action: str, **params: Any) -> tuple[bool, dict[str, Any]]:
    return await call(client, "ai_write", {"op": {"action": action, **params}})


async def _read(client: Any, action: str, **params: Any) -> tuple[bool, dict[str, Any]]:
    return await call(client, "ai_read", {"op": {"action": action, **params}})


async def _delete(client: Any, action: str, **params: Any) -> tuple[bool, dict[str, Any]]:
    return await confirm(client, "ai_delete", {"op": {"action": action, **params}})


async def _run_job(client: Any, action: str, **params: Any) -> dict[str, Any]:
    err, out = await _write(client, action, **params)
    assert not err and out["status"] == "started", out
    err, waited = await call(client, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 10})
    assert not err
    return waited


# -- long-running jobs -----------------------------------------------------------------------


async def test_transcribe_job_clip_and_folder(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        job = await _run_job(client, "transcribe_audio", **CLIP, speaker_detection=True)
        assert job["state"] == "succeeded" and world.clip.transcription["language"] == "fr"
        world.clip.transcription = {}
        job = await _run_job(client, "transcribe_audio", folder={})
        assert job["state"] == "succeeded" and world.clip.transcription


async def test_job_failure_from_status_error(settings: Settings) -> None:
    world = make_ai_world()
    world.status["GetTranscribeAudioStatus"] = [{"JobStatus": "Failed", "Error": "disk full"}]
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        job = await _run_job(client, "transcribe_audio", **CLIP)
        assert job["state"] == "failed" and "disk full" in job["error"]


async def test_job_done_when_status_falls_idle(settings: Settings) -> None:
    world = make_ai_world()
    world.status["GetDetectSceneCutsStatus"] = [
        {"JobStatus": "Analyzing", "CompletionPercentage": 40},
        {"JobStatus": "Inactive"},
    ]
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "detect_scene_cuts")
        assert not err and out["status"] == "started"
        err, waited = await call(client, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 10})
        assert not err and waited["state"] == "succeeded"


async def test_ai_launch_errors(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        world.clip.ai_return = EXTRAS_ERROR
        err, out = await _write(client, "analyze_for_slate", **CLIP)
        assert err and out["code"] == "EXTRAS_MISSING"
        world.clip.ai_return = False
        err, out = await _write(client, "analyze_for_slate", **CLIP)
        assert err and out["code"] == "API_CALL_FAILED" and "Extras" in out["hint"]
        world.clip.ai_return = "something else broke"
        err, out = await _write(client, "transcribe_audio", **CLIP)
        assert err and out["code"] == "API_CALL_FAILED"


async def test_create_subtitles_resolves_constants(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        job = await _run_job(
            client,
            "create_subtitles_from_audio",
            language="AUTO_CAPTION_FRENCH",
            chars_per_line=30,
            line_break="AUTO_CAPTION_LINE_DOUBLE",
            gap=2,
        )
        assert job["state"] == "succeeded"
        sent = [a for n, a in world.timeline.calls if n == "CreateSubtitlesFromAudio"][-1][0]
        assert sent == {1: 11, 3: 30, 4: 30, 5: 2}
        err, _ = await _write(client, "create_subtitles_from_audio", chars_per_line=99)
        assert err
        world.timeline.ai_return = EXTRAS_ERROR
        err, out = await _write(client, "create_subtitles_from_audio")
        assert err and out["code"] == "EXTRAS_MISSING"


async def test_smart_reframe_and_scene_cuts(settings: Settings) -> None:
    world = make_ai_world()
    item = {"item": {"track": {"type": "audio", "index": 1}, "position": 1}}
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        assert (await _run_job(client, "smart_reframe", **item))["state"] == "succeeded"
        assert (await _run_job(client, "detect_scene_cuts"))["state"] == "succeeded"
        world.item.ai_return = False
        err, out = await _write(client, "smart_reframe", **item)
        assert err and out["code"] == "API_CALL_FAILED"


async def test_slate_color_handling(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        job = await _run_job(client, "analyze_for_slate", **CLIP, marker_color="MARKER_BLUE")
        assert job["state"] == "succeeded"
        assert [a for n, a in world.clip.calls if n == "AnalyzeForSlate"][-1] == (60,)
        err, _ = await _write(client, "analyze_for_slate", **CLIP, marker_color="MARKER_NONE")
        assert err
        err, _ = await _write(client, "analyze_for_slate", marker_color="MARKER_BLUE")
        assert err  # no target
    missing = make_ai_world(constants=False)
    async with mcp_client(missing.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "analyze_for_slate", **CLIP, marker_color="MARKER_BLUE")
        assert err and out["code"] == "CAPABILITY_UNAVAILABLE"


async def test_version_and_edition_gates(settings: Settings) -> None:
    old = make_ai_world()
    old.resolve.GetVersion = lambda: [21, 1, 0, 5, ""]  # type: ignore[method-assign]
    async with mcp_client(old.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "transcribe_audio", **CLIP)
        assert err and out["code"] == "CAPABILITY_UNAVAILABLE"
    free = make_ai_world()
    free.resolve.IsStudio = lambda: False  # type: ignore[method-assign]
    async with mcp_client(free.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "transcribe_audio", **CLIP)
        assert err and out["code"] == "STUDIO_REQUIRED"
        assert not any(n == "Folder.TranscribeAudio" for n, _ in free.resolve.all_calls)


# -- synchronous analysis ---------------------------------------------------------------------


async def test_sync_analysis_actions(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(
            client, "analyze_for_intellisearch", **CLIP, identify_faces=True, better_mode=True
        )
        assert not err and out["status"] == "applied"
        assert [a for n, a in world.clip.calls if n == "AnalyzeForIntellisearch"][-1] == (
            True,
            True,
        )
        err, out = await _write(client, "perform_audio_classification", folder={})
        assert not err and out["receipt"]["result"]["target"] == "folder:Master"
        world.clip.ai_return = EXTRAS_ERROR
        err, out = await _write(client, "analyze_for_intellisearch", **CLIP)
        assert err and out["code"] == "EXTRAS_MISSING"


async def test_clear_audio_classification_needs_confirmation(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, planned = await call(
            client, "ai_delete", {"op": {"action": "clear_audio_classification", **CLIP}}
        )
        assert not err and planned["status"] == "planned"
        assert not any(n == "ClearAudioClassification" for n, _ in world.clip.calls)
        err, out = await _delete(client, "clear_audio_classification", **CLIP)
        assert not err and out["status"] == "applied"
        assert any(n == "ClearAudioClassification" for n, _ in world.clip.calls)


async def test_remove_motion_blur(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(
            client, "remove_motion_blur", **CLIP, options={"UseExtremeMode": False}
        )
        assert not err and out["receipt"]["verified"] is True
        assert len(out["receipt"]["result"]["created_clip_ids"]) == 1
        err, out = await _write(client, "remove_motion_blur", folder={})
        assert not err and len(out["receipt"]["result"]["created_clip_ids"]) == 1
        world.clip.ai_return = False
        err, out = await _write(client, "remove_motion_blur", **CLIP)
        assert err and out["code"] == "API_CALL_FAILED"


# -- speech ----------------------------------------------------------------------------------------


async def test_generate_speech(settings: Settings, tmp_path: Path) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(
            client, "generate_speech", text="Bonjour", speed=1.5, add_to_timeline=True
        )
        assert not err and out["receipt"]["verified"] is True
        sent = [a for n, a in world.resolve.all_calls if n == "Project.GenerateSpeech"][-1][0]
        assert sent["TextInput"] == "Bonjour" and sent["Speed"] == 1.5 and sent["AddToTimeline"]
        err, _ = await _write(client, "generate_speech", text="x" * 351)
        assert err
        err, _ = await _write(client, "generate_speech", text="")
        assert err
        err, out = await _write(client, "generate_speech", text="hi", voice_model="Custom Voice")
        assert err and out["code"] == "INVALID_PARAMS"
        err, out = await _write(
            client, "generate_speech", text="hi", custom_voice_file="/etc/hosts"
        )
        assert err and out["code"] == "PATH_NOT_ALLOWED"
        voice = tmp_path / "voice.wav"
        voice.write_bytes(b"x")
        err, _ = await _write(
            client,
            "generate_speech",
            text="hi",
            voice_model="Custom Voice",
            custom_voice_file=str(voice),
        )
        assert not err
        world.speech_return = EXTRAS_ERROR
        err, out = await _write(client, "generate_speech", text="hi")
        assert err and out["code"] == "EXTRAS_MISSING"


# -- transcription ---------------------------------------------------------------------------------


async def test_get_transcription_pagination(settings: Settings) -> None:
    world = make_ai_world()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _read(client, "get_transcription", **CLIP)
        assert not err and out["result"]["transcribed"] is False
        world.clip.TranscribeAudio()
        err, out = await _read(client, "get_transcription", **CLIP, limit=2)
        page = out["result"]
        assert not err and page["total_segments"] == 5 and page["next_offset"] == 2
        assert len(page["segments"]) == 2 and page["segments"][0]["words"] is None
        err, out = await _read(
            client, "get_transcription", **CLIP, offset=4, limit=2, include_words=True
        )
        page = out["result"]
        assert page["next_offset"] is None and page["segments"][0]["words"][0]["text"] == "w"
        assert page["language"] == "fr"
        err, _ = await _read(client, "get_transcription", clip={"name": "ghost"})
        assert err


async def test_clear_transcription_plan_and_confirm(settings: Settings) -> None:
    world = make_ai_world()
    world.clip.TranscribeAudio()
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, planned = await call(
            client, "ai_delete", {"op": {"action": "clear_transcription", **CLIP}}
        )
        assert not err and planned["status"] == "planned"
        assert world.clip.transcription
        err, out = await _delete(client, "clear_transcription", **CLIP)
        assert not err and out["receipt"]["verified"] is True
        assert world.clip.transcription == {}


async def test_clear_transcription_verification_failure_and_folder(settings: Settings) -> None:
    world: AIWorld = make_ai_world()
    world.clip.TranscribeAudio()
    world.clip.lie = True
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _delete(client, "clear_transcription", folder={})
        assert not err and out["receipt"]["verified"] is False
        world.clip.lie = False
        err, out = await _delete(client, "clear_transcription", folder={})
        assert not err and out["receipt"]["verified"] is True


async def test_job_status_values_are_trusted(settings: Settings) -> None:
    world = make_ai_world()
    world.status["GetDetectSceneCutsStatus"] = [
        {"JobStatus": "Analyzing", "CompletionPercentage": 10},
        {"JobStatus": "Cancelled"},
    ]
    async with mcp_client(world.resolve, settings, ACTIONS) as client:
        err, out = await _write(client, "detect_scene_cuts")
        assert not err
        err, waited = await call(client, "job_wait", {"job_id": out["job"]["id"], "timeout_s": 10})
        assert not err and waited["state"] == "failed" and "Cancelled" in waited["error"]
