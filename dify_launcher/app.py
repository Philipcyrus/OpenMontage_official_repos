"""Dify launcher — the HTTP service Dify talks to.

This is the ONLY networked surface. The OpenMontage engine is not a service; this launcher
starts/resumes agent runs and surfaces the approval gates so Dify can show them to the user.

Endpoints:
  GET  /health
  POST /jobs                      {brief, pipeline?, profile?, options?}  -> start a run
  GET  /jobs/{id}                                                  -> current state + gate + artifacts
  POST /jobs/{id}/respond         {decision: approve|revise, ...}  -> resume to next gate
  POST /jobs/{id}/brand           {profile: bgc}                   -> stamp BGC wordmark on done stills
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

import json
import os
import threading
from typing import Any, Callable, Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from dify_launcher import runner as _runner
from dify_launcher import store

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


def _auth(tok: Optional[str]) -> None:
    if _TOKEN and tok != _TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing X-Dify-Token")


def _public(state: dict[str, Any]) -> dict[str, Any]:
    """The view Dify gets: enough to render the gate + links to artifacts."""
    job_id = state["job_id"]
    arts = state.get("artifacts", {})
    links: dict[str, Any] = {}
    for key, val in arts.items():
        if isinstance(val, str) and val.endswith((".md", ".png", ".jpg", ".mp4")):
            links[key] = f"/jobs/{job_id}/artifacts/{val}"
        elif isinstance(val, list):
            links[key] = [f"/jobs/{job_id}/artifacts/{v}" for v in val]
        else:
            links[key] = val
    return {
        "job_id": job_id,
        "pipeline": state.get("pipeline"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "gate": state.get("gate"),
        "question": state.get("question"),
        "artifacts": links,
    }


def _bg(job_id: str, fn: Callable[..., dict[str, Any]], state: dict[str, Any],
        arg: Optional[dict[str, Any]] = None) -> None:
    """Run one agent leg in the background; persist the result (or a failed state)."""
    try:
        result = fn(state) if arg is None else fn(state, arg)
        store.save_state(result)
    except Exception as e:  # noqa: BLE001 — surface any leg failure to the poller
        st = store.load_state(job_id) or state
        st.update(status="failed", gate=None, question=f"error: {e}")
        store.save_state(st)
    finally:
        with _LOCK:
            _RUNNING.discard(job_id)


def _spawn(job_id: str, fn: Callable[..., dict[str, Any]], state: dict[str, Any],
           arg: Optional[dict[str, Any]] = None) -> None:
    with _LOCK:
        _RUNNING.add(job_id)
    threading.Thread(target=_bg, args=(job_id, fn, state, arg), daemon=True).start()


class StartJob(BaseModel):
    brief: str
    pipeline: Optional[str] = None     # panda-video (default) | panda-carousel
    profile: Optional[str] = "ugc"
    options: dict[str, Any] = {}


class Respond(BaseModel):
    decision: str = "approve"          # "approve" | "revise" | "cancel" (cancel: only at budget gate)
    answer: Optional[str] = None
    stills: list[str] = []             # optional user-supplied storyboard stills (paths)
    shots: list[int] = []              # optional 1-based indices: stills (GATE 3) or clips (GATE 4)
    mode: Optional[Literal["fresh", "edit"]] = None  # stills revise only; omit to infer
    max_higgsfield_credits: Optional[int] = None   # at the budget gate: raise the approved cap


class BrandBody(BaseModel):
    profile: str = "bgc"               # only bgc is implemented (wordmark stamp on stills)


def _resolve_pipeline(name: Optional[str]) -> str:
    p = (name or os.environ.get("PANDA_PIPELINE_TYPE") or "panda-video").strip()
    try:
        from lib.pipeline_loader import list_pipelines
        known = list_pipelines()
    except Exception:  # noqa: BLE001 — launcher must still start if the loader isn't importable
        known = ["panda-video", "panda-carousel"]
    if p not in known:
        raise HTTPException(status_code=400,
                            detail=f"unknown pipeline {p!r}; known: {sorted(known)}")
    return p


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "runner": _RUNNER_NAME, "async": _ASYNC,
            "montage_door": _MONTAGE_DOOR}


@app.post("/jobs")
def create_job(body: StartJob, x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    _auth(x_dify_token)
    job_id = store.new_job_id()
    store.ensure_job(job_id)          # create the job + artifacts dir before the runner writes
    pipeline = _resolve_pipeline(body.pipeline)
    state = {
        "job_id": job_id, "brief": body.brief, "pipeline": pipeline,
        "profile": body.profile, "options": body.options, "status": "running",
        "stage": None, "gate": None, "artifacts": {},
    }
    if _ASYNC:
        state["question"] = "starting — poll GET /jobs/{id} until status is awaiting_human"
        store.save_state(state)       # persist first so GET works immediately
        _spawn(job_id, _RUNNER.start, state)
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
    if _ASYNC:
        running = {**state, "status": "running",
                   "question": "processing — poll GET /jobs/{id} until status changes"}
        store.save_state(running)
        _spawn(job_id, _RUNNER.resume, state, body.model_dump())
        return _public(running)
    state = _RUNNER.resume(state, body.model_dump())
    store.save_state(state)
    return _public(state)


@app.post("/jobs/{job_id}/brand")
def brand_job(job_id: str, body: BrandBody = BrandBody(),
              x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    """Stamp the BGC wordmark onto approved stills. Job must be `done`. Idempotent."""
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
