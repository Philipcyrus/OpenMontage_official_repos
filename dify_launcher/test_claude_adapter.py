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
assert run._gate_stage("approve_hero_still") == "assets"
assert run._gate_stage("approve_stills") == "assets"
assert run._gate_stage("approve_motion_sample") == "assets"
assert run._gate_stage("budget_exceeded") == "assets"
assert run._gate_stage("approve_assets") == "assets"
assert run._gate_stage("approve_scene_plan") == "scene_plan"
assert run._gate_stage("approve_script") == "script"
assert run._gate_stage("approve_brand") == "brand"
assert "approve_brand" in R.GATES
assert "approve_hero_still" in R.GATES
assert R._STAGE_GATE["compose"] == "approve_final"
assert R._STAGE_GATE["scene_plan"] == "approve_scene_plan"
assert "assets" not in R._STAGE_GATE          # assets is phase-resolved, not a 1:1 map entry
assert R._motion_sample_enabled({}) is False
assert R._motion_sample_enabled({"options": {}}) is False
assert R._motion_sample_enabled({"options": {"motion_sample": True}}) is True
assert R._motion_sample_enabled({"options": {"motion_sample": "true"}}) is True
assert R._motion_sample_enabled({"options": {"motion_sample": False}}) is False

# audio_lipsync defaults ON; explicit false/off restores HOLD-only wording
assert R._audio_lipsync_enabled({}) is True
assert R._audio_lipsync_enabled(None) is True
assert R._audio_lipsync_enabled({"audio_lipsync": True}) is True
assert R._audio_lipsync_enabled({"audio_lipsync": None}) is True
assert R._audio_lipsync_enabled({"audio_lipsync": ""}) is True
assert R._audio_lipsync_enabled({"audio_lipsync": False}) is False
assert R._audio_lipsync_enabled({"audio_lipsync": "false"}) is False
assert R._audio_lipsync_enabled({"audio_lipsync": "off"}) is False
assert "AUDIO LIPSYNC — ON" in R._audio_lipsync_line({})
assert "audio_references" in R._audio_lipsync_line({})
assert "generate_audio:false" in R._audio_lipsync_line({})
assert "seedance_2_0" in R._audio_lipsync_line({})
_off = R._audio_lipsync_line({"audio_lipsync": False})
assert "AUDIO LIPSYNC — OFF" in _off
assert "HOLD" in _off
assert "Do NOT pass" in _off and "audio_references" in _off
_sp_on = run._start_prompt("jLips", "a video about eSIM", {}, "panda-video")
assert "AUDIO LIPSYNC — ON" in _sp_on and "audio_references" in _sp_on
_sp_off = run._start_prompt(
    "jLipsOff", "a video about eSIM", {"audio_lipsync": False}, "panda-video")
assert "AUDIO LIPSYNC — OFF" in _sp_off
assert "HOLD" in _sp_off
_stills_on = run._stills_approved_prompt("jLips", {})
assert "AUDIO LIPSYNC — ON" in _stills_on
_stills_off = run._stills_approved_prompt("jLips", {"audio_lipsync": False})
assert "AUDIO LIPSYNC — OFF" in _stills_off
# Materialize true onto panda-video options when omitted
_st = {"brief": "eSIM ad", "pipeline": "panda-video", "options": {}}
R._apply_language_coerce(_st)
assert _st["options"].get("audio_lipsync") is True
_st_off = {"brief": "eSIM ad", "pipeline": "panda-video",
           "options": {"audio_lipsync": False}}
R._apply_language_coerce(_st_off)
assert _st_off["options"].get("audio_lipsync") is False
print("[ok] audio_lipsync default-on; blank/omit stay on; opt-out restores HOLD-only wording")
assert R._hero_still_enabled({}) is True
assert R._hero_still_enabled({"options": {}}) is True
assert R._hero_still_enabled({"options": {"hero_still": False}}) is False
assert R._hero_still_enabled({"pipeline": "panda-image"}) is False
assert R._hero_scene_index({"scenes": [
    {"id": "scene-1"}, {"id": "scene-2", "hero_moment": True}, {"id": "scene-3"}
]}) == 1
print("[ok] gate<->stage mapping")
print("[ok] motion_sample default off; true opts in")

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

# 2e) rejected takes archived beside the kept still must NOT surface as live stills
JOBR = "job_no_rejected_stills"
projr = run._projects_dir / JOBR
(projr / "assets" / "images").mkdir(parents=True, exist_ok=True)
for _n in ("sc-01.png", "sc-02.png"):
    (projr / "assets" / "images" / _n).write_bytes(b"\x89PNG\r\nLIVE")
for _n in ("rejected_sc-01_take1.png", "rejected_sc-02_take4.png"):
    (projr / "assets" / "images" / _n).write_bytes(b"\x89PNG\r\nTAKE")
arts_r = run._mirror_artifacts(JOBR, {
    "asset_manifest": {
        "version": "1.0",
        "assets": [{"id": "sc-01-still", "path": "assets/images/sc-01.png"},
                   {"id": "sc-02-still", "path": "assets/images/sc-02.png"}],
    },
})
assert arts_r.get("stills") == ["sc-01.png", "sc-02.png"], arts_r.get("stills")
print("[ok] rejected takes stay out of artifacts.stills")

# 2f) storyboard builder drops rejected takes; two scenes zip to two live cards
from dify_launcher.storyboard_preview import cards_from_arts, is_superseded_still, still_basenames
arts_sb = {
    "stills": [
        "rejected_sc1_take1.png",
        "rejected_sc2_take1.png",
        "still_sc1.png",
        "still_sc2.png",
    ],
    "scene_plan": {"version": "1.0", "scenes": [
        {"id": "sc1", "captions": {"zh": "SFO", "en": "SFO"},
         "start_seconds": 0, "end_seconds": 4},
        {"id": "sc2", "captions": {"zh": "SHA", "en": "SHA"},
         "start_seconds": 4, "end_seconds": 8, "hero_moment": True},
    ]},
}
assert still_basenames(arts_sb) == ["still_sc1.png", "still_sc2.png"], still_basenames(arts_sb)
cards = cards_from_arts(arts_sb)
assert len(cards) == 2, [c.get("still") for c in cards]
assert cards[0]["still"] == "still_sc1.png" and cards[0]["label"] == "SC 01"
assert cards[1]["still"] == "still_sc2.png" and cards[1]["label"] == "SC 02"
assert is_superseded_still("rejected_sc1_take1.png")
assert is_superseded_still("assets/images/superseded-stills/rejected_sc2_take1.png")
print("[ok] storyboard drops rejected takes; two scenes zip to two cards")

# 2h) long scene-plan framing essays must not become unwrapped shot chips
essay = "Vertical 9:16. Percentages below are of the FINAL 1080x1920 master frame. " * 8
arts_chip = {
    "stills": ["sc-01.png", "sc-02.png"],
    "scene_plan": {"version": "1.0", "scenes": [
        {"id": "sc-01", "framing": essay, "movement": essay,
         "shot_language": {"shot_size": "wide", "camera_movement": "static"},
         "captions": {"en": "Land in Canada, stay connected on both sides."}},
        {"id": "sc-02", "framing": "medium", "movement": "static",
         "captions": {"en": "CTA"}},
    ]},
}
chips = cards_from_arts(arts_chip)
assert chips[0]["framing"] == "wide" and chips[0]["movement"] == "static", chips[0]
assert chips[1]["framing"] == "medium" and chips[1]["movement"] == "static", chips[1]
assert "Percentages" not in (chips[0]["framing"] or "")
print("[ok] storyboard chips prefer short shot_language over framing essays")

# 2g) GET /jobs stills list and revise-shot paths skip discarded takes
from dify_launcher.app import _public
pub = _public({
    "job_id": "job_f5df699fb729",
    "artifacts": {
        "stills": [
            "rejected_sc1_take1.png",
            "rejected_sc2_take1.png",
            "still_sc1.png",
            "still_sc2.png",
        ],
        "preview": ["storyboard.png"],
    },
})
assert pub["artifacts"]["stills"] == [
    "/jobs/job_f5df699fb729/artifacts/still_sc1.png",
    "/jobs/job_f5df699fb729/artifacts/still_sc2.png",
], pub["artifacts"]["stills"]
assert pub["artifacts"]["preview"] == ["/jobs/job_f5df699fb729/artifacts/storyboard.png"]
rev_paths = R._still_abs_paths(
    "job_f5df699fb729",
    {"artifacts": {"stills": [
        "rejected_sc1_take1.png", "still_sc1.png", "still_sc2.png",
    ]}},
    shots=[1],
)
assert rev_paths and Path(rev_paths[0]).name == "still_sc1.png", rev_paths
print("[ok] GET stills + revise shots skip rejected takes")

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

# assets stage, HERO STILL phase -> approve_hero_still
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {},
                   "partial_progress": {"phase": "hero_still", "hero_scene_id": "scene-2",
                                        "look_notes": ["warmer"]}}
st = run._sync({"job_id": "jX"})
assert st["status"] == "awaiting_human" and st["gate"] == "approve_hero_still"
assert st.get("look_notes") == ["warmer"]
assert st["artifacts"].get("hero_scene_id") == "scene-2"

# nested metadata.stage_phase fallback -> stills
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {
    "asset_manifest": {"version": "1.0", "assets": [],
                       "metadata": {"stage_phase": "stills"}}}}
st = run._sync({"job_id": "jNested"})
assert st["gate"] == "approve_stills"

# completed assets with stills-only media -> recover to approve_stills (not skip storyboard)
JOBREC = "jStillsOnlyRecover"
projrec = run._projects_dir / JOBREC
(projrec / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projrec / "artifacts").mkdir(parents=True, exist_ok=True)
from PIL import Image as _ImageRec
_ImageRec.new("RGB", (40, 40), (1, 2, 3)).save(projrec / "assets" / "images" / "still_0.png")
_fake_latest.cp = {"stage": "assets", "status": "completed", "artifacts": {
    "stills": ["still_0.png"]}}
st = run._sync({"job_id": JOBREC, "pipeline": "panda-video"})
assert st["gate"] == "approve_stills", st

# in_progress assets + stills-only must NOT reopen approve_stills (clips still rendering)
JOBIP = "jInProgressStills"
projip = run._projects_dir / JOBIP
(projip / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projip / "artifacts").mkdir(parents=True, exist_ok=True)
_ImageRec.new("RGB", (40, 40), (4, 5, 6)).save(projip / "assets" / "images" / "scene_1.png")
_fake_latest.cp = {
    "stage": "assets", "status": "in_progress", "artifacts": {},
    "metadata": {"partial_progress": {
        "phase": "motion_and_audio_in_flight",
        "motion_jobs": {"scene_1": {"job_id": "abc"}},
    }},
}
st = run._sync({"job_id": JOBIP, "pipeline": "panda-video"})
assert st["status"] == "running", st
assert st.get("gate") is None, st
assert "approve_stills" != st.get("gate")
assert "in progress" in (st.get("question") or "").lower()
print("[ok] _sync in_progress stills-only → running (not approve_stills)")

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
assert st["status"] == "awaiting_human" and st["gate"] == "approve_brand", st

_fake_latest.cp = {"stage": "assets", "status": "completed", "artifacts": {}}
st = run._sync({"job_id": "jC", "pipeline": "panda-carousel"})
assert st["status"] == "awaiting_human" and st["gate"] == "approve_brand", st

_fake_latest.cp = {"stage": "compose", "status": "completed", "artifacts": {}}
st = run._sync({"job_id": "jX", "brand_resolved": "applied"})
assert st["status"] == "done" and st["gate"] is None, st

_fake_latest.cp = {"stage": "assets", "status": "failed", "artifacts": {}, "error": "boom"}
st = run._sync({"job_id": "jX"})
assert st["status"] == "failed" and st["question"] == "boom"
print("[ok] _sync status mapping: awaiting_human / approve_brand / done / failed")

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

# 3c) hero gate: preview is the single PNG (not storyboard); look_notes carry
JOBH = "jPreviewHero"
projh = run._projects_dir / JOBH
(projh / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projh / "artifacts").mkdir(parents=True, exist_ok=True)
_Image.new("RGB", (80, 120), (200, 10, 10)).save(projh / "assets" / "images" / "hero_scene-2.png")
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human",
                   "partial_progress": {"phase": "hero_still", "hero_scene_id": "scene-2",
                                        "look_notes": ["warmer light"]},
                   "artifacts": {}}
st = run._sync({"job_id": JOBH})
assert st["gate"] == "approve_hero_still"
assert st["artifacts"].get("stills") == ["hero_scene-2.png"]
assert st["artifacts"].get("preview") == ["hero_scene-2.png"]
assert st["artifacts"].get("hero_scene_id") == "scene-2"
assert st.get("look_notes") == ["warmer light"]
assert "HERO" in (st.get("question") or ""), st.get("question")
assert "Approve assets" not in (st.get("question") or "")
print("[ok] _sync hero_still: single PNG preview, not storyboard")

# 3c2) stills-only with NO phase → approve_stills + storyboard (never approve_assets)
JOBSO = "jStillsOnlyNoPhase"
projso = run._projects_dir / JOBSO
(projso / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projso / "artifacts").mkdir(parents=True, exist_ok=True)
_Image.new("RGB", (40, 40), (9, 9, 9)).save(projso / "assets" / "images" / "scene-1.png")
_Image.new("RGB", (40, 40), (8, 8, 8)).save(projso / "assets" / "images" / "scene-2.png")
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {},
                   "pipeline_type": "panda-video"}
st = run._sync({"job_id": JOBSO, "pipeline": "panda-video"})
assert st["gate"] == "approve_stills", st
assert st["artifacts"].get("preview") == ["storyboard.png"], st["artifacts"].get("preview")
assert store.artifact_path(JOBSO, "storyboard.png").is_file()
assert "stills" in (st.get("question") or "").lower()
assert "clips + audio" not in (st.get("question") or "")
print("[ok] _sync stills-only no phase → approve_stills + storyboard")

# 3c3) metadata.partial_progress.phase=stills (agent nest mistake) → approve_stills
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human", "artifacts": {},
                   "metadata": {"partial_progress": {"phase": "stills", "hero_scene_id": "scene-3"}},
                   "pipeline_type": "panda-video"}
st = run._sync({"job_id": JOBSO, "pipeline": "panda-video"})
assert st["gate"] == "approve_stills", st
assert st["artifacts"].get("hero_scene_id") == "scene-3"
print("[ok] _sync metadata.partial_progress.phase fallback")

# 3c4) completed assets + stills-only recover → approve_stills + stills question
JOBREC2 = "jCompletedStillsRecover"
projrec2 = run._projects_dir / JOBREC2
(projrec2 / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projrec2 / "artifacts").mkdir(parents=True, exist_ok=True)
_Image.new("RGB", (40, 40), (3, 3, 3)).save(projrec2 / "assets" / "images" / "still_0.png")
_fake_latest.cp = {"stage": "assets", "status": "completed", "artifacts": {},
                   "pipeline_type": "panda-video"}
# stub write so backfill does not require a real project marker
_bf = []
_real_write = cp.write_checkpoint
def _capture_write(*a, **kw):
    _bf.append(kw.get("partial_progress"))
    try:
        return _real_write(*a, **kw)
    except Exception:
        return None
cp.write_checkpoint = _capture_write
st = run._sync({"job_id": JOBREC2, "pipeline": "panda-video"})
cp.write_checkpoint = _real_write
assert st["gate"] == "approve_stills", st
assert st["artifacts"].get("preview") == ["storyboard.png"]
assert "HERO" not in (st.get("question") or "")
assert "stills" in (st.get("question") or "").lower()
print("[ok] completed stills-only → approve_stills + storyboard question")

# 3d) ordered_still_basenames: hero named hero_scene-3 must not sort before scene-1
from dify_launcher.storyboard_preview import ordered_still_basenames
_ord_arts = {
    "stills": ["hero_scene-3.png", "still_scene-1.png", "still_scene-2.png"],
    "scene_plan": {"version": "1.0", "scenes": [
        {"id": "scene-1"}, {"id": "scene-2"}, {"id": "scene-3", "hero_moment": True},
    ]},
}
assert ordered_still_basenames(_ord_arts) == [
    "still_scene-1.png", "still_scene-2.png", "hero_scene-3.png",
], ordered_still_basenames(_ord_arts)
print("[ok] ordered_still_basenames matches scene_plan order")

# 3e) hero-approved prompt keeps PNG + look ref + look_notes + top-level phase=stills
hap = run._hero_approved_prompt("jH", {"look_notes": ["warmer"], "artifacts": {
    "hero_scene_id": "scene-2"}})
assert "KEEP the approved hero PNG" in hap
assert "style/look" in hap.lower() or "LOOK LOCK" in hap
assert "warmer" in hap
assert "hero_scene_id=scene-2" in hap
assert '"phase":"stills"' in hap
assert "top-level" in hap
phases = run._assets_phases_text(False, hero_still=True)
assert "hero_still" in phases and "PHASE 0" in phases
assert "hero_still" not in run._assets_phases_text(False, hero_still=False)
print("[ok] hero-approved prompt + assets phases text")


# 4) _approve_stage writes completed + human_approved ----------------------
captured = {}
cp.read_checkpoint = lambda _pd, _jid, stage: {"artifacts": {"asset_manifest": {}}}
def _fake_write(_pd, _jid, stage, status, _arts, **kw):
    captured.update(stage=stage, status=status, approved=kw.get("human_approved"))
cp.write_checkpoint = _fake_write
run._approve_stage("jX", "assets")
assert captured == {"stage": "assets", "status": "completed", "approved": True}, captured
print("[ok] _approve_stage flips checkpoint to completed + human_approved")

# 4b) _approve_stage is a no-op for stills-only video (must not skip storyboard)
JOBGUARD = "jApproveGuard"
projg = run._projects_dir / JOBGUARD
(projg / "assets" / "images").mkdir(parents=True, exist_ok=True)
(projg / "artifacts").mkdir(parents=True, exist_ok=True)
_Image.new("RGB", (20, 20), (1, 1, 1)).save(projg / "assets" / "images" / "scene-1.png")
captured.clear()
cp.read_checkpoint = lambda _pd, _jid, stage: {"artifacts": {}}
run._approve_stage(JOBGUARD, "assets", "panda-video")
assert captured == {}, f"must not complete stills-only assets: {captured}"
# carousel stills-terminal may complete
captured.clear()
run._approve_stage(JOBGUARD, "assets", "panda-carousel")
assert captured.get("status") == "completed", captured
print("[ok] _approve_stage refuses stills-only video; carousel still completes")

# 4c) hero resume never calls _approve_stage
_approve_calls = []
_real_approve = run._approve_stage
run._approve_stage = lambda *a, **k: _approve_calls.append((a, k))  # type: ignore[method-assign]
_agent_calls_h = []
run._run_agent = lambda *a, **k: _agent_calls_h.append((a, k))  # type: ignore[method-assign]
_fake_latest.cp = {"stage": "assets", "status": "awaiting_human",
                   "partial_progress": {"phase": "stills"}, "artifacts": {}}
_fake_next.val = "edit"
st_h = run.resume(
    {"job_id": JOBH, "gate": "approve_hero_still", "status": "awaiting_human",
     "pipeline": "panda-video", "artifacts": {"stills": ["hero_scene-2.png"],
                                              "hero_scene_id": "scene-2"},
     "look_notes": []},
    {"decision": "approve"})
run._approve_stage = _real_approve  # type: ignore[method-assign]
assert not _approve_calls, _approve_calls
assert _agent_calls_h and "stills" in str(_agent_calls_h[0]), _agent_calls_h
print("[ok] hero approve resume: no _approve_stage; runs stills leg")

# 4c2) after hero approve, stale hero_still checkpoint triggers continue (not UI loop)
_labels_ah = []
_round_ah = {"n": 0}
_real_sync2 = run._sync
_real_run2 = run._run_agent

def _sync_stale_hero(state):
    _round_ah["n"] += 1
    # first sync after initial stills leg + each continue until n>=3 → stills
    if _round_ah["n"] < 3:
        return {**state, "status": "awaiting_human", "gate": "approve_hero_still",
                "stage": "assets", "question": "Approve this HERO still", "artifacts": {}}
    return {**state, "status": "awaiting_human", "gate": "approve_stills",
            "stage": "assets", "question": "Approve the stills", "artifacts": {}}

run._sync = _sync_stale_hero  # type: ignore[method-assign]
run._run_agent = (lambda prompt, job_id="", label="":
                  _labels_ah.append(label))  # type: ignore[method-assign]
os.environ["CLAUDE_STILLS_AFTER_HERO_MAX"] = "5"
st_ah = run._run_after_hero_approved(
    {"job_id": "jAfterHero", "pipeline": "panda-carousel", "options": {},
     "artifacts": {"hero_scene_id": "slide-2"}, "look_notes": []})
run._sync = _real_sync2  # type: ignore[method-assign]
run._run_agent = _real_run2  # type: ignore[method-assign]
assert st_ah["gate"] == "approve_stills", st_ah
assert "stills" in _labels_ah[0]
assert any("stills_after_hero" in str(x) for x in _labels_ah), _labels_ah
cont = run._stills_after_hero_continue_prompt("jAfterHero", {"pipeline": "panda-carousel"})
assert "ALREADY APPROVED" in cont and "Do NOT ask" in cont
assert "Do NOT stop to ask" in run._hero_approved_prompt("jH", {})
print("[ok] _run_after_hero_approved continues past stale hero_still")

# 4d) _run_until_assets_gate continues while assets stay in_progress
_cont_labels = []
_real_sync = run._sync
_real_run = run._run_agent
_real_ip = run._assets_cp_in_progress
_round = {"n": 0}

def _sync_cont(state):
    _round["n"] += 1
    if _round["n"] < 3:
        return {**state, "status": "running", "gate": None, "stage": "assets",
                "question": "in progress", "artifacts": {}}
    return {**state, "status": "awaiting_human", "gate": "approve_assets", "stage": "assets",
            "question": "Approve the generated media", "artifacts": {"clips": ["c1.mp4"]}}

run._sync = _sync_cont  # type: ignore[method-assign]
run._run_agent = (lambda prompt, job_id="", label="":
                  _cont_labels.append(label))  # type: ignore[method-assign]
run._assets_cp_in_progress = (lambda jid: _round["n"] < 3)  # type: ignore[method-assign]
os.environ["CLAUDE_IN_PROGRESS_MAX"] = "5"
st_c = run._run_until_assets_gate(
    {"job_id": "jCont", "pipeline": "panda-video", "options": {}}, label="assets_media")
run._sync = _real_sync  # type: ignore[method-assign]
run._run_agent = _real_run  # type: ignore[method-assign]
run._assets_cp_in_progress = _real_ip  # type: ignore[method-assign]
assert st_c["gate"] == "approve_assets", st_c
assert st_c["status"] == "awaiting_human"
assert any("continue" in str(x) for x in _cont_labels), _cont_labels
assert "IN PROGRESS" in run._assets_in_progress_prompt("jCont")
print("[ok] _run_until_assets_gate re-invokes continue while in_progress")

# 4e) _run_until_final_gate: stuck running → continue → fail (never hung running)
_fc_labels = []
_real_sync_fc = run._sync
_real_run_fc = run._run_agent
_real_stuck = run._stuck_before_final_gate
_round_fc = {"n": 0}

def _sync_stuck_forever(state):
    # Always "stage assets completed; next: edit" — the hang from job_84e41f0738fc
    return {**state, "status": "running", "gate": None, "stage": "assets",
            "question": "stage assets completed; next: edit", "artifacts": {}}

run._sync = _sync_stuck_forever  # type: ignore[method-assign]
run._run_agent = (lambda prompt, job_id="", label="":
                  _fc_labels.append(label))  # type: ignore[method-assign]
run._stuck_before_final_gate = (lambda st: True)  # type: ignore[method-assign]
os.environ["CLAUDE_EDIT_COMPOSE_MAX"] = "2"
st_fail = run._run_until_final_gate(
    {"job_id": "jEditHang", "pipeline": "panda-video", "options": {}, "artifacts": {}})
run._sync = _real_sync_fc  # type: ignore[method-assign]
run._run_agent = _real_run_fc  # type: ignore[method-assign]
run._stuck_before_final_gate = _real_stuck  # type: ignore[method-assign]
assert st_fail["status"] == "failed", st_fail
assert st_fail.get("gate") is None
assert "approve_final" in (st_fail.get("question") or "")
assert _fc_labels[0] == "edit", _fc_labels
assert any("edit_continue_" in str(x) for x in _fc_labels), _fc_labels
assert len([x for x in _fc_labels if str(x).startswith("edit_continue_")]) == 2
aap = run._assets_approved_prompt("jEditHang", "panda-video")
assert "Do NOT ask" in aap or "Do NOT stop to ask" in aap, aap
assert "extend" in aap.lower() and ("hold" in aap.lower() or "PACING" in aap)
assert "Do NOT ask" in run._edit_compose_continue_prompt("jEditHang", "panda-video")
cont_vid = run._continue_prompt("jEditHang", "panda-video")
assert "ungated" in cont_vid.lower() or "Do NOT" in cont_vid
print("[ok] _run_until_final_gate fails instead of hung running")

# 4f) _run_until_final_gate stops when approve_final appears
_fc2_labels = []
_real_sync_fc2 = run._sync
_real_run_fc2 = run._run_agent
_real_stuck2 = run._stuck_before_final_gate
_round_fc2 = {"n": 0}

def _sync_then_final(state):
    _round_fc2["n"] += 1
    if _round_fc2["n"] < 2:
        return {**state, "status": "running", "gate": None, "stage": "assets",
                "question": "stage assets completed; next: edit", "artifacts": {}}
    return {**state, "status": "awaiting_human", "gate": "approve_final", "stage": "compose",
            "question": "Approve the finished (unbranded) video", "artifacts": {"final": "final.mp4"}}

def _stuck_until_final(st):
    return st.get("status") == "running" and st.get("gate") is None

run._sync = _sync_then_final  # type: ignore[method-assign]
run._run_agent = (lambda prompt, job_id="", label="":
                  _fc2_labels.append(label))  # type: ignore[method-assign]
run._stuck_before_final_gate = _stuck_until_final  # type: ignore[method-assign]
os.environ["CLAUDE_EDIT_COMPOSE_MAX"] = "5"
st_ok = run._run_until_final_gate(
    {"job_id": "jEditOk", "pipeline": "panda-video", "options": {}, "artifacts": {}})
run._sync = _real_sync_fc2  # type: ignore[method-assign]
run._run_agent = _real_run_fc2  # type: ignore[method-assign]
run._stuck_before_final_gate = _real_stuck2  # type: ignore[method-assign]
assert st_ok["status"] == "awaiting_human" and st_ok["gate"] == "approve_final", st_ok
assert _fc2_labels[0] == "edit"
assert any("edit_continue_" in str(x) for x in _fc2_labels), _fc2_labels
print("[ok] _run_until_final_gate stops at approve_final")

# 4g) approve_assets resume routes through _run_until_final_gate (not bare continue)
_final_gate_calls = []
_real_final = run._run_until_final_gate
_real_approve_aa = run._approve_stage
run._approve_stage = lambda *a, **k: None  # type: ignore[method-assign]
run._run_until_final_gate = (lambda st: (_final_gate_calls.append(st) or
    {**st, "status": "awaiting_human", "gate": "approve_final", "stage": "compose"}))  # type: ignore[method-assign]
st_aa = run.resume(
    {"job_id": "jAA", "gate": "approve_assets", "status": "awaiting_human",
     "pipeline": "panda-video", "stage": "assets", "artifacts": {"clips": ["c.mp4"]}},
    {"decision": "approve"})
run._run_until_final_gate = _real_final  # type: ignore[method-assign]
run._approve_stage = _real_approve_aa  # type: ignore[method-assign]
assert _final_gate_calls, "approve_assets must call _run_until_final_gate"
assert st_aa["gate"] == "approve_final"
print("[ok] approve_assets resume uses _run_until_final_gate")

# 5) legacy gate on resume -> clear migration message (no agent run) --------
mig = run.resume({"job_id": "jLegacy", "gate": "approve_storyboard", "artifacts": {}},
                 {"decision": "approve"})
assert mig["status"] == "failed" and "start a new job" in mig["question"].lower()
print("[ok] legacy-gate resume returns migration message")

# 5b) approve_brand is launcher-only — resume must not start Claude
_agent_calls = []
run._run_agent = lambda *a, **k: _agent_calls.append((a, k))  # type: ignore[method-assign]
st_rev = run.resume(
    {"job_id": "jB", "gate": "approve_brand", "status": "awaiting_human", "artifacts": {}},
    {"decision": "revise", "answer": "not yet"})
assert st_rev["gate"] == "approve_brand" and st_rev["status"] == "awaiting_human"
st_skip = run.resume(
    {"job_id": "jB", "gate": "approve_brand", "status": "awaiting_human", "artifacts": {}},
    {"decision": "skip"})
assert st_skip["status"] == "done" and st_skip.get("brand_resolved") == "skipped"
assert st_skip["artifacts"].get("branded") is False
assert not _agent_calls, _agent_calls
print("[ok] approve_brand resume: no agent; revise stays; skip → done")

# 6) carousel/image start prompts are stills-only; video prompt is unchanged --
cv = run._start_prompt("jC", "6-slide carousel", {"aspect_ratio": "4:5"}, "panda-carousel")
assert "STILLS-ONLY" in cv and "NOT a video" in cv
assert "Do NOT generate motion clips" in cv
assert "4:5" in cv
assert "089ddcec-c375-4299-8a65-6d8b757dd81a" in cv
assert "4c01c8f9-6cfb-4d8c-9eb9-74cb61462103" in cv
assert "2D flat" in cv or "2D" in cv
assert "Max 2 paid" in cv
wide = run._start_prompt("jW", "story stills", {"aspect_ratio": "9:16"}, "panda-carousel")
assert "aspect_ratio: 9:16" in wide
assert "NEVER 9:16" not in wide
img = run._start_prompt("jI", "one still", {}, "panda-image")
assert "ONE STILLS-ONLY" in img and "NO script" in img
assert "1:1" in img
assert "NOT a carousel" in img
assert "089ddcec-c375-4299-8a65-6d8b757dd81a" in img
assert "Max 2 paid" in img
vid = run._start_prompt("jV", "a video", {}, "panda-video")
assert "produce a video" in vid
assert "STILLS-ONLY" not in vid
assert "089ddcec-c375-4299-8a65-6d8b757dd81a" in vid
assert "4c01c8f9-6cfb-4d8c-9eb9-74cb61462103" in vid
assert "STILLS 2-TAKE" in vid
assert "2D flat" in vid or "2D MEDIUM" in vid
assert "PHASE 2 (motion sample)" not in vid, "default motion_sample=off must skip sample phase"
assert "PHASE 3 (media)" in vid
assert "TTS-FIRST" in vid
vid_ms = run._start_prompt("jV", "a video", {"motion_sample": True}, "panda-video")
assert "PHASE 2 (motion sample)" in vid_ms
assert "TTS-FIRST" in vid_ms
print("[ok] start prompts: carousel/image stills-only vs video")

# 6b) VOICE CAST — the narration counterpart of CHARACTER LOCK. Parity is three things: the
# LITERAL brand ids sit in the prompt (not an instruction to go look them up), they are present
# on the legs that actually call ElevenLabs (every leg is a cold `claude -p`, so naming them once
# at start does not reach them), and an unresolvable speaker/language BLOCKS instead of silently
# downgrading to a generic preset.
assert R._resolve_voice_id("panda", "en") == "hMSPJ6ja4HIrFHhCGCMl"
assert R._resolve_voice_id("customer", "zh") == "BqljjWyTnrioXPCNkCd4"
assert R._resolve_voice_id("narrator", "zh") == "JZLpE3AGwpKYZI2X65hN"
assert R._resolve_voice_id("robot", "en") is None
assert R._resolve_voice_id("panda", "fr") is None

cast_en = R._resolve_voice_cast("en")
assert cast_en["panda"] == "hMSPJ6ja4HIrFHhCGCMl"
assert cast_en["customer"] == "cgSgspJ2msm6clMCkdW9"
assert cast_en["narrator"] == "8Ln42OXYupYsag45MAUy"

_vopts = {"narrator": "panda", "language": "en"}
vz = run._start_prompt("jV", "a video", _vopts, "panda-video")
assert "VOICE CAST" in vz and "hMSPJ6ja4HIrFHhCGCMl" in vz, "start prompt must carry the cast"
assert "cgSgspJ2msm6clMCkdW9" in vz and "8Ln42OXYupYsag45MAUy" in vz, "all three brand ids"
assert "default speaker=panda" in vz
assert "`voices` matching narrator" not in vz, "must be the id itself, not a lookup instruction"

for _p in (run._stills_approved_prompt("jV", _vopts), run._motion_approved_prompt("jV", _vopts)):
    assert "VOICE CAST" in _p and "hMSPJ6ja4HIrFHhCGCMl" in _p, "media leg must carry the cast"
assert "VOICE CAST" in run._stills_approved_prompt("jV")        # no options must not crash
assert "VOICE CAST" in run._motion_approved_prompt("jV")

blk = run._start_prompt("jV", "a video", {"narrator": "robot", "language": "fr"}, "panda-video")
assert "BLOCKER" in blk, "an unresolvable language must block (missing cast ids)"
assert "may you fall back to Higgsfield" not in blk, "a missing id must NOT offer the fallback"

# Unknown default speaker with valid language still emits the full cast (default falls to panda note)
vl_robot_en = R._voice_line({"narrator": "robot", "language": "en"})
assert "VOICE CAST" in vl_robot_en and "BLOCKER" not in vl_robot_en
assert "hMSPJ6ja4HIrFHhCGCMl" in vl_robot_en

ovr = run._start_prompt("jV", "a video", {"voice_id": "OVERRIDE123"}, "panda-video")
assert "OVERRIDE123" in ovr and "OVERRIDE" in ovr
print("[ok] VOICE CAST: three brand ids in start + media legs; unresolvable language blocks")

# Mandarin brief wins over stale language:en
_zh_brief = ("买哪个套餐才能在中国和美国都能用啊？用 Panda Mobile 就好啦，有 OnePool，"
             "一个流量池，中美通用。限时闪购。")
assert R._brief_looks_mandarin(_zh_brief)
assert not R._brief_looks_mandarin("Panda Mobile eSIM before you fly")
opts_c, coerced = R._coerce_language_from_brief({"language": "en", "narrator": "panda"}, _zh_brief)
assert coerced and opts_c["language"] == "zh"
assert "MI36FIkp9wRP7cpWKPTl" in R._voice_line(opts_c)
assert "BqljjWyTnrioXPCNkCd4" in R._voice_line(opts_c)
assert "JZLpE3AGwpKYZI2X65hN" in R._voice_line(opts_c)
opts_en, coerced_en = R._coerce_language_from_brief(
    {"language": "en", "narrator": "panda"}, "Panda waves at the airport")
assert not coerced_en and opts_en["language"] == "en"
assert "hMSPJ6ja4HIrFHhCGCMl" in R._voice_line(opts_en)
opts_vid, coerced_vid = R._coerce_language_from_brief(
    {"language": "en", "voice_id": "KEEP_ME"}, _zh_brief)
assert not coerced_vid and opts_vid.get("voice_id") == "KEEP_ME"
opts_zh, coerced_zh = R._coerce_language_from_brief({"language": "zh"}, _zh_brief)
assert not coerced_zh and opts_zh["language"] == "zh"
st_coerce = {"brief": _zh_brief, "options": {"language": "en", "narrator": "panda"}}
assert R._apply_language_coerce(st_coerce) is True
assert st_coerce["options"]["language"] == "zh" and st_coerce.get("language_coerced_from_brief")
sp_zh = run._start_prompt("jZh", _zh_brief, st_coerce["options"], "panda-video",
                          language_coerced=True)
assert "language: zh" in sp_zh
assert "MI36FIkp9wRP7cpWKPTl" in sp_zh
assert "Do NOT stop to re-ask language" in sp_zh
assert "stale language:en" in sp_zh
print("[ok] Mandarin brief coerces language:en → zh; VOICE CAST + start prompt note")

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

import subprocess as _subprocess
from types import SimpleNamespace as _SimpleNamespace

_real_subprocess_run = _subprocess.run

# stdin must be DEVNULL (avoids Claude's 3s empty-pipe warning masking real errors)
_stdin_run = R.ClaudeCodeRunner()
_stdin_kwargs = []
def _fake_run_check_stdin(cmd, **kwargs):
    _stdin_kwargs.append(kwargs)
    return _SimpleNamespace(returncode=0, stdout="ok", stderr="")
_subprocess.run = _fake_run_check_stdin
try:
    _stdin_run._run_agent("ping", "job_stdin", "stdin_check")
finally:
    _subprocess.run = _real_subprocess_run
assert _stdin_kwargs and _stdin_kwargs[0].get("stdin") is _subprocess.DEVNULL, _stdin_kwargs
fail_txt = _stdin_run._agent_failure_text(_SimpleNamespace(
    stdout="Failed to authenticate: OAuth session expired and could not be refreshed",
    stderr="Warning: no stdin data received in 3s, proceeding without it. "
           "If piping from a slow command, redirect stdin explicitly: < /dev/null to skip, "
           "or wait longer.\n",
))
assert "OAuth session expired" in fail_txt
assert "no stdin data received" not in fail_txt.lower()
print("[ok] Claude stdin=DEVNULL; auth errors not masked by stdin warning")

print("\n[PASS] ClaudeCodeRunner adapter: mapping, mirroring, sync, approval, migration")
