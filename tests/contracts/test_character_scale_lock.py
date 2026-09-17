"""Panda/customer relative scale and posture lock contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_pair_scale_lock_is_canonical_brand_config() -> None:
    config = json.loads(
        (ROOT / "config/panda-elements.json").read_text(encoding="utf-8")
    )
    lock = config["character_references"]["pair_scale_lock"]
    assert lock["human_height_units"] == 1.0
    assert lock["panda_height_ratio"] == 0.58
    assert lock["ratio_tolerance"] == 0.05
    assert "same ground line" in lock["ground_plane"]
    assert "upright" in lock["human_posture"].lower()
    assert "upright" in lock["panda_posture"].lower()


def test_pair_scale_lock_reaches_plan_generation_motion_and_qa() -> None:
    scene_plan = (
        ROOT / "skills/pipelines/panda-video/scene-plan-director.md"
    ).read_text(encoding="utf-8")
    assets = (
        ROOT / "skills/pipelines/panda-video/asset-director.md"
    ).read_text(encoding="utf-8")
    bridge = (ROOT / "skills/meta/higgsfield-mcp-bridge.md").read_text(
        encoding="utf-8"
    )
    manifest = (ROOT / "pipeline_defs/panda-video.yaml").read_text(
        encoding="utf-8"
    )

    assert "panda ear-top height = 0.58" in scene_plan
    assert "description" in scene_plan and "required_assets" in scene_plan
    assert "PAIR SCALE LOCK" in assets
    assert "beginning/middle/end frames" in assets
    assert "metadata.character_scale_qa" in assets
    assert "PAIR SCALE + POSTURE LOCK" in bridge
    assert "0.53–0.63" in bridge
    assert "start_image" in bridge
    assert "0.58 ±0.05" in manifest


def _manifest_with_scale_qa(ratio: float) -> dict:
    return {
        "version": "1.0",
        "assets": [
            {
                "id": "sc2",
                "type": "video",
                "path": "assets/video/sc2.mp4",
                "source_tool": "higgsfield_mcp",
                "scene_id": "sc2",
            }
        ],
        "metadata": {
            "character_scale_qa": {
                "status": "pass",
                "human_height_units": 1.0,
                "panda_height_ratio": ratio,
                "ratio_tolerance": 0.05,
                "shared_ground_plane_required": True,
                "scenes": {
                    "sc2": {
                        "contains_pair": True,
                        "still_status": "pass",
                        "clip_status": "pass",
                        "estimated_panda_height_ratio": 0.58,
                        "shared_ground_plane": True,
                        "posture_consistent": True,
                        "frame_paths": [
                            "assets/video/scale_qa/sc2-start.jpg",
                            "assets/video/scale_qa/sc2-mid.jpg",
                            "assets/video/scale_qa/sc2-end.jpg",
                        ],
                        "notes": "Pair remains on one ground line at the approved scale.",
                    }
                },
            }
        },
    }


def test_character_scale_qa_schema_accepts_only_locked_ratio() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas/artifacts/asset_manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(instance=_manifest_with_scale_qa(0.58), schema=schema)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=_manifest_with_scale_qa(0.7), schema=schema)
