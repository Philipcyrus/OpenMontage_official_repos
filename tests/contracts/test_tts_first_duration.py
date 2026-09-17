"""panda-video TTS-first + duration-driven i2v — director/yaml contract."""

from __future__ import annotations

from pathlib import Path

import yaml

from lib.i2v_duration import allocate_scene_durations
from schemas.artifacts import validate_artifact

ROOT = Path(__file__).resolve().parents[2]


def _panda_video() -> dict:
    return yaml.safe_load(
        (ROOT / "pipeline_defs" / "panda-video.yaml").read_text(encoding="utf-8")
    )


def test_assets_lists_audio_probe_and_tts_before_i2v_in_review_focus():
    stages = {s["name"]: s for s in _panda_video()["stages"]}
    assets = stages["assets"]
    tools = assets["tools_available"]
    assert "elevenlabs_tts" in tools
    assert "audio_probe" in tools
    assert "higgsfield_mcp_video" in tools
    # TTS must be listed; order in yaml is documentary — review_focus carries the rule.
    focus = " ".join(assets.get("review_focus") or [])
    assert "TTS" in focus or "tts" in focus.lower()
    assert "before" in focus.lower()
    assert "i2v" in focus.lower() or "motion" in focus.lower() or "clip" in focus.lower()


def test_asset_director_documents_tts_first_then_duration_then_i2v():
    text = (ROOT / "skills/pipelines/panda-video/asset-director.md").read_text(
        encoding="utf-8"
    )
    lower = text.lower()
    assert "tts-first" in lower or "tts first" in lower
    assert "elevenlabs_tts" in lower
    assert "audio_probe" in lower or "ffprobe" in lower
    assert "snap_i2v_duration" in text or "i2v_duration" in text or "i2v duration" in lower
    # Order: narration / TTS before motion clips in PHASE 3
    phase3 = text.split("### 5. PHASE 3")[1].split("### 6.")[0]
    tts_pos = phase3.lower().find("elevenlabs_tts")
    # Prefer the numbered motion step / generate_video — not the intro that mentions motion earlier
    motion_candidates = [
        phase3.lower().find("3. **motion clips**".lower()),
        phase3.lower().find("generate_video"),
        phase3.lower().find("higgsfield_mcp_video"),
    ]
    motion_pos = min(p for p in motion_candidates if p >= 0)
    assert tts_pos >= 0 and motion_pos >= 0
    assert tts_pos < motion_pos, "PHASE 3 must document TTS before Higgsfield i2v"


def test_motion_sample_tts_before_sample_when_narrated():
    text = (ROOT / "skills/pipelines/panda-video/asset-director.md").read_text(
        encoding="utf-8"
    )
    section = text.split("### 4. PHASE 2")[1].split("### 5. PHASE 3")[0]
    lower = section.lower()
    assert "tts" in lower or "elevenlabs" in lower
    assert "duration" in lower


def test_edit_director_prefers_audio_driven_effective_timeline():
    text = (ROOT / "skills/pipelines/panda-video/edit-director.md").read_text(
        encoding="utf-8"
    )
    lower = text.lower()
    assert "timeline_contract" in text
    assert "audio-driven" in lower
    assert "mute" in lower
    assert "hold" in lower


def test_panda_directors_require_audio_driven_target_band():
    asset = (ROOT / "skills/pipelines/panda-video/asset-director.md").read_text(
        encoding="utf-8"
    )
    edit = (ROOT / "skills/pipelines/panda-video/edit-director.md").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "skills/pipelines/panda-video/compose-director.md").read_text(
        encoding="utf-8"
    )
    combined = " ".join((asset, edit, compose))
    assert "allocate_scene_durations" in asset
    assert "timeline_contract" in combined
    assert "±5%" in combined
    assert "effective_scene_start + original_scene_local_offset" in edit
    assert "target_duration_s" in compose


def test_typed_timeline_contract_validates_in_asset_manifest():
    timeline = allocate_scene_durations(
        [
            {
                "scene_id": "sc1",
                "vo_seconds": 6.5,
                "audio_end_seconds": 6.5,
                "allowed_durations": list(range(5, 11)),
                "planned_duration_seconds": 9,
            },
            {
                "scene_id": "sc2",
                "vo_seconds": 4.0,
                "audio_end_seconds": 4.0,
                "allowed_durations": list(range(5, 11)),
                "planned_duration_seconds": 9,
            },
        ],
        18,
    )
    validate_artifact(
        "asset_manifest",
        {
            "version": "1.0",
            "assets": [
                {
                    "id": "sc1_video",
                    "type": "video",
                    "path": "assets/video/sc1.mp4",
                    "source_tool": "higgsfield_mcp",
                    "scene_id": "sc1",
                }
            ],
            "metadata": {"timeline_contract": timeline},
        },
    )
