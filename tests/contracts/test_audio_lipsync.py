"""panda-video audio_lipsync — Seedance native speech, re-voiced into the cast voice."""

from __future__ import annotations

from pathlib import Path

import yaml

from lib.i2v_duration import effective_audio_start

ROOT = Path(__file__).resolve().parents[2]


def _panda_video() -> dict:
    return yaml.safe_load(
        (ROOT / "pipeline_defs" / "panda-video.yaml").read_text(encoding="utf-8")
    )


def test_assets_review_focus_mentions_audio_lipsync_seedance():
    stages = {s["name"]: s for s in _panda_video()["stages"]}
    focus = " ".join(stages["assets"].get("review_focus") or []).lower()
    assert "audio_lipsync" in focus or "audio lipsync" in focus
    assert "generate_audio:true" in focus
    assert "elevenlabs_voice_changer" in focus
    assert "seedance" in focus
    assert "audio_references = that one line" not in focus


def test_asset_director_documents_native_speech_and_revoicing():
    text = (ROOT / "skills/pipelines/panda-video/asset-director.md").read_text(
        encoding="utf-8"
    )
    assert "**No `audio_references`**" in text
    assert "**`generate_audio:true`**" in text
    assert "elevenlabs_voice_changer" in text
    assert "spoken_form" in text and "pronunciation_guides" in text
    assert "transcript_match" in text
    assert "in English" in text and "then stops talking" in text
    assert "generated_i2v_duration" in text
    assert "seedance_2_0" in text
    assert "audio_lipsync" in text
    # Default-on lipsync path + HOLD fallback must both appear
    lower = text.lower()
    assert "fallback" in lower or "fall back" in lower
    assert "hold" in lower
    assert "speaking subshot" in lower
    assert "build_scene_subshots" in text
    assert "subshot_content_seconds" in text
    assert "timing-preserving scene-local vo bed" not in lower
    assert "concat" not in lower


def test_higgsfield_bridge_documents_panda_audio_lipsync():
    text = (ROOT / "skills/meta/higgsfield-mcp-bridge.md").read_text(encoding="utf-8")
    lower = text.lower()
    assert "audio lip-sync" in lower or "audio_lipsync" in lower
    assert "**No `audio_references`**" in text
    assert "**`generate_audio: true`**" in text
    assert "elevenlabs_voice_changer" in text
    assert "seedance" in lower
    assert "speaking subshot" in lower
    assert "build_scene_subshots" in text
    assert "never a premixed bed" in lower
    assert "timing-preserving scene-local" not in lower
    assert "relative_at_s" not in text


def test_compose_and_edit_lay_each_vo_at_its_subshot():
    edit = (ROOT / "skills/pipelines/panda-video/edit-director.md").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "skills/pipelines/panda-video/compose-director.md").read_text(
        encoding="utf-8"
    )
    assert "AUDIO LIPSYNC" in edit or "audio_lipsync" in edit
    assert "mute" in edit.lower()
    assert "place_scene_subshots" in edit
    assert "one cut per returned subshot" in edit.replace("\n", " ")
    assert "Overlapping windows mix" not in edit
    assert "generate_audio:true" in compose
    assert "elevenlabs_voice_changer" in compose
    assert "never reach the master" in compose.replace("\n", " ")
    assert "elevenlabs_voice_changer" in edit
    assert "one `panda_render` scene per edit cut" in compose.replace("\n", " ")
    assert "source_in_s" in compose
    assert "allow_voice_overlap" in compose


def test_runner_prompts_never_ask_for_a_mixed_scene_bed():
    from dify_launcher import runner as runner_module

    line = runner_module._audio_lipsync_line({})
    assert "SPEAKING SUBSHOT" in line
    assert "ONLY that ONE line" in line
    assert "never attach a VO file" in line
    assert "generate_audio:true" in line
    assert "elevenlabs_voice_changer" in line
    assert "in English" in line and "then stops talking" in line
    assert "adelay + amix" not in line
    assert "in Mandarin Chinese" in runner_module._audio_lipsync_line({"language": "zh"})
    qa = runner_module._lip_sync_qa_line({})
    assert "that speaker's own VO file" in qa
    assert "transcript_match" in qa
    assert "scene-local VO bed" not in qa


def test_effective_audio_start_moves_picture_and_vo_together_idempotently():
    kwargs = {
        "effective_scene_start_seconds": 20.0,
        "original_section_start_seconds": 18.5,
        "original_scene_start_seconds": 18.0,
        "validated_lip_sync_delta_seconds": 0.25,
    }
    first = effective_audio_start(**kwargs)
    resumed = effective_audio_start(**kwargs)
    assert first == 20.75
    assert resumed == first


def test_effective_audio_start_without_qa_delta_preserves_local_offset():
    assert (
        effective_audio_start(
            effective_scene_start_seconds=24.0,
            original_section_start_seconds=27.4,
            original_scene_start_seconds=27.0,
        )
        == 24.4
    )


def test_effective_audio_start_refuses_to_stack_a_line_on_the_previous_one():
    import pytest

    with pytest.raises(ValueError, match="overlap"):
        effective_audio_start(
            effective_scene_start_seconds=10.0,
            original_section_start_seconds=13.0,
            original_scene_start_seconds=10.0,
            previous_audio_end_seconds=4.2,
        )
    assert effective_audio_start(
        effective_scene_start_seconds=10.0,
        original_section_start_seconds=13.0,
        original_scene_start_seconds=10.0,
        previous_audio_end_seconds=2.9,
    ) == 13.0


def test_asset_manifest_accepts_per_subshot_lipsync_reviews():
    import json

    import pytest

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas/artifacts/asset_manifest.schema.json").read_text(encoding="utf-8")
    )
    review = {
        "eligible": True,
        "subshot_id": "sc11-s22",
        "speaker": "panda",
        "audio_path": "assets/audio/vo-s22-panda.mp3",
        "status": "pass",
        "attempts": [],
        "retry_count": 0,
        "selected_take": "original",
        "unresolved_warning": None,
    }
    manifest = {
        "version": "1.0",
        "assets": [],
        "metadata": {
            "lip_sync_qa": {
                "status": "pass",
                "reviewed_scene_count": 1,
                "failed_scene_count": 0,
                "retry_count": 0,
                "unresolved_warnings": [],
                "scenes": {},
                "subshots": {
                    "sc11": {
                        "s22": review,
                        "s23": {**review, "subshot_id": "sc11-s23", "speaker": "customer",
                                "audio_path": "assets/audio/vo-s23-customer.mp3"},
                    }
                },
            }
        },
    }
    jsonschema.validate(instance=manifest, schema=schema)
    manifest["metadata"]["lip_sync_qa"]["subshots"]["sc11"]["s22"]["speaker"] = "robot"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=manifest, schema=schema)
    manifest["metadata"]["lip_sync_qa"]["subshots"]["sc11"]["s22"]["speaker"] = "panda"
    manifest["metadata"]["lip_sync_qa"]["subshots"]["sc11"]["s22"]["retry_count"] = 2
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=manifest, schema=schema)


def test_edit_decisions_accepts_subshot_cuts():
    import json

    import pytest

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas/artifacts/edit_decisions.schema.json").read_text(encoding="utf-8")
    )
    required = schema.get("required", [])
    cut_schema = schema["properties"]["cuts"]["items"]
    cut = {
        "id": "cut-sc11-s22",
        "source": "sc11-s22",
        "in_seconds": 0.0,
        "out_seconds": 3.3,
        "scene_id": "sc11",
        "subshot_id": "sc11-s22",
        "subshot_kind": "speaking",
        "section_id": "s22",
        "speaker": "panda",
        "audio_lipsync": True,
        "audio_end_seconds": 3.05,
    }
    missing = [k for k in cut_schema.get("required", []) if k not in cut]
    assert not missing, f"test cut lacks required keys {missing}"
    jsonschema.validate(instance=cut, schema=cut_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={**cut, "subshot_kind": "mixed_bed"}, schema=cut_schema)
    assert "cuts" in required


def test_asset_manifest_rejects_illegal_per_row_lipsync_fields():
    """Asset rows are additionalProperties:false — audio_lipsync/speaker/duration are illegal."""
    import json

    import pytest

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas/artifacts/asset_manifest.schema.json").read_text(encoding="utf-8")
    )
    legal = {
        "version": "1.0",
        "assets": [
            {
                "id": "sc2-clip",
                "type": "video",
                "path": "assets/video/sc2.mp4",
                "source_tool": "higgsfield_mcp",
                "scene_id": "sc2",
                "model": "seedance_2_0",
                "duration_seconds": 8,
                "generation_summary": "[audio_lipsync:true] seedance_2_0 with audio_references",
            },
            {
                "id": "vo-s2-panda",
                "type": "narration",
                "path": "assets/audio/vo-s2-panda.mp3",
                "source_tool": "elevenlabs_tts",
                "scene_id": "sc2",
                "duration_seconds": 5.3,
                "voice_performance": {"source_section_id": "s2"},
                "generation_summary": "speaker=panda section s2",
            },
        ],
        "metadata": {
            "lip_sync_qa": {
                "status": "pass",
                "reviewed_scene_count": 1,
                "failed_scene_count": 0,
                "retry_count": 0,
                "unresolved_warnings": [],
                "scenes": {
                    "sc2": {
                        "eligible": True,
                        "status": "pass",
                        "attempts": [],
                        "retry_count": 0,
                        "selected_take": "original",
                        "unresolved_warning": None,
                    }
                },
            }
        },
    }
    jsonschema.validate(instance=legal, schema=schema)

    illegal = {
        "version": "1.0",
        "assets": [
            {
                "id": "sc2-clip",
                "type": "video",
                "path": "assets/video/sc2.mp4",
                "source_tool": "higgsfield_mcp",
                "scene_id": "sc2",
                "audio_lipsync": True,
                "duration": 8,
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=illegal, schema=schema)


def test_asset_director_avoids_illegal_per_row_manifest_fields():
    text = (ROOT / "skills/pipelines/panda-video/asset-director.md").read_text(
        encoding="utf-8"
    )
    assert "additionalProperties: false" in text
    assert "never add `audio_lipsync`" in text or "must not invent an `audio_lipsync`" in text
    assert "voice_performance.source_section_id" in text
    assert "Manifest: `audio_lipsync: true`" not in text
    assert "clip metadata `audio_lipsync: true`" not in text
    assert "also record `speaker` and the script `section` id in metadata" not in text
