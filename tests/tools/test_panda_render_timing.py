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


def test_preflight_counts_head_trim_against_source_motion() -> None:
    scene = {
        "duration_s": 3.25,
        "source_duration_s": 5.0,
        "audio_end_s": 3.0,
        "audio_lipsync": True,
        "source_in_s": 0.4,
    }
    assert expected_timeline_duration(
        [scene], {"type": "cut", "duration_s": 0}
    ) == pytest.approx(3.25)
    with pytest.raises(ValueError, match="frozen hold would cover active speech"):
        expected_timeline_duration(
            [{**scene, "source_in_s": 2.5}], {"type": "cut", "duration_s": 0}
        )
    with pytest.raises(ValueError, match="source_in_s cannot be negative"):
        expected_timeline_duration(
            [{**scene, "source_in_s": -0.1}], {"type": "cut", "duration_s": 0}
        )


def _silent_voice(path: Path) -> Path:
    path.write_bytes(b"")
    return path


def test_panda_render_refuses_overlapping_voice_tracks(tmp_path: Path) -> None:
    narrator = _silent_voice(tmp_path / "vo-s21-narrator.mp3")
    panda = _silent_voice(tmp_path / "vo-s22-panda.mp3")
    result = PandaRender().execute(
        {
            "scenes": [{"media_path": str(tmp_path / "never-staged.mp4"), "duration_s": 8}],
            "transition": {"type": "cut", "duration_s": 0},
            "audio": {
                "voice_tracks": [
                    {"path": str(narrator), "at_s": 0.0, "duration_s": 3.1,
                     "section_id": "s21"},
                    {"path": str(panda), "at_s": 2.0, "duration_s": 4.2,
                     "section_id": "s22"},
                ]
            },
            "output_path": str(tmp_path / "never-rendered.mp4"),
        }
    )
    assert result.success is False
    assert "voice_tracks overlap" in (result.error or "")
    assert "s21" in result.error and "s22" in result.error


def test_panda_render_rejects_more_cuts_than_montage_accepts(tmp_path: Path) -> None:
    result = PandaRender().execute(
        {
            "scenes": [
                {"media_path": str(tmp_path / f"cut-{i}.mp4"), "duration_s": 1}
                for i in range(61)
            ],
            "transition": {"type": "cut", "duration_s": 0},
            "output_path": str(tmp_path / "never-rendered.mp4"),
        }
    )
    assert result.success is False
    assert "at most 60" in (result.error or "")


def test_voice_tracks_schema_documents_sequential_contract() -> None:
    audio = PandaRender.input_schema["properties"]["audio"]["properties"]
    item = audio["voice_tracks"]["items"]["properties"]
    assert {"path", "at_s", "duration_s", "section_id"} <= set(item)
    assert audio["allow_voice_overlap"]["default"] is False
    scene = PandaRender.input_schema["properties"]["scenes"]["items"]["properties"]
    assert scene["source_in_s"]["minimum"] == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_subshot_cuts_render_with_sequential_voices(tmp_path: Path) -> None:
    from lib.i2v_duration import build_scene_subshots, place_scene_subshots

    def _tone(path: Path, seconds: float) -> Path:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-c:a", "libmp3lame", str(path)],
            check=True, capture_output=True,
        )
        return path

    vo = {
        "s1": _tone(tmp_path / "vo-s1-narrator.mp3", 1.5),
        "s2": _tone(tmp_path / "vo-s2-panda.mp3", 2.0),
    }
    plan = build_scene_subshots(
        "sc1",
        [
            {"section_id": "s1", "speaker": "narrator", "measured_seconds": 1.5,
             "script_start_seconds": 0.0, "script_end_seconds": 1.0, "path": str(vo["s1"])},
            {"section_id": "s2", "speaker": "panda", "measured_seconds": 2.0,
             "script_start_seconds": 1.0, "script_end_seconds": 2.5, "path": str(vo["s2"]),
             "generated_i2v_duration": 5},
        ],
        allowed_durations=[5],
    )
    placed = place_scene_subshots(
        plan, effective_scene_start_seconds=0.0, effective_duration_seconds=5.0,
    )
    clip = tmp_path / "sc1-s2.mp4"
    _color_clip(clip, 5)
    still = tmp_path / "sc1-still.mp4"
    _color_clip(still, 5)
    scenes = []
    for cut in placed["subshots"]:
        speaking = cut["kind"] == "speaking"
        scenes.append({
            "media_path": str(clip if speaking else still),
            "duration_s": cut["duration_seconds"],
            "source_duration_s": 5,
            "audio_end_s": cut["audio_end_seconds"],
            "audio_lipsync": speaking,
            "source_in_s": cut["source_in_seconds"],
        })
    result = PandaRender().execute(
        {
            "profile": "ugc",
            "resolution": "64x64",
            "fps": 12,
            "transition": {"type": "cut", "duration_s": 0},
            "scenes": scenes,
            "audio": {"voice_tracks": [
                {k: t[k] for k in ("path", "at_s", "duration_s", "section_id")}
                for t in placed["voice_tracks"]
            ]},
            "target_duration_s": 5,
            "duration_tolerance_fraction": 0.05,
            "output_path": str(tmp_path / "final.mp4"),
            "run_id": "subshot-render",
        }
    )
    assert result.success, result.error
    assert result.data["voice_track_count"] == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_missing_duration_probe_is_not_a_target_band_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ffprobe omits duration, fail with probe-unavailable — not a ±5% band error."""
    import tools.video._shared as shared

    clip = tmp_path / "sc1.mp4"
    _color_clip(clip, 5)
    output = tmp_path / "final.mp4"

    real_probe = shared.probe_output

    def _probe_without_duration(path: Path) -> dict:
        info = dict(real_probe(path))
        info.pop("duration_seconds", None)
        return info

    monkeypatch.setattr(shared, "probe_output", _probe_without_duration)

    result = PandaRender().execute(
        {
            "profile": "ugc",
            "resolution": "64x64",
            "fps": 12,
            "transition": {"type": "cut", "duration_s": 0},
            "scenes": [{"media_path": str(clip), "duration_s": 5}],
            "target_duration_s": 5,
            "duration_tolerance_fraction": 0.05,
            "run_id": "probe-missing-test",
            "output_path": str(output),
        }
    )

    assert result.success is False
    assert "duration probe unavailable" in (result.error or "")
    assert "postflight failed" not in (result.error or "")
    assert result.artifacts == [str(output)]
    assert output.is_file()
