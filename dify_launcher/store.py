"""Local, file-based job store for the Dify launcher.

No S3, no Postgres (Phase 5 deferred). Every job lives in a folder:

    {DIFY_DATA_DIR}/jobs/{job_id}/
        state.json          # the job's current state (stage, status, gate, artifacts)
        artifacts/          # script.md, stills, final.mp4, etc.

Swap this module for an S3/Postgres-backed one later without touching the API.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DIFY_DATA_DIR", _ENGINE_ROOT / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def artifacts_dir(job_id: str) -> Path:
    return job_dir(job_id) / "artifacts"


def ensure_job(job_id: str) -> Path:
    artifacts_dir(job_id).mkdir(parents=True, exist_ok=True)
    return job_dir(job_id)


def save_state(state: dict[str, Any]) -> None:
    d = ensure_job(state["job_id"])
    (d / "state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_state(job_id: str) -> dict[str, Any] | None:
    p = job_dir(job_id) / "state.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def artifact_path(job_id: str, name: str) -> Path:
    """Resolve an artifact by basename, confined to the job's artifacts dir."""
    base = artifacts_dir(job_id).resolve()
    p = (base / name).resolve()
    if base not in p.parents and p != base:
        raise ValueError(f"artifact path escapes job dir: {name}")
    return p
