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
assert arts.get("scene_plan_md") == "scene_plan.md"
assert store.artifact_path(JOB, "scene_plan.md").is_file()
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
assert a.get("script_md") == "script.md"
assert "Meet Panda" in store.artifact_path(JOB2, "script.md").read_text(encoding="utf-8")
assert "preview" not in a, "preview is gate-specific; mirroring does not know the gate"
(proj2 / "artifacts" / "script.json").write_text(_json.dumps(_script_obj), encoding="utf-8")
b = run._mirror_artifacts(JOB2, {})                                      # (b) on-disk script.json
assert isinstance(b.get("script"), dict) and b["script"]["title"] == "Panda tip", b.get("script")
assert b.get("script_md") == "script.md"
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
assert d.get("scene_plan_md") == "scene_plan.md"
assert store.artifact_path(JOB3, "scene_plan.md").is_file()
print("[ok] stray cost_report.md not mislabeled as script at a scriptless gate")

# 2d) superseded revise leftovers must NOT surface as live stills (history/ or *.pre-*)
JOBS = "job_no_superseded_stills"
projs = run._projects_dir / JOBS
(projs / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projs / "history" / "superseded-stills").mkdir(parents=True, exist_ok=True)
(projs / "assets" / "images" / "slide-1.png").write_bytes(b"\x89PNG\r\nLIVE")
(projs / "history" / "superseded-stills" / "slide-1.pre-sunset-aff1e9c3.png").write_bytes(b"\x89PNG\r\nOLD")
arts_s = run._mirror_artifacts(JOBS, {
    "asset_manifest": {
        "version": "1.0",
        "assets": [{"id": "slide-1-still", "path": "assets/images/slide-1.png"}],
        "metadata": {"revisions": [{
            "slide": "slide-1", "mode": "edit",
            "superseded_still": "history/superseded-stills/slide-1.pre-sunset-aff1e9c3.png",
        }]},
    },
})
assert arts_s.get("stills") == ["slide-1.png"], arts_s.get("stills")
print("[ok] superseded stills stay out of artifacts.stills")

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

# 3b) _sync sets gate-specific preview URLs without replacing inline JSON
_fake_latest.cp = {"stage": "script", "status": "awaiting_human",
                   "artifacts": {"script": _script_obj}}
st = run._sync({"job_id": "jPreviewScript"})
assert st["gate"] == "approve_script"
assert isinstance(st["artifacts"].get("script"), dict)
assert st["artifacts"].get("script_md") == "script.md"
assert st["artifacts"].get("preview") == ["script.md"]
_fake_latest.cp = {"stage": "scene_plan", "status": "awaiting_human",
                   "artifacts": {"scene_plan": {"version": "1.0",
                                                "scenes": [{"id": "s1", "description": "Airport"}]}}}
st = run._sync({"job_id": "jPreviewPlan"})
assert st["gate"] == "approve_scene_plan"
assert isinstance(st["artifacts"].get("scene_plan"), dict)
assert st["artifacts"].get("preview") == ["scene_plan.md"]
assert st["artifacts"]["preview"] != ["script.md"]
print("[ok] _sync dual-surface: inline JSON + gate-specific preview .md")
# 3b) _sync at stills writes storyboard.png into preview, not into stills
from PIL import Image as _Image
JOBSB = "jPreviewStills"
projsb = run._projects_dir / JOBSB
(projsb / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projsb / "artifacts").mkdir(parents=True, exist_ok=True)
_Image.new("RGB", (80, 120), (11, 11, 11)).save(projsb / "assets" / "images" / "still_0.png")
_Image.new("RGB", (80, 120), (253, 197, 13)).save(projsb / "assets" / "images" / "still_1.png")
(projsb / "assets" / "images" / "storyboard.png").write_bytes(b"\x89PNG\r\nNOTBOARD")
_sp = {"version": "1.0", "scenes": [
    {"id": "sc1", "description": "Wave", "start_seconds": 0, "end_seconds": 3,
     "framing": "medium", "movement": "static"},
    {"id": "sc2", "description": "CTA", "start_seconds": 3, "end_seconds": 6,
     "framing": "close", "movement": "static"},
]}
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human",
                   "partial_progress": {"phase": "stills"},
                   "artifacts": {"scene_plan": _sp}}
st = run._sync({"job_id": JOBSB})
assert st["gate"] == "approve_stills"
assert st["artifacts"].get("preview") == ["storyboard.png"]
assert "storyboard.png" not in (st["artifacts"].get("stills") or [])
assert store.artifact_path(JOBSB, "storyboard.png").is_file()
assert store.artifact_path(JOBSB, "storyboard.html").is_file()
html_sb = store.artifact_path(JOBSB, "storyboard.html").read_text(encoding="utf-8")
assert "SC 01" in html_sb and "Wave" in html_sb
print("[ok] _sync stills dual-surface: storyboard.png preview, not listed as a still")


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

# 6) carousel/image start prompts are stills-only; video prompt is unchanged --
cv = run._start_prompt("jC", "6-slide carousel", {"aspect_ratio": "4:5"}, "panda-carousel")
assert "STILLS-ONLY" in cv and "NOT a video" in cv
assert "Do NOT generate motion clips" in cv
assert "4:5" in cv
wide = run._start_prompt("jW", "story stills", {"aspect_ratio": "9:16"}, "panda-carousel")
assert "aspect_ratio: 9:16" in wide
assert "NEVER 9:16" not in wide
img = run._start_prompt("jI", "one still", {}, "panda-image")
assert "ONE STILLS-ONLY" in img and "NO script" in img
assert "1:1" in img
assert "NOT a carousel" in img
vid = run._start_prompt("jV", "a video", {}, "panda-video")
assert "produce a video" in vid
assert "STILLS-ONLY" not in vid
print("[ok] start prompts: carousel/image stills-only vs video")

# 7) _pipeline_of / gate-collapse helpers -----------------------------------
assert R._pipeline_of({}) == "panda-video"
assert R._pipeline_of({"pipeline": "panda-carousel"}) == "panda-carousel"
assert R._pipeline_of({"pipeline": "panda-image"}) == "panda-image"
assert R._is_carousel({"pipeline": "panda-carousel"})
assert not R._is_carousel({"pipeline": "panda-video"})
assert R._is_image({"pipeline": "panda-image"})
assert not R._is_image({"pipeline": "panda-carousel"})
assert R._is_stills_terminal({"pipeline": "panda-carousel"})
assert R._is_stills_terminal({"pipeline": "panda-image"})
assert not R._is_stills_terminal({"pipeline": "panda-video"})
assert R._script_gate_enabled({})
assert R._script_gate_enabled({"options": {}})
assert not R._script_gate_enabled({"options": {"gates": ["scene_plan", "stills"]}})
print("[ok] pipeline + gates helpers")

assert R._carousel_aspect({}) == "4:5"
assert R._carousel_aspect({"aspect_ratio": "9:16"}) == "9:16"
assert R._stills_aspect({}, pipeline="panda-image") == "1:1"
assert R._stills_aspect({"aspect_ratio": "9:16"}, pipeline="panda-image") == "9:16"
assert R._carousel_pixel_size("1:1") == (1080, 1080)
assert R._carousel_pixel_size("4:5") == (1080, 1350)
assert R._carousel_pixel_size("9:16") == (1080, 1920)
assert R._carousel_pixel_size("1080x1080") == (1080, 1080)
print("[ok] stills aspect helpers")

# 8) stills revise prompt: EDIT vs FRESH + still path; infer-if-omitted ------
assert R._stills_revise_mode({"mode": "edit", "answer": "redo everything"}) == "edit"
assert R._stills_revise_mode({"mode": "fresh", "shots": [1], "answer": "remove the panda"}) == "fresh"
assert R._stills_revise_mode({"shots": [3], "answer": "remove the peeking pandas"}) == "edit"
assert R._stills_revise_mode({"answer": "make the panda brighter"}) == "fresh"
assert R._stills_revise_mode({"answer": "regenerate from scratch"}) == "fresh"
print("[ok] stills revise mode infer")

JOBR = "job_revise_stills"
p_flagged = store.artifact_path(JOBR, "still_02.png")
p_flagged.parent.mkdir(parents=True, exist_ok=True)
p_flagged.write_bytes(b"\x89PNG\r\n")
st_rev = {"job_id": JOBR, "gate": "approve_stills",
          "artifacts": {"stills": ["still_00.png", "still_01.png", "still_02.png"]}}
pe = run._revise_prompt(
    JOBR, "assets (STILLS phase — revise the flagged stills)",
    {"decision": "revise", "mode": "edit", "shots": [3],
     "answer": "remove the peeking pandas"},
    state=st_rev)
assert "MODE=EDIT" in pe, pe
assert "MODE=FRESH" not in pe, pe
assert str(p_flagged.resolve()) in pe or "still_02.png" in pe
assert "media_import" in pe
pf = run._revise_prompt(
    JOBR, "assets (STILLS phase — revise the flagged stills)",
    {"decision": "revise", "mode": "fresh", "shots": [1],
     "answer": "different composition, panda on the right"},
    state=st_rev)
assert "MODE=FRESH" in pf, pf
assert "MODE=EDIT" not in pf, pf
assert "Do NOT pass the old PNG" in pf
p_other = run._revise_prompt("jS", "script", {"answer": "shorter"},
                             state={"gate": "approve_script"})
assert "MODE=" not in p_other
print("[ok] stills revise prompt: EDIT vs FRESH + still path")

print("\n[PASS] ClaudeCodeRunner adapter: mapping, mirroring, sync, approval, migration")
