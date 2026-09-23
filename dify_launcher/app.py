"""Dify launcher — the HTTP service Dify talks to.

This is the ONLY networked surface. The OpenMontage engine is not a service; this launcher
starts/resumes agent runs and surfaces the approval gates so Dify can show them to the user.

Endpoints:
  GET  /health
  POST /jobs                      {brief, pipeline?, profile?, options?}  -> start a run
  GET  /jobs/{id}                                                  -> current state + gate + artifacts
  POST /jobs/{id}/respond         {decision: approve|revise|skip|cancel, ...}  -> resume to next gate
  POST /jobs/{id}/brand           {profile: bgc}                   -> stamp BGC wordmark on done stills / video master
  GET  /jobs/{id}/artifacts/{name}                                 -> download a still/script/final.mp4

Sync vs async:
  Real agent legs (claude runner) take MINUTES, which would hang an HTTP client. So when the
  runner is `claude` the launcher runs ASYNC: POST /jobs and /respond return IMMEDIATELY with
  status="running", the agent runs in a background thread, and the caller POLLS GET /jobs/{id}
  until status is "awaiting_human" | "done" | "failed". The mock runner stays SYNC (fast) so
  local tests keep their one-shot behavior. Override with env DIFY_ASYNC=1|0.

  Single uvicorn worker assumed (in-process job registry). Storage is local (see store.py).
  Auth: optional shared token via env DIFY_TOKEN (X-Dify-Token).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from dify_launcher import runner as _runner
from dify_launcher import store
from dify_launcher.storyboard_preview import is_superseded_still

app = FastAPI(title="Panda AI — Dify Launcher", version="0.2.0")

# --- montage-svc "raw render" door (additive; see dify_launcher/montage_routes.py) ------
# A SECOND, independent entrance to the SAME vendored render core, mounted under /montage.
# It does NOT touch the agent pipeline below — both call the same stateless render funcs.
# Wired defensively: any import/mount problem here must never take down the launcher.
if os.environ.get("MONTAGE_DOOR", "1").lower() not in ("0", "false", "no", ""):
    try:
        from fastapi.staticfiles import StaticFiles

        from dify_launcher import montage_routes as _montage
        _montage.init()                               # ensure render data dir + profiles exist
        app.include_router(_montage.router)           # /montage/compose, /overlay, /mix-audio, ...
        app.add_exception_handler(_montage.StorageError, _montage.storage_error_handler)
        _montage.MONTAGE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        app.mount("/montage/files",                   # serve rendered outputs
                  StaticFiles(directory=str(_montage.MONTAGE_DATA_DIR)), name="montage-files")
        _MONTAGE_DOOR = True
    except Exception as _e:  # noqa: BLE001 — the raw door is optional; the launcher isn't
        import logging
        logging.getLogger("dify_launcher").warning("montage door disabled: %s", _e)
        _MONTAGE_DOOR = False
else:
    _MONTAGE_DOOR = False

_RUNNER_NAME = os.environ.get("DIFY_RUNNER", "mock")
_RUNNER = _runner.get_runner(_RUNNER_NAME)
_TOKEN = os.environ.get("DIFY_TOKEN", "")
# Async by default for the (slow) claude runner; sync for mock so tests stay one-shot.
_ASYNC = os.environ.get("DIFY_ASYNC", "1" if _RUNNER_NAME == "claude" else "0").lower() \
    not in ("0", "false", "no", "")

_RUNNING: set[str] = set()
_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loaded_revision() -> str:
    configured = os.environ.get("OPENMONTAGE_BUILD_REVISION", "").strip()
    if configured:
        return configured
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _launcher_code_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(_runner.__file__ or "")):
        try:
            digest.update(path.resolve().read_bytes())
        except OSError:
            digest.update(f"unreadable:{path}".encode())
    return digest.hexdigest()[:16]


_PROCESS_STARTED_AT = _utc_now()
_LOADED_REVISION = _loaded_revision()
_LAUNCHER_CODE_FINGERPRINT = _launcher_code_fingerprint()


def _job_is_active(job_id: str) -> bool:
    with _LOCK:
        return job_id in _RUNNING


def _clear_processing_markers(state: dict[str, Any]) -> dict[str, Any]:
    state.pop("_recovery_gate", None)
    state.pop("_recovery_stage", None)
    state.pop("_processing_operation", None)
    if state.get("processing_started_at"):
        state["processing_finished_at"] = _utc_now()
    return state


_ASSETS_PHASE_GATES = {
    "hero_still": "approve_hero_still",
    "stills": "approve_stills",
    "motion_sample": "approve_motion_sample",
    "budget_hold": "budget_exceeded",
}


def _truthy_option(options: dict[str, Any], key: str, default: Any = False) -> bool:
    v = options.get(key, default)
    return str(v).lower() not in ("false", "0", "no", "off", "")


def _next_recovery_target(
    state: dict[str, Any], decision: str
) -> tuple[Optional[str], Optional[str]]:
    """(gate, stage) to reopen if an async leg dies after leaving the current gate.

    Approve advances recovery to the *next* expected human pause. Revise/skip/cancel
    keep the current gate so paid media is not skipped.
    """
    gate = state.get("gate")
    stage = state.get("stage")
    if decision != "approve":
        return (gate if isinstance(gate, str) else None,
                stage if isinstance(stage, str) else None)

    pipeline = state.get("pipeline") or "panda-video"
    options = state.get("options") if isinstance(state.get("options"), dict) else {}
    hero_on = _truthy_option(options, "hero_still", True)
    motion_on = _truthy_option(options, "motion_sample", False)
    stills_terminal = pipeline in ("panda-carousel", "panda-image")

    if gate == "approve_script":
        return "approve_scene_plan", "scene_plan"
    if gate == "approve_scene_plan":
        if pipeline == "panda-image" or not hero_on:
            return "approve_stills", "assets"
        return "approve_hero_still", "assets"
    if gate == "approve_hero_still":
        return "approve_stills", "assets"
    if gate == "approve_stills":
        if stills_terminal:
            return "approve_brand", "brand"
        if motion_on:
            return "approve_motion_sample", "assets"
        return "approve_assets", "assets"
    if gate == "approve_motion_sample":
        return "approve_assets", "assets"
    if gate == "approve_assets":
        return "approve_final", "compose"
    if gate == "approve_final":
        return "approve_brand", "brand"
    if gate == "budget_exceeded":
        return "budget_exceeded", "assets"
    return (gate if isinstance(gate, str) else None,
            stage if isinstance(stage, str) else None)


def _running_ack_question(gate: Optional[str], decision: str) -> str:
    """Non-empty question so Agent Door never treats a long async hop as 'no reply'."""
    if decision == "revise":
        return "processing — revising; poll GET /jobs/{id} until status changes"
    if decision == "skip":
        return "processing — finishing without brand; poll GET /jobs/{id}"
    if decision == "cancel":
        return "processing — cancelling; poll GET /jobs/{id}"
    by_gate = {
        "approve_script": "processing — writing scene plan; poll GET /jobs/{id}",
        "approve_scene_plan": (
            "processing — generating look-lock / storyboard stills; poll GET /jobs/{id}"
        ),
        "approve_hero_still": (
            "processing — generating storyboard stills; poll GET /jobs/{id}"
        ),
        "approve_stills": "processing — generating clips; poll GET /jobs/{id}",
        "approve_motion_sample": (
            "processing — generating remaining clips; poll GET /jobs/{id}"
        ),
        "approve_assets": (
            "processing — editing and composing final; poll GET /jobs/{id}"
        ),
        "approve_final": "processing — opening brand gate; poll GET /jobs/{id}",
        "approve_brand": "processing — applying brand; poll GET /jobs/{id}",
        "budget_exceeded": "processing — resuming under budget; poll GET /jobs/{id}",
    }
    return by_gate.get(
        gate or "",
        "processing — poll GET /jobs/{id} until status changes",
    )


def _raw_checkpoint_status(job_id: str, stage: str) -> Optional[str]:
    """Peek checkpoint status from disk without soft-load validation.

    Soft-load can drop completed/thin checkpoints that lack a canonical artifact; recovery
    only needs the status field to decide mid-render vs invent-start-gate.
    """
    try:
        from lib.paths import PROJECTS_DIR
    except ImportError:
        return None
    path = PROJECTS_DIR / job_id / f"checkpoint_{stage}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    return status if isinstance(status, str) else None


def _mid_render_stage(job_id: str) -> Optional[str]:
    """Stage name when assets/compose is legitimately mid-render (do not invent a gate)."""
    for stage in ("assets", "compose"):
        if _raw_checkpoint_status(job_id, stage) == "in_progress":
            return stage
    return None


def _disk_recovery_pause(job_id: str) -> Optional[tuple[str, str]]:
    """Prefer an on-disk later human pause over an earlier text gate / stale recovery.

    Returns (gate, stage) when checkpoint_assets/compose is awaiting_human.
    """
    try:
        from lib import checkpoint as cp
        from lib.paths import PROJECTS_DIR
    except ImportError:
        return None

    compose = cp.read_checkpoint(PROJECTS_DIR, job_id, "compose", soft=True)
    if isinstance(compose, dict) and compose.get("status") == "awaiting_human":
        return "approve_final", "compose"

    assets = cp.read_checkpoint(PROJECTS_DIR, job_id, "assets", soft=True)
    if not isinstance(assets, dict) or assets.get("status") != "awaiting_human":
        return None

    partial = assets.get("partial_progress")
    phase = partial.get("phase") if isinstance(partial, dict) else None
    if phase in _ASSETS_PHASE_GATES:
        return _ASSETS_PHASE_GATES[phase], "assets"

    arts = assets.get("artifacts") if isinstance(assets.get("artifacts"), dict) else {}
    # Phase-less full-media pause: require manifest; clips optional but preferred.
    if arts.get("asset_manifest"):
        return "approve_assets", "assets"
    return None


def _infer_assets_start_gate(
    job_id: str, state: dict[str, Any]
) -> Optional[tuple[str, str]]:
    """When scene_plan is done but assets never started, reopen the first assets pause.

    Covers the production failure where the assets agent no-ops (no checkpoint) and the
    worker exits as gate-less ``running``.
    """
    pipeline = state.get("pipeline") or "panda-video"
    if pipeline not in ("panda-video", "panda-carousel", "panda-image"):
        return None

    if _raw_checkpoint_status(job_id, "scene_plan") != "completed":
        return None

    assets_status = _raw_checkpoint_status(job_id, "assets")
    if assets_status in {"awaiting_human", "in_progress", "completed"}:
        # Assets already started or finished — do not invent a start gate.
        return None

    options = state.get("options") if isinstance(state.get("options"), dict) else {}
    if pipeline == "panda-image" or not _truthy_option(options, "hero_still", True):
        return "approve_stills", "assets"
    return "approve_hero_still", "assets"


def _recovery_original(
    job_id: str, state: dict[str, Any], persisted: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Build a recover context that survives in-place mutation of the worker state dict.

    ``resume`` clears ``gate`` on the same object passed to ``_bg``. Recovery markers live
    on the processing snapshot and/or were stamped onto the worker copy at spawn time.
    """
    snap = dict(persisted or {})
    original = dict(state)
    for key in ("_recovery_gate", "_recovery_stage"):
        if not original.get(key) and snap.get(key):
            original[key] = snap[key]
    # Prefer the pre-mutation leaving gate only as last resort; next-target markers win.
    if not original.get("gate") and snap.get("gate"):
        # processing_state intentionally has gate=None while running — ignore that.
        pass
    return original


def _recover_worker_result(
    result: dict[str, Any],
    original: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Never let an exiting async worker strand a job in `running` — except mid-render.

    ``_run_until_assets_gate`` intentionally returns ``running`` / ``gate=None`` while
    assets (or compose) is ``in_progress``. Do not reopen a stamped human gate over that.
    """
    recovered = dict(result)
    job_id = recovered.get("job_id") or original.get("job_id")
    if job_id:
        mid = _mid_render_stage(str(job_id))
        if mid:
            q = recovered.get("question") or (
                f"{mid} generation in progress — poll GET /jobs/{{id}} until "
                "status is awaiting_human"
            )
            recovered.update(status="running", stage=mid, gate=None, question=q)
            return _clear_processing_markers(recovered)

    disk_pause = _disk_recovery_pause(str(job_id)) if job_id else None

    gate = (
        (disk_pause[0] if disk_pause else None)
        or recovered.get("gate")
        or recovered.get("_recovery_gate")
        or original.get("_recovery_gate")
        or original.get("gate")
    )
    # Backward-compatible repair for jobs written before recovery metadata existed.
    # In Panda, a running edit/compose state with generated clips can only originate
    # from the approved-assets gate; retrying there preserves all paid media.
    if (
        not gate
        and recovered.get("pipeline") == "panda-video"
        and recovered.get("stage") in {"edit", "compose"}
        and recovered.get("artifacts", {}).get("clips")
    ):
        gate = "approve_assets"
    if not gate and job_id:
        inferred = _infer_assets_start_gate(str(job_id), {**original, **recovered})
        if inferred:
            gate, inferred_stage = inferred
            recovered.setdefault("stage", inferred_stage)
    stage = (
        (disk_pause[1] if disk_pause else None)
        or recovered.get("stage")
        or recovered.get("_recovery_stage")
        or original.get("_recovery_stage")
        or original.get("stage")
    )
    if gate:
        recovered.update(
            status="awaiting_human",
            stage=stage,
            gate=gate,
            question=(
                f"Background processing stopped before the next gate ({reason}). "
                "Existing checkpoints and generated media are kept; approve to resume "
                "from the latest checkpoint."
            ),
        )
    else:
        recovered.update(
            status="failed",
            stage=stage,
            gate=None,
            question=(
                f"Background processing stopped without a resumable gate ({reason}). "
                "The job is no longer running; inspect its checkpoints before retrying."
            ),
        )
    return _clear_processing_markers(recovered)


def _auth(tok: Optional[str]) -> None:
    if _TOKEN and tok != _TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing X-Dify-Token")


def _public(state: dict[str, Any]) -> dict[str, Any]:
    """The view Dify gets: enough to render the gate + links to artifacts."""
    job_id = state["job_id"]
    arts = state.get("artifacts", {})
    links: dict[str, Any] = {}
    for key, val in arts.items():
        if isinstance(val, str) and val.endswith((".md", ".html", ".png", ".jpg", ".mp4")):
            links[key] = f"/jobs/{job_id}/artifacts/{val}"
        elif isinstance(val, list):
            items = val
            if key == "stills":
                items = [v for v in val if not is_superseded_still(v)]
            links[key] = [f"/jobs/{job_id}/artifacts/{v}" for v in items]
        else:
            links[key] = val
    view = {
        "job_id": job_id,
        "pipeline": state.get("pipeline"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "gate": state.get("gate"),
        "question": state.get("question"),
        "worker_active": _job_is_active(job_id),
        "processing_started_at": state.get("processing_started_at"),
        "processing_finished_at": state.get("processing_finished_at"),
        "updated_at": state.get("updated_at"),
        "artifacts": links,
    }
    if state.get("inputs"):
        view["inputs"] = state["inputs"]   # user screenshots, numbered as the user attached them
    return view


def _bg(job_id: str, fn: Callable[..., dict[str, Any]], state: dict[str, Any],
        arg: Optional[dict[str, Any]] = None) -> None:
    """Run one agent leg; an exiting worker must persist a terminal or human-gated state."""
    # Snapshot recovery identity before resume mutates ``state`` in place (clears gate).
    recovery_snapshot = {
        "gate": state.get("gate"),
        "stage": state.get("stage"),
        "_recovery_gate": state.get("_recovery_gate"),
        "_recovery_stage": state.get("_recovery_stage"),
        "pipeline": state.get("pipeline"),
        "options": state.get("options"),
        "job_id": state.get("job_id") or job_id,
    }
    try:
        result = fn(state) if arg is None else fn(state, arg)
        persisted = store.load_state(job_id) or {}
        if persisted.get("processing_started_at") and not result.get("processing_started_at"):
            result["processing_started_at"] = persisted["processing_started_at"]
        original = _recovery_original(job_id, recovery_snapshot, persisted)
        if result.get("status") == "running":
            result = _recover_worker_result(
                result, original, "worker returned status=running")
        else:
            result = _clear_processing_markers(result)
        store.save_state(result)
    except Exception as e:  # noqa: BLE001 — surface any leg failure to the poller
        persisted = store.load_state(job_id) or {}
        st = persisted or state
        original = _recovery_original(job_id, recovery_snapshot, persisted)
        st = _recover_worker_result(st, original, f"error: {e}")
        store.save_state(st)
    finally:
        with _LOCK:
            _RUNNING.discard(job_id)


def _spawn(job_id: str, fn: Callable[..., dict[str, Any]], state: dict[str, Any],
           arg: Optional[dict[str, Any]] = None,
           processing_state: Optional[dict[str, Any]] = None) -> None:
    with _LOCK:
        _RUNNING.add(job_id)
    if processing_state is not None:
        store.save_state(processing_state)
    threading.Thread(target=_bg, args=(job_id, fn, state, arg), daemon=True).start()


class StartJob(BaseModel):
    brief: str
    pipeline: Optional[str] = None     # panda-video (default) | panda-carousel | panda-image
    profile: Optional[str] = "ugc"
    options: dict[str, Any] = {}


class Respond(BaseModel):
    decision: str = "approve"          # "approve" | "revise" | "skip" (brand gate) | "cancel" (budget)
    answer: Optional[str] = None
    stills: list[str] = []             # optional user-supplied storyboard stills (paths)
    shots: list[int] = []              # optional 1-based indices: stills (GATE 3) or clips (GATE 4)
    mode: Optional[Literal["fresh", "edit"]] = None  # stills revise only; omit to infer
    max_higgsfield_credits: Optional[int] = None   # at the budget gate: raise the approved cap


class BrandBody(BaseModel):
    profile: str = "bgc"               # only bgc is implemented (wordmark on stills + video master)


def _resolve_pipeline(name: Optional[str]) -> str:
    p = (name or os.environ.get("PANDA_PIPELINE_TYPE") or "panda-video").strip()
    try:
        from lib.pipeline_loader import list_pipelines
        known = list_pipelines()
    except Exception:  # noqa: BLE001 — launcher must still start if the loader isn't importable
        known = ["panda-video", "panda-carousel", "panda-image"]
    if p not in known:
        raise HTTPException(status_code=400,
                            detail=f"unknown pipeline {p!r}; known: {sorted(known)}")
    return p


@app.get("/health")
def health() -> dict[str, Any]:
    current_fp = _launcher_code_fingerprint()
    code_stale = current_fp != _LAUNCHER_CODE_FINGERPRINT
    return {"status": "ok", "runner": _RUNNER_NAME, "async": _ASYNC,
            "montage_door": _MONTAGE_DOOR,
            "process_started_at": _PROCESS_STARTED_AT,
            "build_revision": _LOADED_REVISION,
            "launcher_code_fingerprint": _LAUNCHER_CODE_FINGERPRINT,
            "launcher_code_fingerprint_on_disk": current_fp,
            "code_stale": code_stale}


def _prepare_media(options: dict[str, Any], pipeline: str) -> Optional[Any]:
    """User screenshots (options.media): check, download and normalise BEFORE the job exists.

    Any problem is a 400 and no job is created. Returns None when the job has no media.
    """
    if not (options or {}).get("media"):
        return None
    from dify_launcher import screens

    from lib.screen_layout import PIPELINES

    if pipeline not in PIPELINES:
        raise HTTPException(status_code=400,
                            detail="images (options.media) are supported for "
                                   f"{', '.join(PIPELINES)} jobs only")
    from tools.video.screen_overlay import remotion_ready

    ok, why = remotion_ready()
    if not ok:
        raise HTTPException(status_code=400, detail=f"images need Remotion on this server: {why}")
    try:
        return screens.prepare(options)
    except screens.IntakeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _projects_dir() -> Any:
    pd = getattr(_RUNNER, "_projects_dir", None)
    if pd is None:
        from lib.paths import PROJECTS_DIR
        pd = PROJECTS_DIR
    return pd


_STATUS_CHECK_BRIEF_RE = re.compile(
    r"^\s*(checking on the job( again)?|check(ing)? (the )?status|"
    r"any update(\?)?|what('?s| is) the status|poll(ing)?( the)? job|"
    r"extend(ing)? the wait( automatically)?|"
    r"still (waiting|running)|"
    r"done\.?\s*that run stopped( before it finished)?|"
    r"send the brief again( to retry)?|"
    r"anything above was produced before it stopped)\s*[.!]?\s*$",
    re.I,
)


def _is_status_check_brief(brief: str) -> bool:
    """True when Dify/Mochi misfires a poll message into POST /jobs as a new brief."""
    text = (brief or "").strip()
    if not text or len(text) > 120:
        return False
    return bool(_STATUS_CHECK_BRIEF_RE.match(text))


@app.post("/jobs")
def create_job(body: StartJob, x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    _auth(x_dify_token)
    if _launcher_code_fingerprint() != _LAUNCHER_CODE_FINGERPRINT:
        raise HTTPException(
            status_code=503,
            detail=(
                "launcher code changed on disk since process start (code_stale); "
                "restart uvicorn before starting new jobs"
            ),
        )
    if _is_status_check_brief(body.brief):
        raise HTTPException(
            status_code=400,
            detail=(
                "brief looks like a status check, not a production brief — "
                "poll GET /jobs/{id} on the existing job instead of POST /jobs"
            ),
        )
    options = dict(body.options or {})
    prepared = None
    if options.get("media"):
        pipeline = _resolve_pipeline(body.pipeline)
        prepared = _prepare_media(options, pipeline)
    job_id = store.new_job_id()
    store.ensure_job(job_id)          # create the job + artifacts dir before the runner writes
    pipeline = _resolve_pipeline(body.pipeline)
    inputs: list[dict[str, Any]] = []
    if prepared is not None:
        from dify_launcher import screens

        options.pop("media", None)    # the signed links expire; the files now live in the job
        # The language the JOB will run in, not the raw option: the runner switches a Mandarin
        # brief sent with a stale language:en to zh, and the agent writes the slides in that
        # language. job.json drives the placed screenshot cards, so it has to match — and the
        # default is the prompts' default ("en"), not zh.
        # Module function (not a Runner method) — calling via _runner AttributeError'd → HTTP 500
        # after a successful media download, which Dify surfaces as "couldn't reach the render service".
        from dify_launcher.runner import _coerce_language_from_brief

        effective, _coerced = _coerce_language_from_brief(options, body.brief or "")
        try:
            records = screens.commit(
                prepared, _projects_dir() / job_id, pipeline=pipeline,
                language=str(effective.get("language") or "en").strip().lower())
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"could not store the images: {e}") from e
        inputs = [{"n": r["n"], "name": r["name"]} for r in records]
    state = {
        "job_id": job_id, "brief": body.brief, "pipeline": pipeline,
        "profile": body.profile, "options": options, "status": "running",
        "stage": None, "gate": None, "artifacts": {},
    }
    if inputs:
        state["inputs"] = inputs
    if _ASYNC:
        state["_processing_operation"] = "start"
        state["processing_started_at"] = _utc_now()
        state["question"] = "starting — poll GET /jobs/{id} until status is awaiting_human"
        # Reserve the in-process worker before publishing `running`, avoiding a GET race
        # that could otherwise mistake a not-yet-started job for an orphan.
        _spawn(job_id, _RUNNER.start, state, processing_state=state)
        return _public(state)
    state = _RUNNER.start(state)
    store.save_state(state)
    return _public(state)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    _auth(x_dify_token)
    state = store.load_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="job not found")
    if state.get("status") == "running" and not _job_is_active(job_id):
        # Capture freshness markers from the orphan snapshot before recovery.
        # A concurrent worker may finish and persist a newer gate between this
        # load and save_state; never overwrite that newer result.
        loaded_updated_at = state.get("updated_at")
        loaded_started_at = state.get("processing_started_at")
        candidate = _recover_worker_result(
            state, state, "persisted running state has no active worker")
        fresh = store.load_state(job_id) or {}
        still_orphaned = (
            fresh.get("status") == "running"
            and not _job_is_active(job_id)
            and fresh.get("updated_at") == loaded_updated_at
            and fresh.get("processing_started_at") == loaded_started_at
        )
        if still_orphaned:
            store.save_state(candidate)
            state = candidate
        else:
            state = fresh or candidate
    return _public(state)


@app.post("/jobs/{job_id}/respond")
def respond(job_id: str, body: Respond, x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    _auth(x_dify_token)
    state = store.load_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="job not found")
    with _LOCK:
        busy = job_id in _RUNNING
    if busy:
        raise HTTPException(status_code=409, detail="job is still processing; poll GET /jobs/{id}")
    if state.get("status") != "awaiting_human":
        raise HTTPException(status_code=409, detail=f"job is {state.get('status')}, not awaiting_human")
    if body.decision == "skip" and state.get("gate") != "approve_brand":
        raise HTTPException(status_code=400, detail="skip is only valid at the approve_brand gate")
    # Dify/OUI often POSTs decision=revise with answer="approved…" at stills; coerce
    # before recovery targets / ack / spawn so the job advances to animation (i2v).
    response = _runner.coerce_gate_response(state.get("gate"), body.model_dump())
    decision = str(response.get("decision") or body.decision)
    if response.get("_coerced_from_revise_approval"):
        print(
            f"[respond] {job_id} coerced revise→approve at gate={state.get('gate')!r} "
            f"note={response.get('_original_revise_answer')!r}",
            flush=True,
        )
    if _ASYNC:
        recovery_gate, recovery_stage = _next_recovery_target(state, decision)
        running = {
            **state,
            "status": "running",
            "gate": None,
            "question": _running_ack_question(state.get("gate"), decision),
            "_recovery_gate": recovery_gate,
            "_recovery_stage": recovery_stage,
            "_processing_operation": f"resume:{state.get('gate') or 'unknown'}",
            "processing_started_at": _utc_now(),
        }
        # Shallow copy + stamp recovery so resume in-place mutation cannot wipe markers
        # used by _recover_worker_result when the agent exits as gate-less running.
        worker_state = {
            **state,
            "_recovery_gate": recovery_gate,
            "_recovery_stage": recovery_stage,
        }
        _spawn(
            job_id,
            _RUNNER.resume,
            worker_state,
            response,
            processing_state=running,
        )
        return _public(running)
    try:
        state = _RUNNER.resume(state, response)
    except _runner.BrandError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e
    store.save_state(state)
    return _public(state)


@app.post("/jobs/{job_id}/brand")
def brand_job(job_id: str, body: BrandBody = BrandBody(),
              x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    """Stamp the BGC wordmark onto approved stills and/or the video master. Job must be `done`."""
    _auth(x_dify_token)
    state = store.load_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="job not found")
    with _LOCK:
        busy = job_id in _RUNNING
    if busy:
        raise HTTPException(status_code=409, detail="job is still processing; poll GET /jobs/{id}")
    try:
        state = _runner.brand_job(state, body.profile)
    except _runner.BrandError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e
    store.save_state(state)
    return _public(state)


@app.get("/jobs/{job_id}/artifacts/{name}")
def get_artifact(job_id: str, name: str, x_dify_token: Optional[str] = Header(None)):
    _auth(x_dify_token)
    try:
        p = store.artifact_path(job_id, name)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad artifact name")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(p))


@app.get("/jobs/{job_id}/cost")
def get_cost(job_id: str, x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    """Per-project cost & time summary (Higgsfield credits, ElevenLabs usage, generation
    time) in native units. The full human-readable table is the `cost_report.md` artifact."""
    _auth(x_dify_token)
    state = store.load_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="job not found")
    p = store.artifact_path(job_id, "cost_report.json")
    if not p.is_file():
        return {"job_id": job_id, "status": state.get("status"),
                "cost_report": None,
                "note": "no cost report yet — no generation has run for this job"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=500, detail="cost report unreadable")
