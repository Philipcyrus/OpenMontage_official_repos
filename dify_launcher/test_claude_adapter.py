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
assert run._gate_stage("approve_clips") == "assets"
assert run._gate_stage("approve_script") == "script"
assert R._STAGE_GATE["compose"] == "approve_final"
assert R._STAGE_GATE["scene_plan"] == "approve_storyboard"
print("[ok] gate<->stage mapping")

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

arts = run._mirror_artifacts(JOB, {
    "script": "artifacts/script.md",
    "stills": ["assets/images/still_0.png", "assets/images/still_1.png"],
    "clips": ["assets/video/clip_0.mp4", "assets/video/clip_1.mp4", "assets/video/clip_2.mp4"],
    "final": "renders/final.mp4",
})
assert arts["script"] == "script.md"
assert len(arts["stills"]) == 2 and len(arts["clips"]) == 3
assert arts["final"] == "final.mp4" and arts["branded"] is False
assert store.artifact_path(JOB, "final.mp4").is_file()       # actually copied into the store
assert store.artifact_path(JOB, "clip_2.mp4").is_file()
print("[ok] artifact mirroring + grouping (stills/clips/final/script)")

# 3) _sync: checkpoint status -> launcher state (stub the reads) -----------
def _fake_latest(_pd, _jid):
    return _fake_latest.cp
def _fake_next(_pd, _jid, _pt=None):
    return _fake_next.val
cp.get_latest_checkpoint = _fake_latest
cp.get_next_stage = _fake_next

_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {}}
st = run._sync({"job_id": "jX"})
assert st["status"] == "awaiting_human" and st["gate"] == "approve_clips"

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

print("\n[PASS] ClaudeCodeRunner adapter: mapping, mirroring, sync, approval")
