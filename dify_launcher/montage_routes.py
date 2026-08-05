"""The montage-svc "raw render" door — mounted UNDER the launcher, additive.

This exposes montage-svc's render abilities (compose / overlay / mix-audio / media
import) as a SECOND, independent entrance, without touching the agent pipeline. It is
the exact same render core the agent already uses in-process (vendor/montage_svc), just
reachable directly over HTTP for a caller who wants to drive the renderer themselves.

Two doors, one engine:

    /jobs/*      -> Claude agent -> PandaRender ──┐
                                                   ├──> vendor/montage_svc/render/pipelines.py
    /montage/*   -> these routes ─────────────────┘   (compose / overlay / mix)

Nothing here edits the /jobs handlers, the agent, or PandaRender. Both callers hit the
SAME stateless render functions; a request on one door never changes the other's behavior.
The only thing shared is the render code and the box's CPU/disk.

Contract mirrors the standalone montage-svc service (async: POST returns 202 + job_id,
poll GET /montage/jobs/{id}), so a team that already knows montage-svc's API can use it
unchanged — only the path prefix (/montage) and the /montage/files mount differ.

Auth is SEPARATE from the launcher's X-Dify-Token: these routes honor montage-svc's own
X-Panda-Token (env PANDA_TOKEN). Unset => open (fine on a private tunnel; set it before
exposing publicly). /montage/health and /montage/files/* are always open.

Job registry is in-process (thread per job), matching the launcher's own async model — no
SQLite, no new heavy dep. Single-worker uvicorn assumed (same as the launcher).
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

# --- point the vendored montage_svc at the SAME brand/data dirs the agent uses ----------
# Mirrors tools/video/panda_render.py so both doors share one data dir + brand assets.
# setdefault: whichever module imports first wins, and both set identical values.
_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_VENDOR = _ENGINE_ROOT / "vendor"
os.environ.setdefault("MONTAGE_BRAND_DIR", str(_VENDOR / "brand"))
os.environ.setdefault("MONTAGE_DATA_DIR", str(_VENDOR / "data"))
import sys  # noqa: E402
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from fastapi import APIRouter, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from montage_svc.config import (  # noqa: E402
    CONTENT_TYPE_EXT,
    DATA_DIR,
    DOWNLOAD_TIMEOUT_S,
    MEDIA_EXTS,
    fonts_ok,
    has_ffmpeg,
)
from montage_svc.render.pipelines import run_compose, run_mix, run_overlay  # noqa: E402
from montage_svc.schemas import (  # noqa: E402
    ComposeRequest,
    HealthResponse,
    ImportRequest,
    ImportResponse,
    JobAccepted,
    JobStatus,
    MixRequest,
    OverlayRequest,
)
from montage_svc.storage import (  # noqa: E402
    StorageError,
    check_id,
    ensure_default_logo,
    ensure_dirs,
    ensure_profiles,
    ensure_run,
    files_url,
    kind_for_ext,
    list_profiles,
    load_profile,
    missing_media,
    resolve_source,
    save_media,
)

# Exported for the StaticFiles mount in app.py (serves outputs at /montage/files/...).
MONTAGE_DATA_DIR = DATA_DIR

_TOKEN = os.environ.get("PANDA_TOKEN", "")
_AUTH_HEADER = "X-Panda-Token"
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

router = APIRouter(prefix="/montage", tags=["montage-svc (raw render door)"])

# --- in-process job registry ------------------------------------------------------------
_JOBS: dict[str, dict[str, Any]] = {}
_KEYS: dict[tuple[str, str, int], str] = {}   # (run_id, kind, version) -> job_id (idempotency)
_LOCK = threading.Lock()

_RUNNERS = {"compose": run_compose, "overlay": run_overlay, "mix": run_mix}


def init() -> None:
    """Ensure the render data dir + default profiles/logo exist. Safe to call repeatedly."""
    ensure_dirs()
    ensure_profiles()
    ensure_default_logo()


def _auth(tok: Optional[str]) -> None:
    if _TOKEN and tok != _TOKEN:
        raise HTTPException(status_code=401, detail=f"missing or invalid {_AUTH_HEADER}")


def _montage_url(path: Path) -> str:
    """Map an on-disk output path to its public /montage/files/... URL."""
    return "/montage" + files_url(path)   # files_url -> "/files/runs/.../out/final_v1.mp4"


def _new_job_id() -> str:
    # SAFE_ID-compatible (^[a-zA-Z0-9_-]{1,64}$) — work_dir/check_id require it.
    return "mj_" + uuid.uuid4().hex[:16]


def _worker(job_id: str, kind: str, req: Any) -> None:
    runner = _RUNNERS[kind]

    def progress(f: float) -> None:
        with _LOCK:
            j = _JOBS.get(job_id)
            if j:
                j["progress"] = round(float(f), 3)

    with _LOCK:
        _JOBS[job_id]["status"] = "running"
    try:
        path = runner(req, job_id, progress)
        with _LOCK:
            _JOBS[job_id].update(status="done", progress=1.0,
                                 output_media_url=_montage_url(Path(path)))
    except Exception as e:  # noqa: BLE001 — surface any render failure to the poller
        with _LOCK:
            _JOBS[job_id].update(status="failed", error=str(e))


def _accept(kind: str, req: Any) -> JSONResponse:
    """Create-or-return: idempotent on (run_id, kind, version), matching montage-svc."""
    key = (req.run_id, kind, req.version)
    with _LOCK:
        existing = _KEYS.get(key)
        if existing and _JOBS.get(existing, {}).get("status") != "failed":
            return JSONResponse({"job_id": existing}, status_code=202)
        job_id = _new_job_id()
        _JOBS[job_id] = {
            "job_id": job_id, "kind": kind, "run_id": req.run_id,
            "status": "queued", "progress": 0.0,
            "output_media_url": None, "error": None,
        }
        _KEYS[key] = job_id
    threading.Thread(target=_worker, args=(job_id, kind, req), daemon=True).start()
    return JSONResponse({"job_id": job_id}, status_code=202)


def _require_profile(name: str) -> None:
    try:
        load_profile(name)
    except StorageError as exc:
        raise HTTPException(400, str(exc)) from exc


def _require_media(run_id: str, media_ids: list[str]) -> None:
    missing = missing_media(run_id, media_ids)
    if missing:
        raise HTTPException(400, {
            "error": "missing media", "run_id": run_id, "missing": missing,
            "hint": "POST /montage/media/import each of these first",
        })


# --- health -----------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    ff = has_ffmpeg()
    ft = fonts_ok()
    dd = DATA_DIR.is_dir()
    warnings: list[str] = []
    if not ff:
        warnings.append("ffmpeg or ffprobe not found on PATH")
    if not ft:
        warnings.append("a profile font could not be resolved; CJK captions may be empty boxes")
    if not _TOKEN:
        warnings.append("auth disabled (no PANDA_TOKEN) — open door; set a token before public exposure")
    return HealthResponse(
        status="ok" if (ff and ft and dd) else "degraded",
        ffmpeg=ff, fonts=ft, data_dir=dd,
        profiles=list_profiles(), warnings=warnings,
    )


# --- media import -----------------------------------------------------------------------

def _ext_for(url: str, content_type: str) -> str:
    """Content-Type first, then the URL's extension (CDNs often serve octet-stream)."""
    import mimetypes
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[ct]
    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix in MEDIA_EXTS:
        return suffix
    guessed = mimetypes.guess_extension(ct) if ct else None
    if guessed and guessed.lower() in MEDIA_EXTS:
        return guessed.lower()
    raise HTTPException(400, f"unsupported media type {content_type!r} for {url!r}; "
                            f"accepted: {sorted(MEDIA_EXTS)}")


@router.post("/media/import", response_model=ImportResponse)
def media_import(req: ImportRequest, x_panda_token: Optional[str] = Header(None)) -> ImportResponse:
    _auth(x_panda_token)
    import requests  # lazy: keeps the launcher bootable even without `requests` installed
    ensure_run(req.run_id)
    try:
        with requests.get(req.url, timeout=DOWNLOAD_TIMEOUT_S, stream=True) as r:
            r.raise_for_status()
            ext = _ext_for(req.url, r.headers.get("Content-Type", ""))
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(1 << 16):
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    raise HTTPException(502, "download aborted: asset exceeds 512 MB")
                chunks.append(chunk)
    except HTTPException:
        raise
    except requests.RequestException as exc:  # type: ignore[attr-defined]
        raise HTTPException(502, f"download failed: {exc}") from exc

    data = b"".join(chunks)
    if not data:
        raise HTTPException(502, "download failed: empty response")
    path = save_media(req.run_id, req.label, ext, data)
    kind = kind_for_ext(ext)
    return ImportResponse(media_id=req.label, local_url=_montage_url(path),
                          bytes=len(data), kind=kind)


# --- render endpoints (async: 202 + job_id, poll GET /montage/jobs/{id}) ----------------

@router.post("/compose", response_model=JobAccepted, status_code=202)
def compose(req: ComposeRequest, x_panda_token: Optional[str] = Header(None)) -> JSONResponse:
    _auth(x_panda_token)
    ensure_run(req.run_id)
    _require_profile(req.profile)
    _require_media(req.run_id, req.media_ids())
    return _accept("compose", req)


@router.post("/overlay", response_model=JobAccepted, status_code=202)
def overlay(req: OverlayRequest, x_panda_token: Optional[str] = Header(None)) -> JSONResponse:
    _auth(x_panda_token)
    ensure_run(req.run_id)
    _require_profile(req.profile)
    resolve_source(req.run_id, req.source)   # 400 if the source video is absent
    return _accept("overlay", req)


@router.post("/mix-audio", response_model=JobAccepted, status_code=202)
def mix_audio(req: MixRequest, x_panda_token: Optional[str] = Header(None)) -> JSONResponse:
    _auth(x_panda_token)
    ensure_run(req.run_id)
    resolve_source(req.run_id, req.source)
    _require_media(req.run_id, req.audio.media_ids())
    return _accept("mix", req)


@router.get("/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str, x_panda_token: Optional[str] = Header(None)) -> JobStatus:
    _auth(x_panda_token)
    check_id(job_id, "job_id")
    with _LOCK:
        job = _JOBS.get(job_id)
        job = dict(job) if job else None
    if not job:
        raise HTTPException(404, f"job {job_id!r} not found")
    return JobStatus(**job)


async def storage_error_handler(_request: Request, exc: StorageError):
    """App-level handler (registered in app.py): a bad id/missing asset -> 400, not 500."""
    return JSONResponse({"detail": str(exc)}, status_code=400)
