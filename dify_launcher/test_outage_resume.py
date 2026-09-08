"""An agent outage must park the job as resumable, never kill it.

Before this, every leg failure became status="failed". A failed job cannot be resumed —
respond() accepts only "awaiting_human" — so an outage threw away every Higgsfield credit
already spent and the brief had to be run again from the top. The retry inside _run_agent
spans ~9 seconds, which an outage exhausts instantly, so retrying was never the answer.

Run: python dify_launcher/test_outage_resume.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DIFY_RUNNER", "mock")

from dify_launcher import runner as R  # noqa: E402
from dify_launcher import store  # noqa: E402


def _fake_proc(returncode: int, stdout: str = "", stderr: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _Runs:
    """Stand in for subprocess.run and record what the leg was asked to do."""

    def __init__(self, proc: types.SimpleNamespace) -> None:
        self.proc = proc
        self.prompts: list[str] = []

    def __call__(self, argv, **kw):  # noqa: ANN001
        self.prompts.append(argv[2] if len(argv) > 2 else "")
        return self.proc


_real_run, _real_sleep = subprocess.run, time.sleep
time.sleep = lambda *_a, **_k: None  # never actually wait out the backoff

run = R.ClaudeCodeRunner()

# ---------------------------------------------------------------------------
# 1) an infrastructure failure on every attempt -> AgentUnavailable, carrying the prompt
# ---------------------------------------------------------------------------
# Claude prints the real error to stdout while stderr holds only its stdin warning — the
# exact shape that used to hide a 529 entirely.
outage = _Runs(_fake_proc(1, stdout="API Error: 529 Overloaded", stderr="no stdin data"))
subprocess.run = outage
try:
    run._run_agent("THE-INTERRUPTED-PROMPT", "job_t", "assets_media")
    raise SystemExit("FAIL: an outage must raise")
except R.AgentUnavailable as e:
    assert e.prompt == "THE-INTERRUPTED-PROMPT", "the prompt must survive for the resume"
    assert e.label == "assets_media"
    assert "529" in str(e)
assert len(outage.prompts) == 3, f"all 3 attempts should be spent, got {len(outage.prompts)}"
print("[ok] outage -> AgentUnavailable after every attempt, prompt preserved")

# ---------------------------------------------------------------------------
# 2) a real job error is still a plain failure — this must NOT become resumable
# ---------------------------------------------------------------------------
bad = _Runs(_fake_proc(1, stdout="TypeError: scene_plan is not valid", stderr=""))
subprocess.run = bad
try:
    run._run_agent("p", "job_t", "script")
    raise SystemExit("FAIL: a job error must raise")
except R.AgentUnavailable:
    raise SystemExit("FAIL: a job error must NOT be classed as an outage")
except RuntimeError:
    pass
assert len(bad.prompts) == 1, "a non-transient error must not be retried"
print("[ok] job error -> plain RuntimeError, no retry, not resumable")

# ---------------------------------------------------------------------------
# 3) the launcher parks it as a hold instead of killing it
# ---------------------------------------------------------------------------
subprocess.run = outage
JOB = "job_outagetest"
store.ensure_job(JOB)
store.save_state({"job_id": JOB, "status": "running", "stage": "assets",
                  "gate": "approve_stills", "pipeline": "panda-video", "artifacts": {}})

from dify_launcher import app as A  # noqa: E402


def _boom(_state):
    raise R.AgentUnavailable("claude failed: API Error: 529 Overloaded",
                             prompt="THE-INTERRUPTED-PROMPT", label="assets_media")


A._bg(JOB, _boom, store.load_state(JOB))
parked = store.load_state(JOB)

assert parked["status"] == "awaiting_human", f'got {parked["status"]!r}, must be resumable'
assert parked["gate"] == R._OUTAGE_GATE
saved = parked[R._OUTAGE_KEY]
assert saved["prompt"] == "THE-INTERRUPTED-PROMPT"
assert saved["stage"] == "assets" and saved["gate"] == "approve_stills", "pre-outage position kept"
assert "credits were lost" in parked["question"] and "approve" in parked["question"]
print("[ok] outage parked as awaiting_human/agent_unavailable, position + prompt kept")

# the API boundary: this used to 409 forever because the job was "failed"
assert parked["status"] == "awaiting_human", "respond() gates on exactly this"
print("[ok] respond() precondition satisfied — the job is no longer a dead end")

# ---------------------------------------------------------------------------
# 4) approving re-issues the SAME leg and restores the pre-outage position
# ---------------------------------------------------------------------------
issued: list[tuple[str, str]] = []
run._run_agent = lambda prompt, job_id="", label="": issued.append((prompt, label))  # type: ignore[method-assign]
run._sync = lambda state: state  # type: ignore[method-assign]

resumed = run.resume(dict(parked), {"decision": "approve"})
assert issued == [("THE-INTERRUPTED-PROMPT", "assets_media")], issued
assert resumed["stage"] == "assets" and resumed["gate"] == "approve_stills"
assert R._OUTAGE_KEY not in resumed, "the hold must be cleared once resumed"
print("[ok] approve -> the interrupted leg is re-issued verbatim, position restored")

# ---------------------------------------------------------------------------
# 5) revise is the escape hatch — abandon without running anything
# ---------------------------------------------------------------------------
issued.clear()
abandoned = run.resume(dict(parked), {"decision": "revise"})
assert abandoned["status"] == "failed" and abandoned["gate"] is None
assert issued == [], "abandoning must not run a leg"
assert R._OUTAGE_KEY not in abandoned
print("[ok] revise -> abandoned cleanly, nothing run")

subprocess.run, time.sleep = _real_run, _real_sleep
import shutil  # noqa: E402
shutil.rmtree(store.job_dir(JOB), ignore_errors=True)
print("\n[PASS] agent outage: parked resumable, position kept, approve resumes, revise abandons")
