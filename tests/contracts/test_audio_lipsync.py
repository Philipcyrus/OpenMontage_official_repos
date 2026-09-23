"""panda-video audio_lipsync — Seedance audio_references contract."""

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
    assert "audio_references" in focus
    assert "seedance" in focus


def test_asset_director_documents_audio_references_and_generate_audio_false():
    text = (ROOT / "skills/pipelines/panda-video/asset-director.md").read_text(
        encoding="utf-8"
    )
    assert "audio_references" in text
    assert "generate_audio:false" in text or "generate_audio: false" in text
    assert "seedance_2_0" in text
    assert "audio_lipsync" in text
    # Default-on lipsync path + HOLD fallback must both appear
    lower = text.lower()
    assert "fallback" in lower or "fall back" in lower
    assert "hold" in lower
    assert "timing-preserving scene-local vo bed" in lower
    assert "pauses" in lower
    assert "concat" not in lower


def test_higgsfield_bridge_documents_panda_audio_lipsync():
    text = (ROOT / "skills/meta/higgsfield-mcp-bridge.md").read_text(encoding="utf-8")
    lower = text.lower()
    assert "audio lip-sync" in lower or "audio_lipsync" in lower
    assert "audio_references" in text
    assert "generate_audio" in text
    assert "seedance" in lower
    assert "timing-preserving scene-local" in lower
    assert "_premix_voice_tracks" in text
    assert "relative_at_s" in text


def test_compose_and_edit_keep_elevenlabs_vo_bed_for_lipsync():
    edit = (ROOT / "skills/pipelines/panda-video/edit-director.md").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "skills/pipelines/panda-video/compose-director.md").read_text(
        encoding="utf-8"
    )
    assert "AUDIO LIPSYNC" in edit or "audio_lipsync" in edit
    assert "mute" in edit.lower()
    assert "relative_at_s" in edit
    assert "effective_scene_start + original_scene_local_offset" in edit
    assert "generate_audio:false" in compose or "generate_audio: false" in compose
    assert "elevenlabs" in compose.lower() or "voice" in compose.lower()


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


def test_kling_customer_lipsync_is_opt_in_customer_only_and_goes_through_the_ledger():
    text = (ROOT / "skills/pipelines/panda-video/asset-director.md").read_text(encoding="utf-8")
    section = text.split("#### Kling customer lip-sync", 1)[1].split("#### ", 1)[0]
    assert "only when the prompt has a KLING CUSTOMER LIP-SYNC line" in text
    assert "Kling does not support animal characters" in section
    assert "python -m lib.kling_lipsync run" in section
    assert "Never call `kling_lip_sync` or the Kling API directly" in section
    assert "`original_url`" in text
    tools = {s["name"]: s for s in _panda_video()["stages"]}["assets"]["tools_available"]
    assert "kling_lip_sync" not in tools        # the ledger-keeping lib is the only way in
