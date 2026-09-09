"""Standalone health-reporting service — a second, tiny launcher whose only job is to
still be answering when the main one is not.

Why this is a separate process rather than a route on the main launcher: a monitor that
shares a failure domain with the thing it monitors cannot report that thing's failure.
Serving the canary result from `dify_launcher/app.py` works right up until uvicorn dies,
at which point Dify gets a connection error and cannot tell "the launcher is down" apart
from "the network is down" or "my own HTTP node misfired". This process has no such
blind spot: it imports nothing from the engine, holds no job state, and keeps answering
when the main launcher, the runner, the MCP connection, or the engine are all broken.

It answers two different questions in one response:

  launcher_live  a fresh HTTP probe of the main launcher, done now, costing ~nothing
  canary         the stored result of the daily 07:00 Pacific deep check, which spends
                 a real Claude turn proving auth and Higgsfield still work

The deep check stays in cron. It takes up to ~260s worst case, which no HTTP caller
should ever wait on — see `dify_launcher/DIFY_INTEGRATION.md` §4.

Run:
    python -m uvicorn deploy.panda_health_service:app --host 0.0.0.0 --port 8502

Bind 0.0.0.0, not 127.0.0.1: the reverse proxy reaches this box from another network
namespace, and a loopback bind gives Dify 502s while every check run on the box passes.
Restrict inbound 8502 in the EC2 security group instead.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException

from dify_launcher import canary as _canary

app = FastAPI(title="Panda AI — Health Service", version="1.0.0")

# Same optional shared secret as the main launcher, so operators have one token to think
# about. Unset (the current default) means open.
_TOKEN = os.environ.get("DIFY_TOKEN", "")
_LAUNCHER_URL = os.environ.get("PANDA_HEALTH_LAUNCHER_URL", "http://127.0.0.1:8501/health")
# Short by design. This probe runs inside a request Dify is waiting on, and a launcher
# that needs more than a couple of seconds to answer /health is already a problem.
_PROBE_TIMEOUT_S = float(os.environ.get("PANDA_HEALTH_PROBE_TIMEOUT_S", "3"))


def _auth(tok: Optional[str]) -> None:
    if _TOKEN and tok != _TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing X-Dify-Token")


def probe_launcher(url: str = "", timeout: float = 0.0) -> Dict[str, Any]:
    """Ask the main launcher whether it is alive, right now. Never raises.

    This is the half of the report that the main launcher structurally cannot provide
    about itself.
    """
    url = url or _LAUNCHER_URL
    timeout = timeout or _PROBE_TIMEOUT_S
    request = urllib.request.Request(url, headers={"User-Agent": "panda-health-service/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = json.loads(response.read(64 * 1024).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "code": "LAUNCHER_DOWN",
                "detail": f"{type(exc).__name__}: {exc}"[:300], "url": url}
    if status != 200 or body.get("status") != "ok":
        return {"ok": False, "code": "LAUNCHER_UNHEALTHY",
                "detail": f"HTTP {status}; status={body.get('status')!r}", "url": url}
    return {"ok": True, "code": "LAUNCHER_OK", "url": url,
            "runner": body.get("runner"), "async": body.get("async"),
            "montage_door": body.get("montage_door")}


@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness of THIS service. Deliberately does no I/O, so it cannot be dragged down
    by whatever it is reporting on."""
    return {"status": "ok", "service": "panda-health", "watching": _LAUNCHER_URL}


@app.get("/health/canary")
def health_canary(x_dify_token: Optional[str] = Header(None)) -> Dict[str, Any]:
    """The morning report: live launcher state plus the stored daily deep check.

    Same path and same field names the main launcher serves, so Dify can be pointed at
    either port without a workflow change. The difference is that this one keeps
    answering — and keeps saying something useful — when the main launcher is down.

    `status` is the single field to branch on. It is `ok` only when the launcher is live
    now AND the stored canary both passed and is fresh. A stale PASS is not a pass: it
    means the checker itself stopped running.
    """
    _auth(x_dify_token)
    stored = _canary.read_canary()
    live = probe_launcher()

    if not live["ok"]:
        # The launcher being down now outranks whatever cron concluded hours ago.
        status = "failed"
    else:
        status = stored["status"]

    return {**stored, "status": status, "launcher_live": live,
            "canary_status": stored["status"]}
