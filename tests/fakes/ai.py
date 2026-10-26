# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Fakes for the audio and ai domains: audio items/tracks, AI-capable clips and folders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from tests.fakes.fake_resolve import (
    FakeFolder,
    FakeMediaPoolItem,
    FakeResolve,
    FakeTimeline,
    FakeTimelineItem,
    make_resolve,
    recorded,
)

EXTRAS_ERROR = "Required package 'AI Slate ID' is not installed"

CONSTANTS: dict[str, int] = {
    "SUBTITLE_LANGUAGE": 1,
    "SUBTITLE_CAPTION_PRESET": 2,
    "SUBTITLE_CHARS_PER_LINE": 3,
    "SUBTITLE_LINE_BREAK": 4,
    "SUBTITLE_GAP": 5,
    "AUTO_CAPTION_AUTO": 10,
    "AUTO_CAPTION_FRENCH": 11,
    "AUTO_CAPTION_SUBTITLE_DEFAULT": 20,
    "AUTO_CAPTION_LINE_DOUBLE": 30,
    "NORMALIZE_AUDIO_SET_LEVEL_RELATIVE": 40,
    "NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT": 41,
    "DIALOGUE_LEVELER_MODE_ALLOW_WIDER_DYNAMICS": 50,
    "MARKER_BLUE": 60,
}


class AIClip(FakeMediaPoolItem):
    live_class = "MediaPoolItem"

    def __init__(self, root: FakeResolve, name: str = "clip.mov", path: str = "") -> None:
        super().__init__(root, name)
        self._props["File Path"] = path
        self.transcription: dict[str, Any] = {}
        self.audio_mapping = json.dumps({"embedded_audio_channels": 2})
        self.ai_return: Any = True  # True, False or an error string
        self.lie = False

    @recorded
    def TranscribeAudio(self, speaker: bool | None = None, nested: bool = False) -> Any:
        if self.ai_return is True:
            self.transcription = {
                "language": "fr",
                "segments": [
                    {
                        "start": f"01:00:0{i}:00",
                        "end": f"01:00:0{i}:20",
                        "text": f"seg {i}",
                        "speaker": None,
                        "words": [{"start": "01:00:00:00", "end": "01:00:00:05", "text": "w"}],
                    }
                    for i in range(5)
                ],
            }
        return self.ai_return

    @recorded
    def ClearTranscription(self, nested: bool = False) -> Any:
        if self.ai_return is True and not self.lie:
            self.transcription = {}
        return self.ai_return

    @recorded
    def GetTranscription(self, nested: bool = False) -> dict[str, Any]:
        return dict(self.transcription)

    @recorded
    def PerformAudioClassification(self) -> Any:
        return self.ai_return

    @recorded
    def ClearAudioClassification(self) -> Any:
        return self.ai_return

    @recorded
    def AnalyzeForIntellisearch(self, faces: bool, better: bool) -> Any:
        return self.ai_return

    @recorded
    def AnalyzeForSlate(self, color: Any) -> Any:
        return self.ai_return

    @recorded
    def RemoveMotionBlur(self, options: dict[str, Any]) -> Any:
        if self.ai_return is not True:
            return None if self.ai_return is False else self.ai_return
        return AIClip(self._root, f"{self._name} Deblur")

    @recorded
    def GetAudioMapping(self) -> str:
        return self.audio_mapping

    @recorded
    def SetAudioMapping(self, mapping: str) -> bool:
        if not self.lie:
            self.audio_mapping = mapping
        return True


class AIFolder(FakeFolder):
    live_class = "Folder"

    def _each(self, name: str, *args: Any) -> Any:
        clips = [c for c in self.clips if isinstance(c, AIClip)]
        results = [getattr(c, name)(*args) for c in clips]
        for sub in self.subfolders:
            if isinstance(sub, AIFolder):
                results.append(sub._each(name, *args))
        bad = [r for r in results if r is not True]
        return bad[0] if bad else True

    @recorded
    def TranscribeAudio(self, speaker: bool | None = None, nested: bool = False) -> Any:
        return self._each("TranscribeAudio")

    @recorded
    def ClearTranscription(self) -> Any:
        return self._each("ClearTranscription")

    @recorded
    def PerformAudioClassification(self) -> Any:
        return self._each("PerformAudioClassification")

    @recorded
    def ClearAudioClassification(self) -> Any:
        return self._each("ClearAudioClassification")

    @recorded
    def AnalyzeForIntellisearch(self, faces: bool, better: bool) -> Any:
        return self._each("AnalyzeForIntellisearch", faces, better)

    @recorded
    def AnalyzeForSlate(self, color: Any) -> Any:
        return self._each("AnalyzeForSlate", color)

    @recorded
    def RemoveMotionBlur(self, options: dict[str, Any] | None = None) -> Any:
        return [[c.RemoveMotionBlur(options or {})] for c in self.clips if isinstance(c, AIClip)]


class AIItem(FakeTimelineItem):
    live_class = "TimelineItem"

    def __init__(self, root: FakeResolve, name: str, start: int, end: int) -> None:
        super().__init__(root, name, start, end)
        self.voice: dict[str, Any] = {"isEnabled": False, "amount": 50}
        self.source_mapping = json.dumps({"channels": [1, 2]})
        self.audio_props: dict[str, Any] = {"AudioVolume": 0.0, "AudioPan": 0.0}
        self.ai_return: Any = True
        self.lie = False

    @recorded
    def GetVoiceIsolationState(self) -> dict[str, Any]:
        return dict(self.voice)

    @recorded
    def SetVoiceIsolationState(self, state: dict[str, Any]) -> bool:
        if not self.lie:
            self.voice = dict(state)
        return True

    @recorded
    def GetSourceAudioChannelMapping(self) -> str:
        return self.source_mapping

    @recorded
    def SetSourceAudioChannelMapping(self, mapping: str) -> bool:
        if not self.lie:
            self.source_mapping = mapping
        return True

    @recorded
    def GetProperties(self) -> dict[str, Any]:
        return {**self._props, **self.audio_props}

    @recorded
    def SetProperties(self, props: dict[str, Any]) -> bool:
        if not self.lie:
            self.audio_props.update(props)
        return True

    @recorded
    def SmartReframe(self) -> Any:
        return self.ai_return


class AITimeline(FakeTimeline):
    live_class = "Timeline"

    def __init__(self, root: FakeResolve, name: str) -> None:
        super().__init__(root, name, tracks=1, items_per_track=0)
        self.audio_tracks: list[list[AIItem]] = [[AIItem(root, "audio_1", 86400, 86500)]]
        self.track_voice: dict[int, dict[str, Any]] = {1: {"isEnabled": False, "amount": 100}}
        self.ai_return: Any = True
        self.lie = False

    @recorded
    def GetTrackCount(self, track_type: str) -> int:
        return len(self.audio_tracks) if track_type == "audio" else 0

    @recorded
    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        if track_type != "audio" or not 1 <= index <= len(self.audio_tracks):
            return []
        return list(self.audio_tracks[index - 1])

    @recorded
    def GetVoiceIsolationState(self, track: int) -> dict[str, Any]:
        return dict(self.track_voice.get(track, {}))

    @recorded
    def SetVoiceIsolationState(self, track: int, state: dict[str, Any]) -> bool:
        if track not in self.track_voice:
            return False
        if not self.lie:
            self.track_voice[track] = dict(state)
        return True

    @recorded
    def GetNormalizeAudioModes(self) -> list[str]:
        return ["Sample Peak Program", "True Peak Program", "ITU-R BS.1770-4"]

    @recorded
    def NormalizeAudioLevel(self, items: list[Any], options: dict[str, Any] | None = None) -> bool:
        return not self.lie

    @recorded
    def CreateSubtitlesFromAudio(self, settings: dict[Any, Any] | None = None) -> Any:
        return self.ai_return

    @recorded
    def DetectSceneCuts(self) -> Any:
        return self.ai_return


@dataclass
class AIWorld:
    resolve: FakeResolve
    timeline: AITimeline
    folder: AIFolder
    clip: AIClip
    item: AIItem
    status: dict[str, list[dict[str, Any]]] = field(default_factory=dict[str, list[dict[str, Any]]])
    presets: list[str] = field(default_factory=lambda: ["Podcast", "Cinema"])
    applied_presets: list[str] = field(default_factory=list[str])
    speech_return: Any = None


_STATUS_GETTERS = [
    "GetTranscribeAudioStatus",
    "GetDetectSceneCutsStatus",
    "GetSmartReframeStatus",
    "GetCreateSubtitlesFromAudioStatus",
    "GetAnalyzeForSlateStatus",
]


def make_ai_world(file_path: str = "", *, constants: bool = True) -> AIWorld:
    """A fake Resolve with one audio-capable timeline, one folder with one clip."""
    resolve = make_resolve(timelines=0)
    project = resolve.project_manager._current
    assert project is not None
    timeline = AITimeline(resolve, "TL")
    project.timelines = [timeline]
    project._current = timeline
    pool = project.media_pool
    folder = AIFolder(resolve)
    clip = AIClip(resolve, "interview.wav", file_path)
    folder.clips.append(clip)
    pool.root_folder = folder
    pool._current = folder
    project.GetUniqueId = lambda: "project-1"  # type: ignore[attr-defined]
    world = AIWorld(resolve, timeline, folder, clip, timeline.audio_tracks[0][0])

    for getter in _STATUS_GETTERS:
        world.status[getter] = [{"JobStatus": "Complete", "CompletionPercentage": 100}]
        setattr(project, getter, _status_fn(world, getter))
    project.ApplyFairlightPresetToCurrentTimeline = lambda name: (  # type: ignore[attr-defined]
        world.applied_presets.append(name) or True
    )

    def generate_speech(settings: dict[str, Any]) -> Any:
        world.resolve.all_calls.append(("Project.GenerateSpeech", (settings,)))
        if world.speech_return is not None:
            return world.speech_return
        return AIClip(resolve, "speech.wav")

    project.GenerateSpeech = generate_speech  # type: ignore[attr-defined]
    resolve.GetFairlightPresets = lambda: list(world.presets)  # type: ignore[attr-defined]
    if constants:
        for name, value in CONSTANTS.items():
            setattr(resolve, name, value)
    return world


def _status_fn(world: AIWorld, getter: str) -> Any:
    def status() -> dict[str, Any]:
        script = world.status[getter]
        return dict(script.pop(0) if len(script) > 1 else script[0])

    return status
