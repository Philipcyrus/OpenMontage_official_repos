"""Plays the role of Dify against the launcher, in-process (no server, no LLM).

Runs the full gate handshake with the MockRunner and asserts each transition:
    start -> GATE 1 (script) -> GATE 2 (storyboard) -> GATE 3 (final video) -> done

Proves the Dify-facing contract, local storage, checkpoint/resume, and that a REAL clean
video is produced by the folded render. Run:  python dify_launcher/test_dify_flow.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DIFY_RUNNER", "mock")
_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from fastapi.testclient import TestClient

from dify_launcher.app import app
from dify_launcher import store

c = TestClient(app)


def _step(label, resp, want_gate=None, want_status=None):
    body = resp.json()
    print(f"\n[{label}] {resp.status_code}  status={body.get('status')} gate={body.get('gate')}")
    print("   artifacts:", body.get("artifacts"))
    assert resp.status_code == 200, f"{label}: HTTP {resp.status_code} {resp.text}"
    if want_gate is not None:
        assert body.get("gate") == want_gate, f"{label}: gate={body.get('gate')} want {want_gate}"
    if want_status is not None:
        assert body.get("status") == want_status, f"{label}: status={body.get('status')}"
    return body


# 0) health
h = c.get("/health").json()
print("health:", h)

# 1) Dify posts the brief -> GATE 1 (approve_script)
b = _step("POST /jobs", c.post("/jobs", json={
    "brief": "35s vertical eSIM tip: Panda mascot explains avoiding roaming fees."
}), want_gate="approve_script", want_status="awaiting_human")
job = b["job_id"]

# script artifact is fetchable (as Dify would show it)
sc = c.get(b["artifacts"]["script"])
assert sc.status_code == 200 and "Brief" in sc.text, "script artifact not served"
print("   script preview:", sc.text.splitlines()[0])

# 2) user approves script -> GATE 2 (approve_storyboard)
b = _step("respond approve (script)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_storyboard", want_status="awaiting_human")
assert b["artifacts"].get("stills"), "no storyboard stills produced"

# (demonstrate a revision round too)
_step("respond revise (storyboard)", c.post(f"/jobs/{job}/respond",
      json={"decision": "revise", "answer": "make scene 2 brighter"}),
      want_gate="approve_storyboard", want_status="awaiting_human")

# 3) user approves storyboard -> clips generated -> GATE 3 (approve_clips)
b = _step("respond approve (storyboard)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_clips", want_status="awaiting_human")
clips = b["artifacts"].get("clips")
assert clips and len(clips) == 3, f"expected 3 clips, got {clips}"

# revise a specific shot (only that clip regenerates) -> stays at clips gate
_step("respond revise shot 1 (clips)", c.post(f"/jobs/{job}/respond",
      json={"decision": "revise", "shots": [1], "answer": "more motion on shot 2"}),
      want_gate="approve_clips", want_status="awaiting_human")

# 3b) approve clips -> production assembles -> GATE 4 (approve_final)
b = _step("respond approve (clips)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_final", want_status="awaiting_human")
assert b["artifacts"].get("final") == f"/jobs/{job}/artifacts/final.mp4"
assert b["artifacts"].get("branded") is False, "final should be UNBRANDED"

# final video is fetchable + real
fv = c.get(f"/jobs/{job}/artifacts/final.mp4")
assert fv.status_code == 200 and len(fv.content) > 1000, "final.mp4 not served / empty"
final_path = store.artifact_path(job, "final.mp4")
print("   final.mp4 bytes:", final_path.stat().st_size)

# 4) user approves final -> done
_step("respond approve (final)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
      want_status="done")

print("\n[PASS] FULL DIFY GATE FLOW: start -> script -> storyboard -> final -> done")
print("   job dir:", store.job_dir(job))
