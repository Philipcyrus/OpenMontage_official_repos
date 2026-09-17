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
from math import ceil
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
_MOUTH_SHAPES = {"closed", "narrow", "rounded", "wide", "teeth", "unclear"}


def _clamp_audio_offset(value: float) -> float:
    """Bound signed lip-sync audio offsets to a safe correction window."""
    return round(
        max(
            -_MAX_ABS_AUDIO_OFFSET_SECONDS,
            min(_MAX_ABS_AUDIO_OFFSET_SECONDS, float(value)),
        ),
        3,
    )


def _mouth_sequence_metrics(shapes: list[Any]) -> dict[str, Any]:
    """Derive articulation strength from ordered active-speech mouth states."""
    normalized = [str(shape).strip().lower() for shape in shapes]
    if any(shape not in _MOUTH_SHAPES for shape in normalized):
        invalid = sorted({shape for shape in normalized if shape not in _MOUTH_SHAPES})
        raise ValueError(f"unknown active_mouth_shapes labels: {invalid}")

    known = [shape for shape in normalized if shape != "unclear"]
    transitions = sum(
        left != right
        for left, right in zip(known, known[1:])
    )
    longest_run = 0
    current_run = 0
    previous: str | None = None
    for shape in known:
        if shape == previous:
            current_run += 1
        else:
            previous = shape
            current_run = 1
        longest_run = max(longest_run, current_run)

    sample_count = len(normalized)
    known_count = len(known)
    return {
        "mouth_shape_change_count": transitions,
        "longest_static_shape_run": longest_run,
        "longest_static_shape_fraction": (
            round(longest_run / known_count, 3) if known_count else 1.0
        ),
        "unclear_mouth_shape_samples": sample_count - known_count,
        "derived_closed_mouth_active_samples": known.count("closed"),
        "derived_distinct_mouth_shapes": len(set(known)),
    }


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

    sequence = observation.get("active_mouth_shapes")
    if not isinstance(sequence, list) or len(sequence) != active_samples:
        return {
            "status": "inconclusive",
            "reason": (
                "Ordered active_mouth_shapes evidence is required and must contain "
                "one label per active-speech sample."
            ),
        }
    pre_speech_state = observation.get("pre_speech_mouth_state")
    if pre_speech_state not in {"closed", "open", "unclear"}:
        return {
            "status": "inconclusive",
            "reason": "pre_speech_mouth_state must be closed, open, or unclear.",
        }

    metrics = _mouth_sequence_metrics(sequence)
    known_samples = active_samples - metrics["unclear_mouth_shape_samples"]
    if known_samples < 3 or metrics["unclear_mouth_shape_samples"] > max(
        1, active_samples // 5
    ):
        return {
            "status": "inconclusive",
            "reason": "Too many sampled mouth states are unclear for reliable articulation QA.",
            **metrics,
        }

    closed_samples = metrics["derived_closed_mouth_active_samples"]
    distinct_shapes = metrics["derived_distinct_mouth_shapes"]
    if distinct_shapes < 2 or closed_samples / known_samples >= 0.5:
        return {
            "status": "fail_generation",
            "reason": "Mouth articulation is flat or closed through too much active speech.",
            **metrics,
        }
    if active_samples >= 6 and closed_samples == 0:
        return {
            "status": "fail_generation",
            "reason": (
                "The mouth never closes across a sustained spoken passage; continuous-open "
                "oscillation is not credible lip sync."
            ),
            **metrics,
        }
    if active_samples >= 6 and distinct_shapes < 3:
        return {
            "status": "fail_generation",
            "reason": (
                "A sustained spoken passage needs at least three useful mouth states; "
                "two-shape oscillation is too weak."
            ),
            **metrics,
        }

    minimum_changes = max(2, ceil((known_samples - 1) * 0.25))
    if metrics["mouth_shape_change_count"] < minimum_changes:
        return {
            "status": "fail_generation",
            "reason": (
                f"Only {metrics['mouth_shape_change_count']} mouth-shape changes were observed; "
                f"at least {minimum_changes} are required for this passage."
            ),
            **metrics,
        }
    if metrics["longest_static_shape_fraction"] > 0.5:
        return {
            "status": "fail_generation",
            "reason": "One mouth shape is held through more than half of the reviewed passage.",
            **metrics,
        }

    speech_onset = float(observation.get("speech_onset_seconds", 0.0))
    if (
        pre_speech_state == "open"
        and speech_onset > 0.04
        and (
            metrics["longest_static_shape_fraction"] >= 0.35
            or metrics["mouth_shape_change_count"]
            < max(3, ceil((known_samples - 1) * 0.35))
        )
    ):
        return {
            "status": "fail_generation",
            "reason": (
                "The mouth is already open before speech and subsequent articulation is "
                "too static to distinguish speaking from a held smile."
            ),
            **metrics,
        }
    if observed_onset is None:
        return {
            "status": "inconclusive",
            "reason": "Visual review did not identify a mouth-motion onset.",
            **metrics,
        }

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
            **metrics,
        }
    return {
        "status": "pass",
        "reason": "Mouth visibility, articulation, and onset timing passed the conservative rubric.",
        "measured_av_offset_seconds": measured_offset,
        **metrics,
    }


class LipSyncQA(BaseTool):
    name = "lipsync_qa"
    version = "0.2.0"
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
                "required": [
                    "mouth_visible_ratio",
                    "active_speech_samples",
                    "active_mouth_shapes",
                    "pre_speech_mouth_state",
                    "observed_mouth_onset_seconds",
                    "notes",
                ],
                "properties": {
                    "mouth_visible_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                    "active_speech_samples": {"type": "integer", "minimum": 0},
                    "closed_mouth_active_samples": {"type": "integer", "minimum": 0},
                    "distinct_mouth_shapes": {"type": "integer", "minimum": 0},
                    "active_mouth_shapes": {
                        "type": "array",
                        "minItems": 3,
                        "items": {
                            "type": "string",
                            "enum": sorted(_MOUTH_SHAPES),
                        },
                    },
                    "pre_speech_mouth_state": {
                        "type": "string",
                        "enum": ["closed", "open", "unclear"],
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
