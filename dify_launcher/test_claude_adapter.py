"""Verifies ClaudeCodeRunner's ADAPTER logic — the code the launcher actually owns:
gate<->stage mapping, artifact mirroring/grouping, checkpoint status -> launcher state, and
that approval writes completed+human_approved.

The `claude` CLI and the engine's per-artifact content schemas are NOT exercised here (no
CLI on this box; artifact schemas are the engine's own tested concern). Checkpoint reads are
stubbed so we test the adapter in isolation.

Run:  python dify_launcher/test_claude_adapter.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="panda_ck_"))
os.environ["OPENMONTAGE_PROJECTS_DIR"] = str(_TMP)          # isolate project/checkpoint root
os.environ["DIFY_DATA_DIR"] = str(_TMP / "launcher")        # isolate launcher store
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from dify_launcher import runner as R
from dify_launcher import store
from lib import checkpoint as cp

run = R.ClaudeCodeRunner()

# 1) gate <-> stage mapping ------------------------------------------------
# both assets sub-gates reverse-map to the single `assets` stage
assert run._gate_stage("approve_stills") == "assets"
assert run._gate_stage("approve_motion_sample") == "assets"
assert run._gate_stage("budget_exceeded") == "assets"
assert run._gate_stage("approve_assets") == "assets"
assert run._gate_stage("approve_scene_plan") == "scene_plan"
assert run._gate_stage("approve_script") == "script"
assert R._STAGE_GATE["compose"] == "approve_final"
assert R._STAGE_GATE["scene_plan"] == "approve_scene_plan"
assert "assets" not in R._STAGE_GATE          # assets is phase-resolved, not a 1:1 map entry
print("[ok] gate<->stage mapping")

# 1b) idea is INTERNAL — no human gate. The manifest is authoritative; the agent must never
# surface an unexpected `approve_idea`. (Guards the reused-skill "Gate Reminder" conflict.)
import yaml as _yaml
_pv = _yaml.safe_load(open(_ENGINE_ROOT / "pipeline_defs" / "panda-video.yaml", encoding="utf-8"))
_idea = next(s for s in _pv["stages"] if s["name"] == "idea")
assert _idea["human_approval_default"] is False, "panda idea must be internal (no gate)"
assert "approve_idea" not in R.GATES, "approve_idea must not be a real gate"
assert "idea" not in R._STAGE_GATE, "idea must not map 1:1 to a gate"
assert _idea["skill"] == "pipelines/panda-video/idea-director", "idea must use the Panda idea-director"
print("[ok] idea stage is internal — no approve_idea gate, Panda-specific director")

# 2) _mirror_artifacts: copy project files into the store, grouped by kind --
JOB = "job_mirror"
proj = run._projects_dir / JOB
(proj / "artifacts").mkdir(parents=True, exist_ok=True)
(proj / "artifacts" / "script.md").write_text("# script", encoding="utf-8")
(proj / "assets" / "images").mkdir(parents=True, exist_ok=True)
(proj / "assets" / "video").mkdir(parents=True, exist_ok=True)
(proj / "renders").mkdir(parents=True, exist_ok=True)
for i in range(2):
    (proj / "assets" / "images" / f"still_{i}.png").write_bytes(b"\x89PNG\r\n")
for i in range(3):
    (proj / "assets" / "video" / f"clip_{i}.mp4").write_bytes(b"\x00ftypmp42")
(proj / "renders" / "final.mp4").write_bytes(b"\x00ftypmp42")

# scene_plan.json on disk (surfaced inline for TEXT review at GATE 2); asset_manifest inline
# in the checkpoint (surfaced inline for review at GATE 3).
import json as _json
(proj / "artifacts" / "scene_plan.json").write_text(
    _json.dumps({"version": "1.0", "scenes": [{"id": "s1"}]}), encoding="utf-8")

arts = run._mirror_artifacts(JOB, {
    "script": "artifacts/script.md",
    "asset_manifest": {"version": "1.0", "assets": [{"id": "a1", "scene_id": "s1"}]},
    "stills": ["assets/images/still_0.png", "assets/images/still_1.png"],
    "clips": ["assets/video/clip_0.mp4", "assets/video/clip_1.mp4", "assets/video/clip_2.mp4"],
    "final": "renders/final.mp4",
})
assert arts["script"] == "script.md"
assert len(arts["stills"]) == 2 and len(arts["clips"]) == 3
assert arts["final"] == "final.mp4" and arts["branded"] is False
# structured TEXT artifacts surfaced inline (dict), not as files:
assert isinstance(arts.get("scene_plan"), dict) and arts["scene_plan"]["scenes"][0]["id"] == "s1"
assert isinstance(arts.get("asset_manifest"), dict) and arts["asset_manifest"]["assets"][0]["id"] == "a1"
assert store.artifact_path(JOB, "final.mp4").is_file()       # actually copied into the store
assert store.artifact_path(JOB, "clip_2.mp4").is_file()
print("[ok] artifact mirroring: files (stills/clips/final/script) + inline scene_plan/asset_manifest")

# 2b) SCRIPT-GATE FIX (regression guard): the real script-director writes a STRUCTURED script
# (JSON per script.schema.json), not a .md. It MUST be surfaced INLINE at approve_script so the
# human in Dify sees the actual dialogue/sections — otherwise the gate shows only a label and the
# flow appears to skip the reviewer. Covers: inline checkpoint dict, on-disk script.json, and
# that a structured script wins over a stray .md (surfaced as a dict, never just a link).
JOB2 = "job_script_inline"
proj2 = run._projects_dir / JOB2
(proj2 / "artifacts").mkdir(parents=True, exist_ok=True)
_script_obj = {"version": "1.0", "title": "Panda tip", "total_duration_seconds": 10.0,
               "sections": [{"id": "hook", "text": "Meet Panda.",
                             "start_seconds": 0, "end_seconds": 3}]}
a = run._mirror_artifacts(JOB2, {"script": _script_obj})                 # (a) inline in checkpoint
assert isinstance(a.get("script"), dict) and a["script"]["sections"][0]["text"] == "Meet Panda.", a.get("script")
(proj2 / "artifacts" / "script.json").write_text(_json.dumps(_script_obj), encoding="utf-8")
b = run._mirror_artifacts(JOB2, {})                                      # (b) on-disk script.json
assert isinstance(b.get("script"), dict) and b["script"]["title"] == "Panda tip", b.get("script")
(proj2 / "artifacts" / "notes.md").write_text("# not the script", encoding="utf-8")
c = run._mirror_artifacts(JOB2, {"script": _script_obj})                 # (c) structured wins over .md
assert isinstance(c.get("script"), dict), c.get("script")
print("[ok] structured script surfaced INLINE at approve_script (checkpoint dict + script.json + wins over .md)")

# 2c) A stray non-script .md (e.g. cost_report.md) must NOT be mislabeled as the script at a gate
# whose checkpoint carries no script artifact. Regression: at the scene_plan gate the .md fallback
# grabbed cost_report.md and surfaced it as artifacts.script.
JOB3 = "job_no_script_md"
proj3 = run._projects_dir / JOB3
(proj3 / "artifacts").mkdir(parents=True, exist_ok=True)
(proj3 / "artifacts" / "cost_report.md").write_text("# cost report", encoding="utf-8")
sp = {"version": "1.0", "scenes": [{"id": "s1"}]}
(proj3 / "artifacts" / "scene_plan.json").write_text(_json.dumps(sp), encoding="utf-8")
d = run._mirror_artifacts(JOB3, {"scene_plan": sp})
assert "script" not in d, f"cost_report.md must NOT be surfaced as script: {d.get('script')!r}"
assert isinstance(d.get("scene_plan"), dict), d.get("scene_plan")
print("[ok] stray cost_report.md not mislabeled as script at a scriptless gate")

# 3) _sync: checkpoint status -> launcher state (stub the reads) -----------
def _fake_latest(_pd, _jid):
    return _fake_latest.cp
def _fake_next(_pd, _jid, _pt=None):
    return _fake_next.val
cp.get_latest_checkpoint = _fake_latest
cp.get_next_stage = _fake_next

# assets stage, STILLS phase (partial_progress.phase) -> approve_stills
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {},
                   "partial_progress": {"phase": "stills"}}
st = run._sync({"job_id": "jX"})
assert st["status"] == "awaiting_human" and st["gate"] == "approve_stills"

# assets stage, MOTION SAMPLE phase -> approve_motion_sample
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {},
                   "partial_progress": {"phase": "motion_sample"}}
st = run._sync({"job_id": "jX"})
assert st["status"] == "awaiting_human" and st["gate"] == "approve_motion_sample"

# assets stage, BUDGET HOLD phase -> budget_exceeded (conditional cost-cap gate)
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {},
                   "partial_progress": {"phase": "budget_hold"}}
st = run._sync({"job_id": "jX"})
assert st["status"] == "awaiting_human" and st["gate"] == "budget_exceeded"

# assets stage, no phase marker -> full media gate approve_assets
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {}}
st = run._sync({"job_id": "jX"})
assert st["status"] == "awaiting_human" and st["gate"] == "approve_assets"

_fake_latest.cp = {"stage": "scene_plan", "status": "awaiting_human", "artifacts": {}}
st = run._sync({"job_id": "jX"})
assert st["status"] == "awaiting_human" and st["gate"] == "approve_scene_plan"

_fake_latest.cp = {"stage": "compose", "status": "completed", "artifacts": {}}
_fake_next.val = None
st = run._sync({"job_id": "jX"})
assert st["status"] == "done" and st["gate"] is None

_fake_latest.cp = {"stage": "assets", "status": "failed", "artifacts": {}, "error": "boom"}
st = run._sync({"job_id": "jX"})
assert st["status"] == "failed" and st["question"] == "boom"
print("[ok] _sync status mapping: awaiting_human / done / failed")

# 4) _approve_stage writes completed + human_approved ----------------------
captured = {}
cp.read_checkpoint = lambda _pd, _jid, stage: {"artifacts": {"asset_manifest": {}}}
def _fake_write(_pd, _jid, stage, status, _arts, **kw):
    captured.update(stage=stage, status=status, approved=kw.get("human_approved"))
cp.write_checkpoint = _fake_write
run._approve_stage("jX", "assets")
assert captured == {"stage": "assets", "status": "completed", "approved": True}, captured
print("[ok] _approve_stage flips checkpoint to completed + human_approved")

# 5) legacy gate on resume -> clear migration message (no agent run) --------
mig = run.resume({"job_id": "jLegacy", "gate": "approve_storyboard", "artifacts": {}},
                 {"decision": "approve"})
assert mig["status"] == "failed" and "start a new job" in mig["question"].lower()
print("[ok] legacy-gate resume returns migration message")

print("\n[PASS] ClaudeCodeRunner adapter: mapping, mirroring, sync, approval, migration")
