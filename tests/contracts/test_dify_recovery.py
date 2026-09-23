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
    state["updated_at"] = "2026-09-16T20:00:01+00:00"
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


def test_get_recovery_does_not_overwrite_newer_approve_final(monkeypatch) -> None:
    """Poll loads running; worker finishes with approve_final before save — keep final."""
    saved: list[dict] = []
    orphan = _running_state()
    orphan["updated_at"] = "2026-09-16T20:00:01+00:00"
    finished = {
        "job_id": orphan["job_id"],
        "pipeline": "panda-video",
        "status": "awaiting_human",
        "stage": "compose",
        "gate": "approve_final",
        "question": "Approve the finished (unbranded) video.",
        "artifacts": {"clips": ["clip.mp4"], "final": "final.mp4"},
        "updated_at": "2026-09-16T20:01:00+00:00",
        "processing_finished_at": "2026-09-16T20:01:00+00:00",
    }
    loads = [dict(orphan), dict(finished)]

    def _load(_job_id: str) -> dict:
        return loads.pop(0) if loads else dict(finished)

    monkeypatch.setattr(launcher, "_TOKEN", "")
    monkeypatch.setattr(launcher.store, "load_state", _load)
    monkeypatch.setattr(
        launcher.store, "save_state", lambda value: saved.append(dict(value))
    )
    with launcher._LOCK:
        launcher._RUNNING.discard(orphan["job_id"])

    public = launcher.get_job(orphan["job_id"])

    assert public["status"] == "awaiting_human"
    assert public["gate"] == "approve_final"
    assert not saved  # must not persist stale approve_assets recovery


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
    assert "code_stale" in body
    assert body["code_stale"] is False
    assert body["launcher_code_fingerprint_on_disk"] == body["launcher_code_fingerprint"]


def test_next_recovery_target_maps_long_hops() -> None:
    base = {"pipeline": "panda-video", "options": {}, "stage": "assets"}
    assert launcher._next_recovery_target(
        {**base, "gate": "approve_scene_plan", "stage": "scene_plan"}, "approve"
    ) == ("approve_hero_still", "assets")
    assert launcher._next_recovery_target(
        {**base, "gate": "approve_hero_still"}, "approve"
    ) == ("approve_stills", "assets")
    assert launcher._next_recovery_target(
        {**base, "gate": "approve_stills"}, "approve"
    ) == ("approve_assets", "assets")
    assert launcher._next_recovery_target(
        {**base, "gate": "approve_stills", "options": {"motion_sample": True}}, "approve"
    ) == ("approve_motion_sample", "assets")
    assert launcher._next_recovery_target(
        {**base, "gate": "approve_assets"}, "approve"
    ) == ("approve_final", "compose")
    assert launcher._next_recovery_target(
        {**base, "gate": "approve_stills"}, "revise"
    ) == ("approve_stills", "assets")


def test_recover_prefers_on_disk_hero_over_scene_plan(monkeypatch, tmp_path) -> None:
    from lib import checkpoint as cp

    job_id = "job_disk_hero"
    monkeypatch.setenv("OPENMONTAGE_PROJECTS_DIR", str(tmp_path))
    # Reload path constant used by disk recovery.
    import lib.paths as paths
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)

    hero = {
        "version": "1.0",
        "project_id": job_id,
        "pipeline_type": "panda-video",
        "stage": "assets",
        "status": "awaiting_human",
        "timestamp": "2026-09-18T00:00:00+00:00",
        "artifacts": {"stills": ["sc1_hero.png"]},
        "partial_progress": {"phase": "hero_still", "hero_scene_id": "sc1"},
        "human_approval_required": True,
        "human_approved": False,
        "checkpoint_policy": "guided",
    }
    (tmp_path / job_id).mkdir()
    (tmp_path / job_id / "checkpoint_assets.json").write_text(
        __import__("json").dumps(hero), encoding="utf-8"
    )

    original = {
        "job_id": job_id,
        "pipeline": "panda-video",
        "status": "running",
        "stage": "scene_plan",
        "gate": None,
        "_recovery_gate": "approve_scene_plan",
        "_recovery_stage": "scene_plan",
        "artifacts": {},
    }
    recovered = launcher._recover_worker_result(
        original, original, "checkpoint validation failed"
    )
    assert recovered["gate"] == "approve_hero_still"
    assert recovered["stage"] == "assets"


def test_recover_prefers_compose_awaiting_as_final(monkeypatch, tmp_path) -> None:
    import lib.paths as paths

    job_id = "job_disk_final"
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    compose = {
        "version": "1.0",
        "project_id": job_id,
        "pipeline_type": "panda-video",
        "stage": "compose",
        "status": "awaiting_human",
        "timestamp": "2026-09-18T00:00:00+00:00",
        "artifacts": {},
        "partial_progress": {"phase": "preview"},
        "human_approval_required": True,
        "human_approved": False,
        "checkpoint_policy": "guided",
    }
    (tmp_path / job_id).mkdir()
    (tmp_path / job_id / "checkpoint_compose.json").write_text(
        __import__("json").dumps(compose), encoding="utf-8"
    )
    original = {
        "job_id": job_id,
        "pipeline": "panda-video",
        "status": "running",
        "stage": "edit",
        "gate": None,
        "_recovery_gate": "approve_assets",
        "_recovery_stage": "assets",
        "artifacts": {"clips": ["c.mp4"]},
    }
    recovered = launcher._recover_worker_result(original, original, "error: boom")
    assert recovered["gate"] == "approve_final"
    assert recovered["stage"] == "compose"


def test_running_ack_question_is_gate_specific() -> None:
    q = launcher._running_ack_question("approve_stills", "approve")
    assert "clips" in q.lower()
    assert "poll" in q.lower()
    q2 = launcher._running_ack_question("approve_assets", "approve")
    assert "compos" in q2.lower() or "final" in q2.lower()


def test_async_respond_returns_nonempty_question(monkeypatch) -> None:
    saved: list[dict] = []
    state = {
        "job_id": "job_ack",
        "pipeline": "panda-video",
        "status": "awaiting_human",
        "stage": "assets",
        "gate": "approve_stills",
        "question": "Approve the stills",
        "artifacts": {"stills": ["a.png"]},
        "options": {},
    }
    monkeypatch.setattr(launcher, "_TOKEN", "")
    monkeypatch.setattr(launcher, "_ASYNC", True)
    monkeypatch.setattr(launcher.store, "load_state", lambda job_id: dict(state))
    monkeypatch.setattr(
        launcher.store, "save_state", lambda value: saved.append(dict(value))
    )
    monkeypatch.setattr(
        launcher, "_spawn",
        lambda job_id, fn, st, arg=None, processing_state=None: (
            saved.append(dict(processing_state)) if processing_state else None
        ),
    )
    with launcher._LOCK:
        launcher._RUNNING.discard(state["job_id"])

    body = launcher.Respond(decision="approve")
    public = launcher.respond(state["job_id"], body)
    assert public["status"] == "running"
    assert public["worker_active"] is True or public["question"]
    assert public["question"]
    assert "poll" in public["question"].lower()
    assert "clips" in public["question"].lower()
    assert saved[-1]["_recovery_gate"] == "approve_assets"


def test_mutated_worker_state_recovers_via_stamped_recovery_gate() -> None:
    """resume clears gate in place; stamped _recovery_gate on the worker copy must still win."""
    original = {
        "job_id": "job_mut",
        "pipeline": "panda-video",
        "status": "awaiting_human",
        "stage": "scene_plan",
        "gate": None,  # mutated away by resume
        "_recovery_gate": "approve_hero_still",
        "_recovery_stage": "assets",
        "artifacts": {},
        "options": {},
    }
    result = {
        "job_id": "job_mut",
        "pipeline": "panda-video",
        "status": "running",
        "stage": "assets",
        "gate": None,
        "question": "stage scene_plan completed; next: assets",
        "artifacts": {},
        "options": {},
        # markers wiped by in-place mutation / sync
    }
    recovered = launcher._recover_worker_result(
        result, original, "worker returned status=running"
    )
    assert recovered["status"] == "awaiting_human"
    assert recovered["gate"] == "approve_hero_still"
    assert recovered["stage"] == "assets"
    assert "without a resumable gate" not in (recovered.get("question") or "").lower()


def test_recovery_original_merges_persisted_markers() -> None:
    snap = {
        "job_id": "job_merge",
        "gate": None,
        "stage": "scene_plan",
        "_recovery_gate": None,
        "_recovery_stage": None,
    }
    persisted = {
        "job_id": "job_merge",
        "status": "running",
        "gate": None,
        "_recovery_gate": "approve_hero_still",
        "_recovery_stage": "assets",
    }
    merged = launcher._recovery_original("job_merge", snap, persisted)
    assert merged["_recovery_gate"] == "approve_hero_still"
    assert merged["_recovery_stage"] == "assets"


def test_infer_assets_start_when_scene_plan_done_no_assets(monkeypatch, tmp_path) -> None:
    """scene_plan completed + missing assets → reopen approve_hero_still, never gate-less fail."""
    import json
    import lib.paths as paths

    job_id = "job_infer_assets"
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    (tmp_path / job_id).mkdir()
    scene_plan = {
        "version": "1.0",
        "project_id": job_id,
        "pipeline_type": "panda-video",
        "stage": "scene_plan",
        "status": "completed",
        "timestamp": "2026-09-18T00:00:00+00:00",
        "artifacts": {},
        "human_approval_required": True,
        "human_approved": True,
        "checkpoint_policy": "guided",
    }
    (tmp_path / job_id / "checkpoint_scene_plan.json").write_text(
        json.dumps(scene_plan), encoding="utf-8"
    )

    original = {
        "job_id": job_id,
        "pipeline": "panda-video",
        "status": "running",
        "stage": "assets",
        "gate": None,
        "artifacts": {},
        "options": {},
        # no _recovery_gate — simulates wipe after mutate
    }
    recovered = launcher._recover_worker_result(
        original, original, "worker returned status=running"
    )
    assert recovered["status"] == "awaiting_human"
    assert recovered["gate"] == "approve_hero_still"
    assert recovered["stage"] == "assets"
    assert "without a resumable gate" not in (recovered.get("question") or "").lower()


def test_infer_assets_start_skips_when_assets_in_progress(monkeypatch, tmp_path) -> None:
    import json
    import lib.paths as paths

    job_id = "job_assets_ip"
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    (tmp_path / job_id).mkdir()
    for name, status in (
        ("checkpoint_scene_plan.json", "completed"),
        ("checkpoint_assets.json", "in_progress"),
    ):
        stage = "scene_plan" if "scene_plan" in name else "assets"
        (tmp_path / job_id / name).write_text(
            json.dumps({
                "version": "1.0",
                "project_id": job_id,
                "pipeline_type": "panda-video",
                "stage": stage,
                "status": status,
                "timestamp": "2026-09-18T00:00:00+00:00",
                "artifacts": {},
                "human_approval_required": True,
                "human_approved": False,
                "checkpoint_policy": "guided",
            }),
            encoding="utf-8",
        )
    original = {
        "job_id": job_id,
        "pipeline": "panda-video",
        "status": "running",
        "stage": "assets",
        "gate": None,
        "artifacts": {},
        "options": {},
    }
    recovered = launcher._recover_worker_result(
        original, original, "worker returned status=running"
    )
    # Mid-render assets: keep running — do not invent approve_hero_still or fail.
    assert recovered["status"] == "running"
    assert recovered["gate"] is None
    assert recovered["stage"] == "assets"
    assert "in progress" in (recovered.get("question") or "").lower()


def test_stamped_recovery_does_not_reopen_gate_while_assets_in_progress(
    monkeypatch, tmp_path
) -> None:
    """_run_until_assets_gate may return running/gate=null while Higgsfield still renders.

    Stamped _recovery_gate must not reopen approve_assets over that intentional mid-render.
    """
    import json
    import lib.paths as paths

    job_id = "job_mid_render_stamp"
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    (tmp_path / job_id).mkdir()
    (tmp_path / job_id / "checkpoint_assets.json").write_text(
        json.dumps({
            "version": "1.0",
            "project_id": job_id,
            "pipeline_type": "panda-video",
            "stage": "assets",
            "status": "in_progress",
            "timestamp": "2026-09-18T00:00:00+00:00",
            "artifacts": {"stills": ["a.png"]},
            "human_approval_required": True,
            "human_approved": False,
            "checkpoint_policy": "guided",
        }),
        encoding="utf-8",
    )
    result = {
        "job_id": job_id,
        "pipeline": "panda-video",
        "status": "running",
        "stage": "assets",
        "gate": None,
        "question": (
            "assets generation still in progress after 8 continue attempts — "
            "Higgsfield motion jobs may still be rendering; poll GET /jobs/{id}"
        ),
        "artifacts": {"stills": ["a.png"], "clips": []},
        "options": {},
    }
    original = {
        **result,
        "_recovery_gate": "approve_assets",
        "_recovery_stage": "assets",
    }
    recovered = launcher._recover_worker_result(
        result, original, "worker returned status=running"
    )
    assert recovered["status"] == "running"
    assert recovered["gate"] is None
    assert recovered["stage"] == "assets"
    assert recovered["gate"] != "approve_assets"
    assert "in progress" in (recovered.get("question") or "").lower()


def test_async_respond_stamps_recovery_on_worker_state(monkeypatch) -> None:
    spawned: list[dict] = []
    state = {
        "job_id": "job_stamp",
        "pipeline": "panda-video",
        "status": "awaiting_human",
        "stage": "scene_plan",
        "gate": "approve_scene_plan",
        "question": "Approve the scene plan",
        "artifacts": {"scene_plan_md": "x"},
        "options": {},
    }
    monkeypatch.setattr(launcher, "_TOKEN", "")
    monkeypatch.setattr(launcher, "_ASYNC", True)
    monkeypatch.setattr(launcher.store, "load_state", lambda job_id: dict(state))
    monkeypatch.setattr(launcher.store, "save_state", lambda value: None)
    monkeypatch.setattr(
        launcher, "_spawn",
        lambda job_id, fn, st, arg=None, processing_state=None: spawned.append(
            {"worker": dict(st), "processing": dict(processing_state or {})}
        ),
    )
    with launcher._LOCK:
        launcher._RUNNING.discard(state["job_id"])

    launcher.respond(state["job_id"], launcher.Respond(decision="approve"))
    assert spawned
    worker = spawned[0]["worker"]
    processing = spawned[0]["processing"]
    assert worker["gate"] == "approve_scene_plan"  # pre-resume copy keeps leaving gate
    assert worker["_recovery_gate"] == "approve_hero_still"
    assert worker["_recovery_stage"] == "assets"
    assert processing["_recovery_gate"] == "approve_hero_still"
    assert processing["gate"] is None


def test_create_job_refuses_when_code_stale(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_TOKEN", "")
    monkeypatch.setattr(launcher, "_LAUNCHER_CODE_FINGERPRINT", "stale_fingerprint")
    monkeypatch.setattr(
        launcher, "_launcher_code_fingerprint", lambda: "fresh_on_disk_hash"
    )
    import fastapi
    try:
        launcher.create_job(launcher.StartJob(brief="x"))
        raise AssertionError("expected 503")
    except fastapi.HTTPException as exc:
        assert exc.status_code == 503
        assert "code_stale" in exc.detail


def test_create_job_refuses_status_check_brief(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_TOKEN", "")
    monkeypatch.setattr(
        launcher, "_launcher_code_fingerprint",
        lambda: launcher._LAUNCHER_CODE_FINGERPRINT,
    )
    import fastapi
    try:
        launcher.create_job(launcher.StartJob(brief="Checking on the job again."))
        raise AssertionError("expected 400")
    except fastapi.HTTPException as exc:
        assert exc.status_code == 400
        assert "status check" in exc.detail.lower()


def test_is_status_check_brief() -> None:
    assert launcher._is_status_check_brief("Checking on the job again.")
    assert launcher._is_status_check_brief("checking on the job")
    assert launcher._is_status_check_brief("any update?")
    assert launcher._is_status_check_brief("Extending the wait automatically")
    assert launcher._is_status_check_brief("Done. That run stopped before it finished")
    assert launcher._is_status_check_brief("send the brief again to retry")
    assert not launcher._is_status_check_brief(
        "30-second vertical video about OnePool for families"
    )
    assert not launcher._is_status_check_brief(
        "Still waiting for summer vibes in a 15-second airport spot"
    )


def test_health_reports_code_stale(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_LAUNCHER_CODE_FINGERPRINT", "old_fp_value_16")
    monkeypatch.setattr(
        launcher, "_launcher_code_fingerprint", lambda: "new_fp_value_xxxx"
    )
    body = launcher.health()
    assert body["code_stale"] is True
    assert body["launcher_code_fingerprint"] == "old_fp_value_16"
    assert body["launcher_code_fingerprint_on_disk"] == "new_fp_value_xxxx"
