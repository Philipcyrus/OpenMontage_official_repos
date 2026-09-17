"""Async launcher recovery invariants."""

from __future__ import annotations

from dify_launcher import app as launcher


def _running_state() -> dict:
    return {
        "job_id": "job_recovery",
        "pipeline": "panda-video",
        "status": "running",
        "stage": "edit",
        "gate": None,
        "question": "stage edit completed; next: compose",
        "artifacts": {"clips": ["clip.mp4"]},
        "_recovery_gate": "approve_assets",
        "_recovery_stage": "assets",
        "_processing_operation": "resume:approve_assets",
        "processing_started_at": "2026-09-16T20:00:00+00:00",
    }


def test_worker_returned_running_recovers_originating_gate() -> None:
    recovered = launcher._recover_worker_result(
        _running_state(),
        {"gate": "approve_assets", "stage": "assets"},
        "worker returned status=running",
    )
    assert recovered["status"] == "awaiting_human"
    assert recovered["gate"] == "approve_assets"
    assert recovered["artifacts"]["clips"] == ["clip.mp4"]
    assert "approve to resume" in recovered["question"].lower()
    assert "processing_finished_at" in recovered
    assert "_recovery_gate" not in recovered


def test_checkpoint_exception_preserves_hero_retry_gate_and_media() -> None:
    original = {
        "job_id": "job_hero",
        "pipeline": "panda-video",
        "status": "running",
        "stage": "assets",
        "gate": None,
        "_recovery_gate": "approve_hero_still",
        "_recovery_stage": "assets",
        "artifacts": {"stills": ["sc4_hero.png"], "hero_scene_id": "sc4"},
    }
    recovered = launcher._recover_worker_result(
        original,
        original,
        "checkpoint validation failed",
    )

    assert recovered["status"] == "awaiting_human"
    assert recovered["gate"] == "approve_hero_still"
    assert recovered["artifacts"]["stills"] == ["sc4_hero.png"]
    assert recovered["artifacts"]["hero_scene_id"] == "sc4"


def test_pre_guardrail_panda_edit_orphan_infers_assets_gate() -> None:
    legacy = _running_state()
    legacy.pop("_recovery_gate")
    legacy.pop("_recovery_stage")
    recovered = launcher._recover_worker_result(
        legacy, legacy, "legacy persisted running state has no active worker"
    )
    assert recovered["status"] == "awaiting_human"
    assert recovered["gate"] == "approve_assets"


def test_get_recovers_persisted_running_job_without_worker(monkeypatch) -> None:
    saved: list[dict] = []
    state = _running_state()
    monkeypatch.setattr(launcher, "_TOKEN", "")
    monkeypatch.setattr(launcher.store, "load_state", lambda job_id: dict(state))
    monkeypatch.setattr(
        launcher.store, "save_state", lambda value: saved.append(dict(value))
    )
    with launcher._LOCK:
        launcher._RUNNING.discard(state["job_id"])

    public = launcher.get_job(state["job_id"])

    assert public["status"] == "awaiting_human"
    assert public["gate"] == "approve_assets"
    assert public["worker_active"] is False
    assert saved and saved[-1]["status"] == "awaiting_human"


def test_background_worker_cannot_persist_running(monkeypatch) -> None:
    saved: list[dict] = []
    original = {
        "job_id": "job_bg",
        "pipeline": "panda-video",
        "status": "awaiting_human",
        "stage": "assets",
        "gate": "approve_assets",
        "artifacts": {"clips": ["clip.mp4"]},
    }
    persisted = {
        **original,
        "status": "running",
        "gate": None,
        "_recovery_gate": "approve_assets",
        "processing_started_at": "2026-09-16T20:00:00+00:00",
    }
    monkeypatch.setattr(launcher.store, "load_state", lambda job_id: dict(persisted))
    monkeypatch.setattr(
        launcher.store, "save_state", lambda value: saved.append(dict(value))
    )
    with launcher._LOCK:
        launcher._RUNNING.add(original["job_id"])

    launcher._bg(
        original["job_id"],
        lambda state: {
            **state,
            "status": "running",
            "gate": None,
            "question": "stage edit completed; next: compose",
        },
        original,
    )

    assert saved[-1]["status"] == "awaiting_human"
    assert saved[-1]["gate"] == "approve_assets"
    assert original["job_id"] not in launcher._RUNNING


def test_health_identifies_loaded_launcher_code() -> None:
    body = launcher.health()
    assert body["process_started_at"]
    assert body["build_revision"]
    assert len(body["launcher_code_fingerprint"]) == 16
