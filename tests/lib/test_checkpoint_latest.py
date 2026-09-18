"""get_latest_checkpoint ranking + soft-load for phase-gated thin pauses."""

from __future__ import annotations

import json
import time
from pathlib import Path

from lib.checkpoint import get_latest_checkpoint, read_checkpoint, validate_checkpoint


def _cp(
    *,
    project_id: str,
    stage: str,
    status: str,
    artifacts: dict | None = None,
    partial_progress: dict | None = None,
) -> dict:
    body = {
        "version": "1.0",
        "project_id": project_id,
        "pipeline_type": "panda-video",
        "stage": stage,
        "status": status,
        "timestamp": "2026-09-18T00:00:00+00:00",
        "artifacts": artifacts if artifacts is not None else {},
        "human_approval_required": True,
        "human_approved": status == "completed",
        "checkpoint_policy": "guided",
    }
    if partial_progress is not None:
        body["partial_progress"] = partial_progress
    return body


def _write(project_dir: Path, stage: str, body: dict) -> Path:
    path = project_dir / f"checkpoint_{stage}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_thin_hero_pause_validates_with_phase_exemption() -> None:
    hero = _cp(
        project_id="p",
        stage="assets",
        status="awaiting_human",
        artifacts={},
        partial_progress={"phase": "hero_still", "hero_scene_id": "sc1"},
    )
    validate_checkpoint(hero)  # must not raise


def test_phase_less_assets_without_manifest_fails() -> None:
    bare = _cp(project_id="p", stage="assets", status="awaiting_human", artifacts={})
    try:
        validate_checkpoint(bare)
        raise AssertionError("expected CheckpointValidationError")
    except Exception as exc:
        assert "asset_manifest" in str(exc)


def test_get_latest_prefers_awaiting_assets_over_newer_completed_scene_plan(
    tmp_path: Path,
) -> None:
    project_id = "job_rank"
    proj = tmp_path / project_id
    proj.mkdir()

    scene_plan = {
        "version": "1.0",
        "style_playbook": "panda",
        "scenes": [
            {
                "id": "scene-1",
                "type": "generated",
                "description": "Panda waves",
                "start_seconds": 0,
                "end_seconds": 3,
            }
        ],
    }
    _write(
        proj,
        "scene_plan",
        _cp(
            project_id=project_id,
            stage="scene_plan",
            status="completed",
            artifacts={"scene_plan": scene_plan},
        ),
    )
    _write(
        proj,
        "assets",
        _cp(
            project_id=project_id,
            stage="assets",
            status="awaiting_human",
            artifacts={},
            partial_progress={"phase": "stills"},
        ),
    )
    # Make scene_plan newer on disk (the historical mtime footgun).
    time.sleep(0.05)
    sp = proj / "checkpoint_scene_plan.json"
    sp.write_text(sp.read_text(encoding="utf-8"), encoding="utf-8")

    latest = get_latest_checkpoint(tmp_path, project_id)
    assert latest is not None
    assert latest["stage"] == "assets"
    assert latest["status"] == "awaiting_human"
    assert latest["partial_progress"]["phase"] == "stills"


def test_get_latest_soft_loads_thin_hero(tmp_path: Path) -> None:
    project_id = "job_soft"
    proj = tmp_path / project_id
    proj.mkdir()
    _write(
        proj,
        "assets",
        _cp(
            project_id=project_id,
            stage="assets",
            status="awaiting_human",
            artifacts={},
            partial_progress={"phase": "hero_still"},
        ),
    )
    latest = get_latest_checkpoint(tmp_path, project_id)
    assert latest is not None
    assert latest["partial_progress"]["phase"] == "hero_still"
    soft = read_checkpoint(tmp_path, project_id, "assets", soft=True)
    assert soft is not None
