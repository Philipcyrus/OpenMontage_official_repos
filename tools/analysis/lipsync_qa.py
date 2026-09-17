"""Conservative pre-compose lip-sync QA for generated speaking clips.

The tool performs deterministic timing analysis and extracts densely sampled
frames around speech. The agent reviews those frames and may invoke the tool a
second time with ``visual_observation`` to receive a bounded classification.
It deliberately does not claim phoneme-level accuracy from still frames.
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
_OFFSET_TOLERANCE_SECONDS = 0.30
_DURATION_TOLERANCE_SECONDS = 0.15
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
    max_samples: int = 16,
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
            cursor += 0.35
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
        "reason": "Mouth visibility, articulation, and onset timing passed the conservative rubric.",
        "measured_av_offset_seconds": measured_offset,
    }


class LipSyncQA(BaseTool):
    name = "lipsync_qa"
    version = "0.1.0"
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
            "video_path": {"type": "string"},
            "audio_path": {"type": "string"},
            "scene_id": {"type": "string"},
            "output_dir": {"type": "string"},
            "expected_audio_offset_seconds": {"type": "number", "default": 0},
            "max_samples": {"type": "integer", "minimum": 6, "maximum": 24, "default": 16},
            "visual_observation": {
                "type": "object",
                "properties": {
                    "mouth_visible_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                    "active_speech_samples": {"type": "integer", "minimum": 0},
                    "closed_mouth_active_samples": {"type": "integer", "minimum": 0},
                    "distinct_mouth_shapes": {"type": "integer", "minimum": 0},
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
                max_samples=int(inputs.get("max_samples", 16)),
            )
            output_dir = Path(
                inputs.get("output_dir")
                or video_path.parent / "lipsync_qa" / video_path.stem
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            frames = self._extract_frames(video_path, timestamps, output_dir)
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
                "scene_id": inputs.get("scene_id"),
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
