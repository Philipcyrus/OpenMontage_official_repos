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


def _recover_worker_result(
    result: dict[str, Any],
    original: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Never let an exiting async worker strand a job in `running`."""
    recovered = dict(result)
    gate = (
        recovered.get("gate")
        or recovered.get("_recovery_gate")
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
    stage = (
        recovered.get("stage")
        or recovered.get("_recovery_stage")
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
    try:
        result = fn(state) if arg is None else fn(state, arg)
        persisted = store.load_state(job_id) or {}
        if persisted.get("processing_started_at") and not result.get("processing_started_at"):
            result["processing_started_at"] = persisted["processing_started_at"]
        if result.get("status") == "running":
            result = _recover_worker_result(result, state, "worker returned status=running")
        else:
            result = _clear_processing_markers(result)
        store.save_state(result)
    except Exception as e:  # noqa: BLE001 — surface any leg failure to the poller
        st = store.load_state(job_id) or state
        st = _recover_worker_result(st, state, f"error: {e}")
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
    return {"status": "ok", "runner": _RUNNER_NAME, "async": _ASYNC,
            "montage_door": _MONTAGE_DOOR,
            "process_started_at": _PROCESS_STARTED_AT,
            "build_revision": _LOADED_REVISION,
            "launcher_code_fingerprint": _LAUNCHER_CODE_FINGERPRINT}


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


@app.post("/jobs")
def create_job(body: StartJob, x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    _auth(x_dify_token)
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
        effective, _coerced = _runner._coerce_language_from_brief(options, body.brief or "")
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
    if _ASYNC:
        running = {
            **state,
            "status": "running",
            "gate": None,
            "question": "processing — poll GET /jobs/{id} until status changes",
            "_recovery_gate": state.get("gate"),
            "_recovery_stage": state.get("stage"),
            "_processing_operation": f"resume:{state.get('gate') or 'unknown'}",
            "processing_started_at": _utc_now(),
        }
        _spawn(
            job_id,
            _RUNNER.resume,
            state,
            body.model_dump(),
            processing_state=running,
        )
        return _public(running)
    try:
        state = _RUNNER.resume(state, body.model_dump())
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
