"""Conservative pre-compose lip-sync QA for generated speaking clips.

The tool performs deterministic timing analysis and extracts densely sampled
frames around speech. The agent reviews those frames and may invoke the tool a
second time with ``visual_observation`` to receive a bounded classification.
It deliberately does not claim phoneme-level accuracy from still frames.

Each call reviews one speaker: one speaking-subshot clip against that speaker's
own VO file. Scoring a clip against a mix of several voices would measure the
onset of whichever voice speaks first, not the one the mouth follows.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


_SILENCE_RE = re.compile(r"silence_(start|end):\s*([0-9.]+)")
_OFFSET_TOLERANCE_SECONDS = 0.20
_DURATION_TOLERANCE_SECONDS = 0.15
_SAMPLE_STEP_SECONDS = 0.25
_TAIL_FRACTION = 0.4
_LISTENER_OPEN_MAX_RATIO = 0.3
_MAX_ABS_AUDIO_OFFSET_SECONDS = 2.0
_PROBE_TIMEOUT_SECONDS = 15
_SILENCE_DETECT_TIMEOUT_SECONDS = 60
_FRAME_EXTRACT_TIMEOUT_SECONDS = 30


def _clamp_audio_offset(value: float) -> float:
    """Bound signed lip-sync audio offsets to a safe correction window."""
    return round(
        max(
            -_MAX_ABS_AUDIO_OFFSET_SECONDS,
            min(_MAX_ABS_AUDIO_OFFSET_SECONDS, float(value)),
        ),
        3,
    )


def speech_intervals_from_silence(
    duration: float, silence_output: str
) -> list[tuple[float, float]]:
    """Return non-silent intervals from FFmpeg silencedetect output."""
    if duration <= 0:
        return []
    silences: list[tuple[float, float]] = []
    open_start: float | None = None
    for kind, raw_value in _SILENCE_RE.findall(silence_output):
        value = min(max(float(raw_value), 0.0), duration)
        if kind == "start":
            open_start = value
        elif open_start is not None:
            if value > open_start:
                silences.append((open_start, value))
            open_start = None
    if open_start is not None and open_start < duration:
        silences.append((open_start, duration))

    active: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(silences):
        if start > cursor:
            active.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        active.append((cursor, duration))
    return [(round(start, 3), round(end, 3)) for start, end in active if end - start >= 0.04]


def build_sample_timestamps(
    intervals: list[tuple[float, float]],
    *,
    expected_offset: float,
    clip_duration: float,
    max_samples: int = 20,
) -> list[float]:
    """Sample pre-speech, onset, active speech, and post-speech frames."""
    if not intervals or clip_duration <= 0:
        return []
    onset = intervals[0][0]
    speech_end = intervals[-1][1]
    relative: list[float] = [
        max(0.0, onset - 0.15),
        onset + 0.10,
        onset + 0.30,
    ]
    for start, end in intervals:
        cursor = start + 0.15
        while cursor < end:
            relative.append(cursor)
            cursor += _SAMPLE_STEP_SECONDS
    relative.append(max(onset, speech_end - 0.10))
    relative.append(speech_end + 0.15)

    upper = max(clip_duration - 0.04, 0.0)
    timestamps = sorted(
        {
            round(min(max(expected_offset + value, 0.0), upper), 3)
            for value in relative
        }
    )
    if len(timestamps) > max_samples:
        step = (len(timestamps) - 1) / (max_samples - 1)
        timestamps = [timestamps[round(index * step)] for index in range(max_samples)]
    return timestamps


def label_sample_phase(
    timestamp: float,
    intervals: list[tuple[float, float]],
    *,
    expected_offset: float,
) -> str:
    """Where a clip timestamp falls in the line: pre, onset, active, pause, tail or post.

    ``tail`` is active speech in the last 40% of the line, where a mouth that gave up early
    shows; ``pause`` is a gap between words longer than the sampling step.
    """
    if not intervals:
        return "post"
    rel = timestamp - expected_offset
    onset = intervals[0][0]
    speech_end = intervals[-1][1]
    if rel < onset:
        return "pre"
    if rel > speech_end:
        return "post"
    if not any(start <= rel <= end for start, end in intervals):
        return "pause"
    if rel <= onset + 0.30:
        return "onset"
    if rel >= onset + (1.0 - _TAIL_FRACTION) * (speech_end - onset):
        return "tail"
    return "active"


def classify_lipsync(
    *,
    clip_duration: float,
    speech_end: float | None,
    expected_offset: float,
    observation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify concrete timing/articulation evidence supplied by visual review."""
    if speech_end is None:
        return {
            "status": "inconclusive",
            "reason": "No active speech interval was detected in the VO reference.",
        }
    if expected_offset + speech_end > clip_duration + _DURATION_TOLERANCE_SECONDS:
        return {
            "status": "fail_generation",
            "reason": "The generated clip does not cover the complete active speech interval.",
        }
    if observation is None:
        return {
            "status": "needs_visual_review",
            "reason": "Timing is valid; sampled mouth frames require agent review.",
        }

    visible_ratio = float(observation.get("mouth_visible_ratio", 0.0))
    active_samples = int(observation.get("active_speech_samples", 0))
    closed_samples = int(observation.get("closed_mouth_active_samples", 0))
    distinct_shapes = int(observation.get("distinct_mouth_shapes", 0))
    observed_onset = observation.get("observed_mouth_onset_seconds")

    if visible_ratio < 0.8:
        return {
            "status": "fail_generation",
            "reason": "The mouth is not clearly visible in at least 80% of active-speech samples.",
        }
    if active_samples < 3:
        return {
            "status": "inconclusive",
            "reason": "Fewer than three active-speech mouth samples were reviewed.",
        }
    if distinct_shapes < 2 or closed_samples / active_samples >= 0.5:
        return {
            "status": "fail_generation",
            "reason": "Mouth articulation is flat or closed through too much active speech.",
        }
    tail_samples = int(observation.get("tail_active_samples", 0))
    tail_closed = int(observation.get("tail_closed_mouth_samples", 0))
    if tail_samples >= 2 and tail_closed / tail_samples >= 0.5:
        return {
            "status": "fail_generation",
            "reason": "The mouth stops articulating before the line ends (closed through the tail).",
        }
    listener_samples = int(observation.get("listener_visible_samples", 0))
    listener_open = int(observation.get("listener_open_mouth_samples", 0))
    if listener_samples > 0 and listener_open / listener_samples >= _LISTENER_OPEN_MAX_RATIO:
        return {
            "status": "fail_generation",
            "reason": (
                f"The listening character's mouth is open in {listener_open}/{listener_samples} "
                "samples, so both characters read as talking."
            ),
        }
    if observed_onset is None:
        return {
            "status": "inconclusive",
            "reason": "Visual review did not identify a mouth-motion onset.",
        }

    speech_onset = float(observation.get("speech_onset_seconds", 0.0))
    measured_offset = round(
        float(observed_onset) - (expected_offset + speech_onset), 3
    )
    if abs(measured_offset) > _OFFSET_TOLERANCE_SECONDS:
        corrected_offset = _clamp_audio_offset(expected_offset + measured_offset)
        return {
            "status": "fail_timing",
            "reason": (
                f"Mouth motion differs from speech onset by {measured_offset:+.3f}s, "
                f"beyond the {_OFFSET_TOLERANCE_SECONDS:.2f}s tolerance."
            ),
            "measured_av_offset_seconds": measured_offset,
            "recommended_audio_offset_seconds": corrected_offset,
        }
    return {
        "status": "pass",
        "reason": (
            "Mouth visibility, articulation through the tail, a closed listener mouth, and onset "
            "timing passed the conservative rubric."
        ),
        "measured_av_offset_seconds": measured_offset,
    }


class LipSyncQA(BaseTool):
    name = "lipsync_qa"
    version = "0.3.0"
    tier = ToolTier.CORE
    capability = "analysis"
    provider = "ffmpeg"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg", "cmd:ffprobe"]
    install_instructions = "Install FFmpeg: https://ffmpeg.org/download.html"
    agent_skills = ["ffmpeg"]
    capabilities = [
        "detect_speech_windows",
        "extract_lipsync_review_frames",
        "classify_visual_lipsync_observations",
    ]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=200)
    idempotency_key_fields = [
        "video_path",
        "audio_path",
        "expected_audio_offset_seconds",
        "visual_observation",
    ]
    side_effects = ["writes sampled review frames to output_dir"]
    user_visible_verification = ["Review sampled mouth shapes against active VO windows"]

    input_schema = {
        "type": "object",
        "required": ["video_path", "audio_path"],
        "properties": {
            "video_path": {
                "type": "string",
                "description": "One speaking subshot clip (one on-screen speaker, one line).",
            },
            "audio_path": {
                "type": "string",
                "description": (
                    "That speaker's own VO file — the clip's re-voiced line from "
                    "elevenlabs_voice_changer. Never a scene mix or another speaker's line."
                ),
            },
            "scene_id": {"type": "string"},
            "section_id": {
                "type": "string",
                "description": "Script section of the line; the report is stored under "
                               "metadata.lip_sync_qa.subshots.<scene_id>.<section_id>.",
            },
            "subshot_id": {"type": "string"},
            "speaker": {"type": "string", "enum": ["customer", "panda", "narrator"]},
            "output_dir": {"type": "string"},
            "expected_audio_offset_seconds": {"type": "number", "default": 0},
            "max_samples": {"type": "integer", "minimum": 6, "maximum": 24, "default": 20},
            "visual_observation": {
                "type": "object",
                "properties": {
                    "mouth_visible_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                    "active_speech_samples": {"type": "integer", "minimum": 0},
                    "closed_mouth_active_samples": {"type": "integer", "minimum": 0},
                    "distinct_mouth_shapes": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "A smile or grin held unchanged across frames is ONE shape.",
                    },
                    "tail_active_samples": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Frames labelled phase=tail (last 40% of the line).",
                    },
                    "tail_closed_mouth_samples": {"type": "integer", "minimum": 0},
                    "listener_visible_samples": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Active/tail frames where the other character's face shows.",
                    },
                    "listener_open_mouth_samples": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Of those, frames where the listener's lips are parted "
                                       "(open grin, laugh, talking shape).",
                    },
                    "observed_mouth_onset_seconds": {"type": "number", "minimum": 0},
                    "speech_onset_seconds": {"type": "number", "minimum": 0},
                    "notes": {"type": "string"},
                },
            },
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        video_path = Path(inputs["video_path"])
        audio_path = Path(inputs["audio_path"])
        identity = {
            "scene_id": inputs.get("scene_id"),
            "section_id": inputs.get("section_id"),
            "subshot_id": inputs.get("subshot_id"),
            "speaker": inputs.get("speaker"),
            "audio_path": str(audio_path),
        }
        if inputs.get("speaker") == "narrator":
            return ToolResult(success=True, data={
                **identity,
                "status": "skipped",
                "reason": "narrator lines are closed-mouth fills; no mouth follows them",
            })
        if not video_path.is_file():
            return ToolResult(success=False, error=f"Video not found: {video_path}")
        if not audio_path.is_file():
            return ToolResult(success=False, error=f"Audio not found: {audio_path}")

        started = time.time()
        try:
            clip_duration = self._duration(video_path)
            audio_duration = self._duration(audio_path)
            silence = self.run_command([
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(audio_path),
                "-af",
                "silencedetect=noise=-35dB:d=0.05",
                "-f",
                "null",
                os.devnull,
            ], timeout=_SILENCE_DETECT_TIMEOUT_SECONDS)
            intervals = speech_intervals_from_silence(audio_duration, silence.stderr)
            expected_offset = _clamp_audio_offset(
                float(inputs.get("expected_audio_offset_seconds", 0.0))
            )
            timestamps = build_sample_timestamps(
                intervals,
                expected_offset=expected_offset,
                clip_duration=clip_duration,
                max_samples=int(inputs.get("max_samples", 20)),
            )
            output_dir = Path(
                inputs.get("output_dir")
                or video_path.parent / "lipsync_qa" / video_path.stem
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            frames = [
                {
                    **frame,
                    "phase": label_sample_phase(
                        frame["timestamp_seconds"], intervals, expected_offset=expected_offset
                    ),
                }
                for frame in self._extract_frames(video_path, timestamps, output_dir)
            ]
            speech_onset = intervals[0][0] if intervals else None
            speech_end = intervals[-1][1] if intervals else None
            observation = inputs.get("visual_observation")
            if observation is not None and speech_onset is not None:
                observation = {**observation, "speech_onset_seconds": speech_onset}
            classification = classify_lipsync(
                clip_duration=clip_duration,
                speech_end=speech_end,
                expected_offset=expected_offset,
                observation=observation,
            )
            data = {
                **identity,
                "status": classification["status"],
                "reason": classification["reason"],
                "clip_duration_seconds": round(clip_duration, 3),
                "audio_duration_seconds": round(audio_duration, 3),
                "expected_audio_offset_seconds": expected_offset,
                "speech_onset_seconds": speech_onset,
                "speech_end_seconds": speech_end,
                "speech_intervals": [
                    {"start_seconds": start, "end_seconds": end}
                    for start, end in intervals
                ],
                "sample_timestamps": timestamps,
                "frames": frames,
                "visual_observation": observation,
                **{
                    key: value
                    for key, value in classification.items()
                    if key not in {"status", "reason"}
                },
            }
            return ToolResult(
                success=True,
                data=data,
                artifacts=[frame["path"] for frame in frames],
                duration_seconds=round(time.time() - started, 2),
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"Lip-sync analysis failed: {exc}")

    def _duration(self, path: Path) -> float:
        result = self.run_command([
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ], timeout=_PROBE_TIMEOUT_SECONDS)
        return float(result.stdout.strip().splitlines()[0])

    def _extract_frames(
        self, video_path: Path, timestamps: list[float], output_dir: Path
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for index, timestamp in enumerate(timestamps):
            frame_path = output_dir / f"frame_{index:02d}_{timestamp:.3f}s.jpg"
            self.run_command([
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(timestamp),
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(frame_path),
            ], timeout=_FRAME_EXTRACT_TIMEOUT_SECONDS)
            if frame_path.is_file():
                frames.append({"timestamp_seconds": timestamp, "path": str(frame_path)})
        return frames
