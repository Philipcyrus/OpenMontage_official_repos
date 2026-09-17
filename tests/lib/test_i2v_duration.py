"""Unit tests for TTS → Higgsfield duration snapping."""

from __future__ import annotations

import pytest

from lib.i2v_duration import (
    DurationAllocationError,
    allocate_scene_durations,
    snap_i2v_duration,
)


def test_vo_under_min_uses_min_no_hold():
    out = snap_i2v_duration(2.3, min_s=5, max_s=10)
    assert out["i2v_duration"] == 5
    assert out["hold_extend_seconds"] == 0.0


def test_vo_between_allowed_picks_ceil_cover():
    out = snap_i2v_duration(6.1, allowed=[5, 10])
    assert out["i2v_duration"] == 10
    assert out["hold_extend_seconds"] == 0.0


def test_vo_exact_allowed():
    out = snap_i2v_duration(5.0, allowed=[5, 10])
    assert out["i2v_duration"] == 5
    assert out["hold_extend_seconds"] == 0.0


def test_vo_over_max_extends_hold():
    out = snap_i2v_duration(12.4, allowed=[5, 10])
    assert out["i2v_duration"] == 10
    assert out["hold_extend_seconds"] == 2.4


def test_contiguous_range_ceils():
    out = snap_i2v_duration(6.1, min_s=5, max_s=10)
    assert out["i2v_duration"] == 7
    assert out["hold_extend_seconds"] == 0.0


def test_zero_vo_uses_min():
    out = snap_i2v_duration(0.0, min_s=5, max_s=10)
    assert out["i2v_duration"] == 5
    assert out["hold_extend_seconds"] == 0.0


def test_full_timeline_allocator_keeps_audio_driven_45s_target():
    audio = [6.531, 6.766, 9.639, 4.911, 3.788]
    roles = [
        "establish_context",
        "introduce_subject",
        "deliver_payload",
        "resolution",
        "call_to_action",
    ]
    out = allocate_scene_durations(
        [
            {
                "scene_id": f"sc{i + 1}",
                "vo_seconds": seconds,
                "audio_end_seconds": seconds,
                "allowed_durations": list(range(5, 11)),
                "planned_duration_seconds": 9,
                "narrative_role": roles[i],
            }
            for i, seconds in enumerate(audio)
        ],
        45,
    )

    assert out["status"] == "within_target_band"
    assert out["within_target_band"] is True
    assert out["output_duration_seconds"] == pytest.approx(45)
    assert 42.75 <= out["output_duration_seconds"] <= 47.25
    durations = [scene["effective_duration_seconds"] for scene in out["scenes"]]
    assert sum(durations) == pytest.approx(45)
    assert len(set(durations)) > 1, "audio-driven scenes need not use equal 9s slots"
    for scene, minimum in zip(out["scenes"], audio):
        assert scene["i2v_duration"] >= minimum
        assert scene["tail_hold_seconds"] == 0


def test_allocator_rebuilds_cumulative_starts_and_budgets_transition_overlap():
    out = allocate_scene_durations(
        [
            {
                "scene_id": "a",
                "audio_end_seconds": 4,
                "allowed_durations": [5, 6],
                "planned_duration_seconds": 5,
            },
            {
                "scene_id": "b",
                "audio_end_seconds": 4,
                "allowed_durations": [5, 6],
                "planned_duration_seconds": 5,
            },
        ],
        10,
        transition_overlap_seconds=0.5,
    )

    assert out["within_target_band"] is True
    assert out["output_duration_seconds"] == pytest.approx(10.5)
    assert out["scenes"][1]["effective_start_seconds"] == pytest.approx(
        out["scenes"][0]["effective_end_seconds"] - 0.5
    )


def test_allocator_locks_an_approved_motion_sample_duration():
    out = allocate_scene_durations(
        [
            {
                "scene_id": "sample",
                "audio_end_seconds": 6.2,
                "fixed_i2v_duration": 7,
                "planned_duration_seconds": 9,
            },
            {
                "scene_id": "remaining",
                "audio_end_seconds": 4,
                "allowed_durations": [5, 6, 7, 8, 9, 10],
                "planned_duration_seconds": 9,
            },
        ],
        16,
    )

    assert out["scenes"][0]["i2v_duration"] == 7
    assert out["within_target_band"] is True


def test_allocator_uses_only_bounded_post_speech_holds():
    out = allocate_scene_durations(
        [
            {
                "scene_id": "a",
                "audio_end_seconds": 4,
                "allowed_durations": [5],
                "planned_duration_seconds": 5,
            },
            {
                "scene_id": "b",
                "audio_end_seconds": 4,
                "allowed_durations": [5],
                "planned_duration_seconds": 5,
            },
        ],
        12,
        max_tail_hold_seconds=1,
    )

    assert out["status"] == "within_target_band_with_holds"
    assert out["output_duration_seconds"] == pytest.approx(12)
    assert all(scene["tail_hold_seconds"] <= 1 for scene in out["scenes"])
    assert all(
        scene["effective_duration_seconds"] >= scene["audio_end_seconds"]
        for scene in out["scenes"]
    )


def test_allocator_rejects_audio_beyond_provider_maximum():
    with pytest.raises(DurationAllocationError, match="revise TTS pacing"):
        allocate_scene_durations(
            [
                {
                    "scene_id": "talking",
                    "audio_end_seconds": 10.2,
                    "allowed_durations": [5, 10],
                }
            ],
            10,
        )
