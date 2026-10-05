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
    label_sample_phase,
    speech_intervals_from_silence,
)


ROOT = Path(__file__).resolve().parents[2]


def _observation(**overrides):
    value = {
        "mouth_visible_ratio": 1.0,
        "active_speech_samples": 6,
        "closed_mouth_active_samples": 1,
        "distinct_mouth_shapes": 3,
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


def test_job_cfb6_late_shot_failures_now_fail_generation() -> None:
    # job_cfb6fd099504: these passed the old onset-only rubric but looked wrong in the master.
    panda_quits_mid_line = classify_lipsync(
        clip_duration=4, speech_end=2.58, expected_offset=0,
        observation=_observation(
            observed_mouth_onset_seconds=0.5,
            tail_active_samples=4,
            tail_closed_mouth_samples=3,
        ),
    )
    assert panda_quits_mid_line["status"] == "fail_generation"
    assert "before the line ends" in panda_quits_mid_line["reason"]

    grinning_listener = classify_lipsync(
        clip_duration=4, speech_end=1.88, expected_offset=0,
        observation=_observation(
            observed_mouth_onset_seconds=0.5,
            listener_visible_samples=8,
            listener_open_mouth_samples=8,
        ),
    )
    assert grinning_listener["status"] == "fail_generation"
    assert "listening character" in grinning_listener["reason"]

    one_open_frame_listener = classify_lipsync(
        clip_duration=4, speech_end=2.5, expected_offset=0,
        observation=_observation(
            listener_visible_samples=8,
            listener_open_mouth_samples=1,
            tail_active_samples=4,
            tail_closed_mouth_samples=1,
        ),
    )
    assert one_open_frame_listener["status"] == "pass"


def test_onset_tolerance_is_point_two_seconds() -> None:
    late = classify_lipsync(
        clip_duration=4, speech_end=2.5, expected_offset=0,
        observation=_observation(observed_mouth_onset_seconds=0.8),
    )
    assert late["status"] == "fail_timing"
    assert late["recommended_audio_offset_seconds"] == pytest.approx(0.3)


def test_sampling_reaches_the_last_word_and_labels_phases() -> None:
    intervals = [(0.0, 0.55), (0.68, 2.19)]
    samples = build_sample_timestamps(intervals, expected_offset=0, clip_duration=4.0)
    assert 2.09 in samples
    assert max(samples) == pytest.approx(2.34)
    phases = {t: label_sample_phase(t, intervals, expected_offset=0) for t in samples}
    assert phases[2.09] == "tail"
    assert phases[2.34] == "post"
    assert label_sample_phase(0.6, intervals, expected_offset=0) == "pause"
    assert label_sample_phase(0.1, intervals, expected_offset=0) == "onset"
    assert label_sample_phase(0.9, intervals, expected_offset=0) == "active"
    assert label_sample_phase(0.2, intervals, expected_offset=0.5) == "pre"


def test_missing_evidence_and_analysis_failure_are_inconclusive() -> None:
    result = classify_lipsync(
        clip_duration=4,
        speech_end=None,
        expected_offset=0,
        observation=None,
    )
    assert result["status"] == "inconclusive"
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
        "status": "pass",
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


def test_unresolved_subshot_warning_names_the_line_and_speaker() -> None:
    artifacts = {
        "asset_manifest": {
            "metadata": {
                "lip_sync_qa": {
                    "unresolved_warnings": ["sc11/s23 customer remained flat"],
                    "scenes": {},
                    "subshots": {
                        "sc11": {
                            "s22": {"speaker": "panda", "unresolved_warning": None},
                            "s23": {
                                "speaker": "customer",
                                "unresolved_warning": "Mouth remained flat after attempt 2.",
                            },
                        }
                    },
                }
            }
        }
    }
    question = runner_module._question_for_gate("approve_assets", artifacts=artifacts)
    assert "sc11/s23 (customer)" in question
    assert "s22" not in question


def test_lipsync_qa_skips_narrator_and_reports_line_identity() -> None:
    result = LipSyncQA().execute({
        "video_path": "/missing/video.mp4",
        "audio_path": "assets/audio/vo-s21-narrator.mp3",
        "scene_id": "sc11",
        "section_id": "s21",
        "speaker": "narrator",
    })
    assert result.success
    assert result.data["status"] == "skipped"
    assert result.data["section_id"] == "s21"
    props = LipSyncQA.input_schema["properties"]
    assert {"section_id", "subshot_id", "speaker"} <= set(props)
    assert "never a scene mix" in props["audio_path"]["description"].lower()


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

    flat_assets = " ".join(assets.split())
    flat_edit = " ".join(edit.split())
    assert "lipsync_qa" in manifest
    assert "lip_sync_qa.subshots.<scene_id>.<section_id>" in manifest
    assert "audio_lipsync:true" in assets
    assert "Narrator fills, HOLD" in flat_assets
    assert "that speaker's own VO file" in flat_assets
    assert "metadata.lip_sync_qa.subshots.<scene_id>.<section_id>" in flat_assets
    assert "never shift a sibling line" in flat_assets
    assert "exact scene-local VO bed" not in flat_assets
    assert "validated_offsets[section_id]" in flat_edit
    assert "Never shift a sibling line" in flat_edit
    assert "Never retry a `pass`" in assets
    assert "speaking prompt overrides the scene plan's `movement`" in flat_assets
    assert "listener_open_mouth_samples" in flat_assets
    scene_plan = (
        ROOT / "skills/pipelines/panda-video/scene-plan-director.md"
    ).read_text(encoding="utf-8")
    assert "Dialogue shots stay still" in scene_plan
    runner_prompt = runner_module._audio_lipsync_line({})
    assert "OVERRIDES the scene plan's movement" in runner_prompt
    assert "listener's lips stay closed" in runner_prompt
    assert "never submit attempt 3" in assets
    assert "get_cost:true" in assets
    assert "validated_audio_offset_seconds" in edit
    assert "immutable scene-plan section/scene timestamps" in edit.replace("\n", " ")
    assert "effective_scene_start + original_scene_local_offset" in edit.replace("\n", " ")
    assert 'recommended_action:"present_to_user"' in compose
    assert "active_mouth_shapes" not in assets
    assert "rubric 2.0" not in manifest
    assert '`final_review.status:"warning"`' not in compose
