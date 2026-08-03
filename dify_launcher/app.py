"""Dify launcher — the HTTP service Dify talks to.

This is the ONLY networked surface. The OpenMontage engine is not a service; this launcher
starts/resumes agent runs and surfaces the approval gates so Dify can show them to the user.

Endpoints:
  GET  /health
  POST /jobs                      {brief, profile?, options?}      -> start a run (stops at GATE 1)
  GET  /jobs/{id}                                                  -> current state + gate + artifacts
  POST /jobs/{id}/respond         {decision: approve|revise, answer?, stills?} -> resume to next gate
  GET  /jobs/{id}/artifacts/{name}                                 -> download a still/script/final.mp4

Runner is chosen by env DIFY_RUNNER = mock (default, testable here) | claude (EC2).
Storage is local (see store.py). Auth: optional shared token via env DIFY_TOKEN (X-Dify-Token).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from dify_launcher import runner as _runner
from dify_launcher import store

app = FastAPI(title="Panda AI — Dify Launcher", version="0.1.0")

_RUNNER = _runner.get_runner(os.environ.get("DIFY_RUNNER", "mock"))
_TOKEN = os.environ.get("DIFY_TOKEN", "")


def _auth(tok: Optional[str]) -> None:
    if _TOKEN and tok != _TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing X-Dify-Token")


def _public(state: dict[str, Any]) -> dict[str, Any]:
    """The view Dify gets: enough to render the gate + links to artifacts."""
    job_id = state["job_id"]
    arts = state.get("artifacts", {})
    links = {}
    for key, val in arts.items():
        if isinstance(val, str) and val.endswith((".md", ".png", ".jpg", ".mp4")):
            links[key] = f"/jobs/{job_id}/artifacts/{val}"
        elif isinstance(val, list):
            links[key] = [f"/jobs/{job_id}/artifacts/{v}" for v in val]
        else:
            links[key] = val
    return {
        "job_id": job_id,
        "status": state.get("status"),
        "stage": state.get("stage"),
        "gate": state.get("gate"),
        "question": state.get("question"),
        "artifacts": links,
    }


class StartJob(BaseModel):
    brief: str
    profile: Optional[str] = "ugc"
    options: dict[str, Any] = {}


class Respond(BaseModel):
    decision: str = "approve"          # "approve" | "revise"
    answer: Optional[str] = None
    stills: list[str] = []             # optional user-supplied storyboard stills (paths)
    shots: list[int] = []              # optional: at the clips gate, which shot indices to revise


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "runner": os.environ.get("DIFY_RUNNER", "mock")}


@app.post("/jobs")
def create_job(body: StartJob, x_dify_token: Optional[str] = Header(None)) -> dict[str, Any]:
    _auth(x_dify_token)
    job_id = store.new_job_id()
    store.ensure_job(job_id)          # create the job + artifacts dir before the runner writes
    state = {
        "job_id": job_id, "brief": body.brief, "profile": body.profile,
        "options": body.options, "status": "running", "stage": None,
        "gate": None, "artifacts": {},
    }
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
    if state.get("status") != "awaiting_human":
        raise HTTPException(status_code=409, detail=f"job is {state.get('status')}, not awaiting_human")
    state = _RUNNER.resume(state, body.model_dump())
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
