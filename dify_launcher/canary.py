"""Read the daily health canary's result file.

Shared by two callers that must never disagree about what the file means: the main
launcher's `GET /health/canary`, and the standalone health service in
`deploy/panda_health_service.py`. The staleness rule in particular has to be identical
in both, or the same box reports two different verdicts depending on which port Dify
happened to ask.

Deliberately stdlib-only, with no import of the engine, the runner, or `app.py`. The
health service exists to keep answering when those are broken, so nothing here may be
able to break along with them.

The file itself is written by `deploy/panda_healthcheck.py` (cron, 07:00 Pacific).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Same env var the canary writes with, so relocating the file moves both ends at once.
DEFAULT_STATE_FILE = "~/.local/state/panda-healthcheck/status.json"
# One daily run plus two hours of grace. Past this the stored verdict is no longer
# evidence about now; it is evidence that cron stopped running.
DEFAULT_MAX_AGE_S = 26 * 3600


def state_path() -> Path:
    return Path(os.environ.get("PANDA_HEALTH_STATE_FILE", DEFAULT_STATE_FILE)).expanduser()


def max_age_s() -> int:
    try:
        return int(os.environ.get("PANDA_HEALTH_MAX_AGE_S", str(DEFAULT_MAX_AGE_S)))
    except ValueError:
        return DEFAULT_MAX_AGE_S


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp, treating a naive one as UTC (the canary writes aware)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_canary(
    path: Optional[Path] = None,
    max_age: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return the last canary verdict, with freshness folded into a single `status`.

    Never raises. A missing or malformed file is itself a reportable state
    ("never_run") rather than an exception for the caller to turn into a 500.

    `status` is the field consumers branch on, because `healthy` alone is a trap: a
    stale result can say `healthy: true`, meaning "the last check passed, and then the
    checker died". Reporting that as a pass would show a green morning indefinitely
    after the box stopped checking anything.
    """
    path = path or state_path()
    max_age = max_age_s() if max_age is None else max_age
    now = now or datetime.now(timezone.utc)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state file is not a JSON object")
    except (OSError, ValueError) as exc:
        return {
            "status": "never_run", "healthy": None, "stale": True,
            "checked_at": None, "age_seconds": None, "first_failed_at": None,
            "detail": f"no canary result at {path} ({type(exc).__name__})",
            "codes": [], "results": [],
        }

    checked_at = raw.get("checked_at")
    when = parse_ts(checked_at)
    age = None if when is None else int((now - when).total_seconds())
    healthy = raw.get("healthy")
    stale = age is None or age > max_age

    return {
        "status": "stale" if stale else ("ok" if healthy else "failed"),
        "healthy": None if healthy is None else bool(healthy),
        "checked_at": checked_at,
        "age_seconds": age,
        "stale": stale,
        "first_failed_at": raw.get("first_failed_at"),
        "codes": raw.get("codes", []),
        "results": raw.get("results", []),
    }
