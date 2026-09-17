"""Target-duration guardrails for the Panda ffmpeg renderer."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lib.i2v_duration import allocate_scene_durations
from tools.video.panda_render import (
    PandaRender,
    _target_duration_error,
    expected_timeline_duration,
)


def test_expected_timeline_duration_accounts_for_crossfade_overlap() -> None:
    scenes = [{"duration_s": 9.0} for _ in range(5)]
    assert expected_timeline_duration(
        scenes, {"type": "cut", "duration_s": 0.5}
    ) == pytest.approx(45.0)
    assert expected_timeline_duration(
        scenes, {"type": "xfade", "duration_s": 0.5}
    ) == pytest.approx(43.0)


def test_target_guard_rejects_the_reported_33_second_timeline() -> None:
    error = _target_duration_error(33.208, 45.0, 0.05)
    assert error is not None
    assert "42.750-47.250s" in error
    assert _target_duration_error(45.0, 45.0, 0.05) is None


def _color_clip(path: Path, duration: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=12",
            "-t",
            str(duration),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_regression_clips_render_inside_45_second_target(tmp_path: Path) -> None:
    source_durations = [7, 7, 10, 5, 4]
    audio_ends = [6.531, 6.766, 9.639, 4.911, 3.788]
    allocation = allocate_scene_durations(
        [
            {
                "scene_id": f"sc{i + 1}",
                "audio_end_seconds": audio_ends[i],
                "vo_seconds": audio_ends[i],
                "allowed_durations": list(range(5, 11)),
                "planned_duration_seconds": 9,
            }
            for i in range(5)
        ],
        45,
    )
    clips: list[Path] = []
    for index, duration in enumerate(source_durations):
        path = tmp_path / f"source-{index}.mp4"
        _color_clip(path, duration)
        clips.append(path)

    output = tmp_path / "final.mp4"
    result = PandaRender().execute(
        {
            "profile": "ugc",
            "resolution": "64x64",
            "fps": 12,
            "transition": {"type": "cut", "duration_s": 0},
            "scenes": [
                {
                    "media_path": str(path),
                    "duration_s": scene["effective_duration_seconds"],
                    "source_duration_s": source_durations[index],
                    "audio_end_s": audio_ends[index],
                    "audio_lipsync": index in {1, 2, 3},
                }
                for index, (path, scene) in enumerate(
                    zip(clips, allocation["scenes"])
                )
            ],
            "target_duration_s": 45,
            "duration_tolerance_fraction": 0.05,
            "output_path": str(output),
            "run_id": "duration-regression",
        }
    )

    assert result.success, result.error
    assert result.data is not None
    assert 42.75 <= result.data["duration_seconds"] <= 47.25
    assert result.data["expected_timeline_duration_seconds"] == pytest.approx(45)


def test_panda_render_preflight_rejects_unbudgeted_crossfade(tmp_path: Path) -> None:
    result = PandaRender().execute(
        {
            "scenes": [
                {"media_path": str(tmp_path / f"missing-{i}.mp4"), "duration_s": 9}
                for i in range(5)
            ],
            "transition": {"type": "xfade", "duration_s": 0.75},
            "target_duration_s": 45,
            "duration_tolerance_fraction": 0.05,
            "output_path": str(tmp_path / "never-rendered.mp4"),
        }
    )

    assert result.success is False
    assert result.error is not None
    assert "duration preflight failed" in result.error


def test_preflight_rejects_frozen_mouth_during_active_speech() -> None:
    with pytest.raises(ValueError, match="frozen hold would cover active speech"):
        expected_timeline_duration(
            [
                {
                    "duration_s": 9,
                    "source_duration_s": 7,
                    "audio_end_s": 8,
                    "audio_lipsync": True,
                }
            ],
            {"type": "cut", "duration_s": 0},
        )
