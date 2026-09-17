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
