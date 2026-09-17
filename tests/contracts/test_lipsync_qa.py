"""Contracts for Panda's bounded pre-compose lip-sync QA."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dify_launcher import runner as runner_module
from tools.analysis.lipsync_qa import (
    LipSyncQA,
    build_sample_timestamps,
    classify_lipsync,
    speech_intervals_from_silence,
)


ROOT = Path(__file__).resolve().parents[2]


def _observation(**overrides):
    value = {
        "mouth_visible_ratio": 1.0,
        "active_speech_samples": 6,
        "closed_mouth_active_samples": 2,
        "distinct_mouth_shapes": 5,
        "active_mouth_shapes": [
            "closed",
            "narrow",
            "rounded",
            "wide",
            "closed",
            "teeth",
        ],
        "pre_speech_mouth_state": "closed",
        "observed_mouth_onset_seconds": 0.7,
        "speech_onset_seconds": 0.5,
        "notes": "Face visible with changing mouth shapes.",
    }
    value.update(overrides)
    return value


def _attempt(attempt: int, take: str, status: str) -> dict:
    return {
        "attempt": attempt,
        "take": take,
        "video_asset_id": f"vid-scene-1-{take}",
        "status": status,
        "measured_av_offset_seconds": 0.6 if attempt == 1 else 0.1,
        "recommended_audio_offset_seconds": 0.6 if attempt == 1 else None,
        "job_id": None if attempt == 1 else "hf-retry-1",
        "credits": 0 if attempt == 1 else 12,
        "evidence": {
            "expected_audio_offset_seconds": 0,
            "speech_onset_seconds": 0.5,
            "speech_end_seconds": 2.5,
            "frame_paths": ["assets/analysis/scene-1/frame-01.jpg"],
            "mouth_visible_ratio": 1,
            "active_speech_samples": 6,
            "closed_mouth_active_samples": 4 if attempt == 1 else 1,
            "distinct_mouth_shapes": 1 if attempt == 1 else 3,
            "active_mouth_shapes": (
                ["closed", "closed", "closed", "closed", "narrow", "closed"]
                if attempt == 1
                else ["closed", "narrow", "rounded", "wide", "closed", "teeth"]
            ),
            "pre_speech_mouth_state": "closed",
            "mouth_shape_change_count": 3 if attempt == 1 else 5,
            "longest_static_shape_run": 4 if attempt == 1 else 1,
            "longest_static_shape_fraction": 0.667 if attempt == 1 else 0.167,
            "unclear_mouth_shape_samples": 0,
            "observed_mouth_onset_seconds": 1.1 if attempt == 1 else 0.6,
            "notes": "Observed sampled mouth states.",
        },
    }


def test_speech_windows_are_complement_of_detected_silence() -> None:
    output = """
    [silencedetect] silence_start: 0
    [silencedetect] silence_end: 0.4 | silence_duration: 0.4
    [silencedetect] silence_start: 1
    [silencedetect] silence_end: 1.3 | silence_duration: 0.3
    [silencedetect] silence_start: 2
    """
    assert speech_intervals_from_silence(3.0, output) == [(0.4, 1.0), (1.3, 2.0)]


def test_sampling_covers_onset_active_speech_and_settle() -> None:
    samples = build_sample_timestamps(
        [(0.4, 1.0), (1.3, 2.0)],
        expected_offset=0.5,
        clip_duration=3.0,
    )
    assert 0.75 in samples  # pre-onset evidence
    assert 1.0 in samples   # just after onset
    assert 2.65 in samples  # post-speech settle
    assert samples == sorted(set(samples))


def test_conservative_classification_pass_and_offset() -> None:
    passed = classify_lipsync(
        clip_duration=4,
        speech_end=2.5,
        expected_offset=0,
        observation=_observation(),
    )
    assert passed["status"] == "pass"

    delayed = classify_lipsync(
        clip_duration=4,
        speech_end=2.5,
        expected_offset=0,
        observation=_observation(observed_mouth_onset_seconds=1.1),
    )
    assert delayed["status"] == "fail_timing"
    assert delayed["measured_av_offset_seconds"] == pytest.approx(0.6)
    assert delayed["recommended_audio_offset_seconds"] == pytest.approx(0.6)

    early = classify_lipsync(
        clip_duration=4,
        speech_end=2.5,
        expected_offset=0,
        observation=_observation(observed_mouth_onset_seconds=0.0),
    )
    assert early["status"] == "fail_timing"
    assert early["measured_av_offset_seconds"] == pytest.approx(-0.5)
    assert early["recommended_audio_offset_seconds"] == pytest.approx(-0.5)


def test_negative_recommended_offset_is_clamped_not_zeroed() -> None:
    """Mouths that start early must keep a signed correction within ±2s."""
    result = classify_lipsync(
        clip_duration=8,
        speech_end=5.0,
        expected_offset=0.1,
        observation=_observation(
            observed_mouth_onset_seconds=0.0,
            speech_onset_seconds=1.0,
        ),
    )
    assert result["status"] == "fail_timing"
    assert result["measured_av_offset_seconds"] == pytest.approx(-1.1)
    assert result["recommended_audio_offset_seconds"] == pytest.approx(-1.0)


def test_bad_first_shot_calibration_is_generation_failure() -> None:
    # Calibrates the concrete flat/closed-mouth pattern observed in
    # job_aa6ca2800fcf scene 1; it must trigger one articulation retry.
    result = classify_lipsync(
        clip_duration=4,
        speech_end=2.5,
        expected_offset=0,
        observation=_observation(
            mouth_visible_ratio=0.5,
            closed_mouth_active_samples=6,
            distinct_mouth_shapes=1,
        ),
    )
    assert result["status"] == "fail_generation"


def test_reported_continuous_open_retry_is_generation_failure() -> None:
    # job_a8269ac536a9 sc2 attempt 2 had 0/15 closed samples. Variation among
    # open shapes must not make continuous-open oscillation pass.
    result = classify_lipsync(
        clip_duration=8,
        speech_end=5.169,
        expected_offset=0,
        observation=_observation(
            active_speech_samples=15,
            closed_mouth_active_samples=0,
            distinct_mouth_shapes=3,
            active_mouth_shapes=[
                "wide", "rounded", "wide", "teeth", "wide",
                "rounded", "wide", "wide", "teeth", "wide",
                "rounded", "wide", "teeth", "wide", "rounded",
            ],
            observed_mouth_onset_seconds=0,
            speech_onset_seconds=0,
        ),
    )
    assert result["status"] == "fail_generation"
    assert "never closes" in result["reason"]


def test_reported_two_shape_held_smile_is_generation_failure() -> None:
    # job_a8269ac536a9 sc4: already open before onset and mostly held one wide
    # shape. The old aggregate threshold accepted this.
    result = classify_lipsync(
        clip_duration=9,
        speech_end=4.598,
        expected_offset=0,
        observation=_observation(
            active_speech_samples=12,
            closed_mouth_active_samples=1,
            distinct_mouth_shapes=2,
            active_mouth_shapes=[
                "wide", "wide", "wide", "wide", "wide", "wide",
                "wide", "rounded", "wide", "wide", "wide", "closed",
            ],
            pre_speech_mouth_state="open",
            observed_mouth_onset_seconds=0,
            speech_onset_seconds=0.073,
        ),
    )
    assert result["status"] == "fail_generation"


def test_richer_good_job_mouth_sequence_passes() -> None:
    # job_7d112150c67f showed natural alternation among 4 shapes with closures.
    result = classify_lipsync(
        clip_duration=10,
        speech_end=9.357,
        expected_offset=0,
        observation=_observation(
            active_speech_samples=14,
            closed_mouth_active_samples=3,
            distinct_mouth_shapes=4,
            active_mouth_shapes=[
                "wide", "rounded", "closed", "teeth", "wide", "narrow", "closed",
                "rounded", "wide", "teeth", "closed", "narrow", "rounded", "wide",
            ],
            pre_speech_mouth_state="open",
            observed_mouth_onset_seconds=0,
            speech_onset_seconds=0.073,
        ),
    )
    assert result["status"] == "pass"
    assert result["mouth_shape_change_count"] == 13
    assert result["longest_static_shape_run"] == 1


def test_missing_evidence_and_analysis_failure_are_inconclusive() -> None:
    result = classify_lipsync(
        clip_duration=4,
        speech_end=None,
        expected_offset=0,
        observation=None,
    )
    assert result["status"] == "inconclusive"
    missing_sequence = classify_lipsync(
        clip_duration=4,
        speech_end=2.5,
        expected_offset=0,
        observation={
            key: value
            for key, value in _observation().items()
            if key != "active_mouth_shapes"
        },
    )
    assert missing_sequence["status"] == "inconclusive"
    tool_result = LipSyncQA().execute(
        {"video_path": "/missing/video.mp4", "audio_path": "/missing/voice.wav"}
    )
    assert not tool_result.success


def test_asset_manifest_serializes_two_attempts_and_caps_each_scene() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas/artifacts/asset_manifest.schema.json").read_text(encoding="utf-8")
    )
    manifest = {
        "version": "1.0",
        "assets": [
            {
                "id": "vid-scene-1-retry",
                "type": "video",
                "path": "assets/video/scene-1-retry.mp4",
                "source_tool": "higgsfield_mcp_video",
                "scene_id": "scene-1",
            }
        ],
        "metadata": {
            "lip_sync_qa": {
                "rubric_version": "2.0",
                "status": "warning",
                "reviewed_scene_count": 1,
                "failed_scene_count": 1,
                "retry_count": 1,
                "scenes": {
                    "scene-1": {
                        "eligible": True,
                        "status": "fail_generation",
                        "attempts": [
                            _attempt(1, "original", "fail_generation"),
                            _attempt(2, "retry", "fail_generation"),
                        ],
                        "retry_count": 1,
                        "selected_take": "retry",
                        "validated_audio_offset_seconds": None,
                        "retry_job_id": "hf-retry-1",
                        "retry_credits": 12,
                        "unresolved_warning": "Mouth remained flat after the bounded retry.",
                    }
                },
                "unresolved_warnings": ["scene-1: mouth remained flat after retry"],
            }
        },
    }
    jsonschema.validate(manifest, schema)

    legacy = json.loads(json.dumps(manifest))
    legacy["metadata"]["lip_sync_qa"].pop("rubric_version")
    for attempt in legacy["metadata"]["lip_sync_qa"]["scenes"]["scene-1"]["attempts"]:
        evidence = attempt["evidence"]
        for key in (
            "active_mouth_shapes",
            "pre_speech_mouth_state",
            "mouth_shape_change_count",
            "longest_static_shape_run",
            "longest_static_shape_fraction",
            "unclear_mouth_shape_samples",
        ):
            evidence.pop(key)
    jsonschema.validate(legacy, schema)

    manifest["metadata"]["lip_sync_qa"]["scenes"]["scene-1"]["retry_count"] = 2
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, schema)


def test_final_review_warning_is_presented_not_blocked() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas/artifacts/final_review.schema.json").read_text(encoding="utf-8")
    )
    review = {
        "version": "1.0",
        "output_path": "renders/final.mp4",
        "status": "warning",
        "checks": {
            "technical_probe": {},
            "visual_spotcheck": {},
            "audio_spotcheck": {},
            "promise_preservation": {},
            "subtitle_check": {},
            "lip_sync_check": {
                "status": "warning",
                "scenes_reviewed": ["scene-1"],
                "unresolved_scene_ids": ["scene-1"],
                "offsets_applied_seconds": {},
                "warnings": ["Mouth remained flat after attempt 2."],
                "recommended_action": "present_to_user",
            },
        },
        "recommended_action": "present_to_user",
    }
    jsonschema.validate(review, schema)


def test_runner_prompts_enforce_retry_cap_and_surface_scene_warning() -> None:
    runner = runner_module.ClaudeCodeRunner()
    prompts = [
        runner._stills_approved_prompt("job-test", {}),
        runner._motion_approved_prompt("job-test", {}),
        runner._assets_in_progress_prompt("job-test", {}),
    ]
    for prompt in prompts:
        normalized = prompt.lower()
        assert "lipsync_qa" in normalized
        assert "once" in normalized
        assert "attempt 3" in normalized
        assert "checkpoint" in normalized
        assert "active_mouth_shapes" in normalized
        assert "continuous-open" in normalized
        assert "rubric_version='2.0'" in prompt

    artifacts = {
        "asset_manifest": {
            "metadata": {
                "lip_sync_qa": {
                    "unresolved_warnings": ["scene-1 remained flat"],
                    "scenes": {
                        "scene-1": {
                            "unresolved_warning": "Mouth remained flat after attempt 2."
                        }
                    },
                }
            }
        }
    }
    for gate in ("approve_assets", "approve_final"):
        question = runner_module._question_for_gate(gate, artifacts=artifacts)
        assert "scene-1" in question
        assert "does not block delivery" in question


def test_pipeline_and_directors_require_eligible_only_bounded_qa() -> None:
    manifest = (ROOT / "pipeline_defs/panda-video.yaml").read_text(encoding="utf-8")
    assets = (
        ROOT / "skills/pipelines/panda-video/asset-director.md"
    ).read_text(encoding="utf-8")
    edit = (
        ROOT / "skills/pipelines/panda-video/edit-director.md"
    ).read_text(encoding="utf-8")
    compose = (
        ROOT / "skills/pipelines/panda-video/compose-director.md"
    ).read_text(encoding="utf-8")

    assert "lipsync_qa" in manifest
    assert "audio_lipsync:true" in assets
    assert "Narrator, HOLD" in assets
    assert "Never retry a `pass`" in assets
    assert "never submit attempt 3" in assets
    assert "`active_mouth_shapes`" in assets
    assert '`final_review.status:"warning"`' in compose
    assert "get_cost:true" in assets
    assert "validated_audio_offset_seconds" in edit
    assert "immutable scene-plan section/scene timestamps" in edit.replace("\n", " ")
    assert "effective_scene_start + original_scene_local_offset" in edit.replace("\n", " ")
    assert 'recommended_action:"present_to_user"' in compose
