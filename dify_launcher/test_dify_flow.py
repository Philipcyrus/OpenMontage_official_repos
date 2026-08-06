"""Plays the role of Dify against the launcher, in-process (no server, no LLM).

Runs the full gate handshake with the MockRunner and asserts each transition — the
best-of-both shape (upstream text plan + a Panda stills cost gate):
    start -> GATE 1 (script) -> GATE 2 (scene_plan, TEXT) -> GATE 3 (stills, NO video)
          -> GATE 4 (assets, all media) -> GATE 5 (final) -> done

Proves the Dify-facing contract, local storage, checkpoint/resume, that scene_plan produces
NO media (text only), that the STILLS gate produces stills with NO video on disk (the cost
gate), that clips + a schema-valid asset_manifest appear only at the assets gate, and that a
REAL clean video is produced by the folded render.
Run:  python dify_launcher/test_dify_flow.py
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
from schemas.artifacts import validate_artifact

c = TestClient(app)


def _step(label, resp, want_gate=None, want_status=None):
    body = resp.json()
    print(f"\n[{label}] {resp.status_code}  status={body.get('status')} gate={body.get('gate')}")
    print("   artifacts:", list((body.get("artifacts") or {}).keys()))
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

# 2) approve script -> GATE 2 (approve_scene_plan): a TEXT plan, NO media -----------------
b = _step("respond approve (script)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_scene_plan", want_status="awaiting_human")
sp = b["artifacts"].get("scene_plan")
# (e) Dify receives TEXT at the scene_plan gate: an inline structured plan
assert isinstance(sp, dict) and sp.get("scenes"), "scene_plan gate must return a text scene list"
validate_artifact("scene_plan", sp)                            # (a/d) schema-valid scene_plan
# (b) the scene_plan gate is reached & approvable WITHOUT stills
assert "stills" not in b["artifacts"], "scene_plan gate must not carry stills"
assert "clips" not in b["artifacts"], "scene_plan gate must not carry clips"
# (a/c) NO media files exist on disk yet
imgs = list(store.artifacts_dir(job).glob("*.png")) + list(store.artifacts_dir(job).glob("*.mp4"))
assert not imgs, f"scene_plan stage must generate NO media, found: {[p.name for p in imgs]}"
print(f"   scene_plan: {len(sp['scenes'])} scenes, text-only, schema-valid, no media on disk")

# a revision round at the scene_plan gate (still text)
b = _step("respond revise (scene_plan)", c.post(f"/jobs/{job}/respond",
          json={"decision": "revise", "answer": "tighten scene 2 timing"}),
          want_gate="approve_scene_plan", want_status="awaiting_human")
assert isinstance(b["artifacts"].get("scene_plan"), dict) and "stills" not in b["artifacts"]

# 3) approve scene_plan -> GATE 3 (approve_stills): STILLS ONLY, NO video yet ------------
b = _step("respond approve (scene_plan)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_stills", want_status="awaiting_human")
stills = b["artifacts"].get("stills")
assert stills and len(stills) == 3, f"expected 3 stills at the stills gate, got {stills}"
# COST GATE: no clips, no manifest, and NO video files exist on disk yet
assert "clips" not in b["artifacts"], "stills gate must NOT carry clips"
assert "asset_manifest" not in b["artifacts"], "stills gate must NOT carry an asset_manifest"
vids = list(store.artifacts_dir(job).glob("*.mp4"))
assert not vids, f"stills gate must generate NO video, found: {[p.name for p in vids]}"
print(f"   stills gate: {len(stills)} stills, NO video on disk (cost gate holds)")

# revise the stills (still no video generated)
b = _step("respond revise (stills)", c.post(f"/jobs/{job}/respond",
          json={"decision": "revise", "answer": "make the panda brighter"}),
          want_gate="approve_stills", want_status="awaiting_human")
assert not list(store.artifacts_dir(job).glob("*.mp4")), "revising stills must not generate video"

# 4) approve stills -> GATE 4 (approve_assets): clips + audio + manifest -----------------
b = _step("respond approve (stills)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_assets", want_status="awaiting_human")
clips = b["artifacts"].get("clips")
manifest = b["artifacts"].get("asset_manifest")
assert clips and len(clips) == 3, f"expected 3 clips at assets, got {clips}"
# (d) generated files are represented in a schema-valid asset_manifest
assert isinstance(manifest, dict), "assets gate must return an asset_manifest"
validate_artifact("asset_manifest", manifest)
assert len(manifest["assets"]) == 6, f"expected 6 manifest assets, got {len(manifest['assets'])}"
assert all(a.get("scene_id") and a.get("path") for a in manifest["assets"]), "manifest assets need scene_id+path"
print(f"   assets: {len(b['artifacts']['stills'])} stills + {len(clips)} clips, manifest {len(manifest['assets'])} assets (schema-valid)")

# revise a specific shot (only that clip regenerates) -> stays at assets gate
_step("respond revise shot 1 (assets)", c.post(f"/jobs/{job}/respond",
      json={"decision": "revise", "shots": [1], "answer": "more motion on shot 2"}),
      want_gate="approve_assets", want_status="awaiting_human")

# 4b) approve assets -> production assembles -> GATE 5 (approve_final)
b = _step("respond approve (assets)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_final", want_status="awaiting_human")
assert b["artifacts"].get("final") == f"/jobs/{job}/artifacts/final.mp4"
assert b["artifacts"].get("branded") is False, "final should be UNBRANDED"

# final video is fetchable + real
fv = c.get(f"/jobs/{job}/artifacts/final.mp4")
assert fv.status_code == 200 and len(fv.content) > 1000, "final.mp4 not served / empty"
print("   final.mp4 bytes:", store.artifact_path(job, "final.mp4").stat().st_size)

# 4) approve final -> done
_step("respond approve (final)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
      want_status="done")

# 5) (g) a legacy job (old storyboard-stills gate) is readable but resume returns a migration note
legacy = store.new_job_id()
store.ensure_job(legacy)
store.save_state({"job_id": legacy, "brief": "old job", "status": "awaiting_human",
                  "stage": "scene_plan", "gate": "approve_storyboard", "artifacts": {}})
readable = c.get(f"/jobs/{legacy}")
assert readable.status_code == 200 and readable.json()["gate"] == "approve_storyboard", "legacy job must stay readable"
mig = _step("respond (legacy gate)", c.post(f"/jobs/{legacy}/respond", json={"decision": "approve"}),
            want_status="failed")
assert "start a new job" in (mig.get("question") or "").lower(), "legacy resume must return a migration message"
print("   legacy migration message OK")

print("\n[PASS] FULL DIFY GATE FLOW: start -> script -> scene_plan(text) -> stills(no video) -> assets(media) -> final -> done")
print("   job dir:", store.job_dir(job))
