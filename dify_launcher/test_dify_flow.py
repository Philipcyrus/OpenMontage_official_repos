"""Plays the role of Dify against the launcher, in-process (no server, no LLM).

Runs the full gate handshake with the MockRunner and asserts each transition — the
best-of-both shape (upstream text plan + a Panda stills cost gate):
    start -> GATE 1 (script) -> GATE 2 (scene_plan, TEXT) -> GATE 3 (stills, NO video)
          -> GATE 3.5 (motion sample, ONE clip) -> GATE 4 (assets, all media)
          -> GATE 5 (final) -> GATE 6 (approve_brand) -> done
Also proves the motion_sample=false toggle skips the motion gate (stills -> assets directly).

Proves the Dify-facing contract, local storage, checkpoint/resume, that scene_plan produces
NO media (text only), that the STILLS gate produces stills with NO video on disk (the cost
gate), that clips + a schema-valid asset_manifest appear only at the assets gate, and that a
REAL clean video is produced by the folded render.
Run:  python dify_launcher/test_dify_flow.py
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

os.environ.setdefault("DIFY_RUNNER", "mock")
_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from PIL import Image
from fastapi.testclient import TestClient

from dify_launcher.app import app
from dify_launcher import store
from schemas.artifacts import validate_artifact

c = TestClient(app)


def _digest(job_id: str, name: str) -> str:
    return hashlib.md5(store.artifact_path(job_id, Path(name).name).read_bytes()).hexdigest()


def _size(job_id: str, name: str) -> tuple[int, int]:
    with Image.open(store.artifact_path(job_id, Path(name).name)) as im:
        return im.size


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

# script is inline JSON; markdown preview is a fetchable file URL
sc_obj = b["artifacts"].get("script")
assert isinstance(sc_obj, dict) and sc_obj.get("sections"), "script gate must return inline JSON"
validate_artifact("script", sc_obj)
assert sc_obj["sections"][0]["text"]
assert b["artifacts"].get("script_md") == f"/jobs/{job}/artifacts/script.md"
assert b["artifacts"].get("preview") == [f"/jobs/{job}/artifacts/script.md"]
sc = c.get(b["artifacts"]["script_md"])
assert sc.status_code == 200 and "Open on the Panda mascot." in sc.text, "script.md not served"
print("   script preview:", sc.text.splitlines()[0])

# 2) approve script -> GATE 2 (approve_scene_plan): a TEXT plan, NO media -----------------
b = _step("respond approve (script)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_scene_plan", want_status="awaiting_human")
sp = b["artifacts"].get("scene_plan")
# (e) Dify receives TEXT at the scene_plan gate: an inline structured plan
assert isinstance(sp, dict) and sp.get("scenes"), "scene_plan gate must return a text scene list"
validate_artifact("scene_plan", sp)                            # (a/d) schema-valid scene_plan
assert b["artifacts"].get("scene_plan_md") == f"/jobs/{job}/artifacts/scene_plan.md"
assert b["artifacts"].get("preview") == [f"/jobs/{job}/artifacts/scene_plan.md"], \
    "scene_plan preview must be THIS gate's .md only"
sp_md = c.get(b["artifacts"]["scene_plan_md"])
assert sp_md.status_code == 200 and sp["scenes"][0]["id"] in sp_md.text
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
assert all("storyboard" not in Path(s).name.lower() for s in stills)
assert "preview" not in b["artifacts"], "stills gate must not carry a combined contact sheet preview"
assert "storyboard_html" not in b["artifacts"], "contact sheet removed: no storyboard_html"
assert not list(store.artifacts_dir(job).glob("storyboard*")), "no storyboard files should be written"
# COST GATE: no clips, no manifest, and NO video files exist on disk yet
assert "clips" not in b["artifacts"], "stills gate must NOT carry clips"
assert "asset_manifest" not in b["artifacts"], "stills gate must NOT carry an asset_manifest"
vids = list(store.artifacts_dir(job).glob("*.mp4"))
assert not vids, f"stills gate must generate NO video, found: {[p.name for p in vids]}"
print(f"   stills gate: {len(stills)} stills, NO video on disk (cost gate holds)")

# dual-mode: edit shot 3 keeps 1080x1920 and does not drop other stills
assert len(stills) == 3
assert all(_size(job, s) == (1080, 1920) for s in stills)
d0, d1, d2 = [_digest(job, s) for s in stills]
b = _step("respond revise stills mode=edit shot 3", c.post(f"/jobs/{job}/respond", json={
    "decision": "revise", "mode": "edit", "shots": [3],
    "answer": "remove the peeking pandas; keep the OnePool diagram",
}), want_gate="approve_stills", want_status="awaiting_human")
stills_e = b["artifacts"].get("stills")
assert stills_e and len(stills_e) == 3, "edit must not drop other stills"
assert [_digest(job, s) for s in stills_e[:2]] == [d0, d1], "unflagged stills must be unchanged"
assert _digest(job, stills_e[2]) != d2, "flagged still must be rewritten"
assert all(_size(job, s) == (1080, 1920) for s in stills_e), "edit must keep 9:16"
assert "preview" not in b["artifacts"], "no combined preview at the stills gate"
print("   stills edit shot 3: 1080x1920 kept, other stills untouched")

# revise the stills (still no video generated)
b = _step("respond revise (stills)", c.post(f"/jobs/{job}/respond",
          json={"decision": "revise", "answer": "make the panda brighter"}),
          want_gate="approve_stills", want_status="awaiting_human")
assert not list(store.artifacts_dir(job).glob("*.mp4")), "revising stills must not generate video"

# 4) approve stills -> GATE 3.5 (approve_motion_sample): ONE sample clip, NO full batch ----
b = _step("respond approve (stills)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_motion_sample", want_status="awaiting_human")
assert b["artifacts"].get("motion_sample"), "motion-sample gate must carry a sample clip"
assert "preview" not in b["artifacts"], "motion-sample gate must not carry a preview"
assert "clips" not in b["artifacts"], "motion-sample gate must NOT carry the full clip batch"
assert "asset_manifest" not in b["artifacts"], "motion-sample gate must NOT carry a manifest"
vids = list(store.artifacts_dir(job).glob("*.mp4"))
assert len(vids) == 1, f"motion-sample gate should have exactly ONE clip on disk, got {[p.name for p in vids]}"
print(f"   motion-sample gate: 1 sample clip ({b['artifacts']['motion_sample']}), full batch NOT generated")

# revise the motion sample -> stays at the motion-sample gate
b = _step("respond revise (motion_sample)", c.post(f"/jobs/{job}/respond",
          json={"decision": "revise", "answer": "slower push-in, less shake"}),
          want_gate="approve_motion_sample", want_status="awaiting_human")
assert "clips" not in b["artifacts"], "revising the motion sample must not batch the full clips"

# 4b) approve motion sample -> GATE 4 (approve_assets): clips + audio + manifest ----------
b = _step("respond approve (motion_sample)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
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

# 4c) approve assets -> production assembles -> GATE 5 (approve_final)
b = _step("respond approve (assets)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_final", want_status="awaiting_human")
assert b["artifacts"].get("final") == f"/jobs/{job}/artifacts/final.mp4"
assert b["artifacts"].get("branded") is False, "final should be UNBRANDED"

# final video is fetchable + real
fv = c.get(f"/jobs/{job}/artifacts/final.mp4")
assert fv.status_code == 200 and len(fv.content) > 1000, "final.mp4 not served / empty"
print("   final.mp4 bytes:", store.artifact_path(job, "final.mp4").stat().st_size)

# 4) approve final -> approve_brand (not done); approve branding stamps copies
b = _step("respond approve (final)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_gate="approve_brand", want_status="awaiting_human")
assert b["artifacts"].get("final") == f"/jobs/{job}/artifacts/final.mp4"
assert b["artifacts"].get("branded") is False
br_too_early = c.post(f"/jobs/{job}/brand", json={"profile": "bgc"})
assert br_too_early.status_code == 409, br_too_early.text
d_final = _digest(job, "final.mp4")
b = _step("respond approve (brand)", c.post(f"/jobs/{job}/respond", json={"decision": "approve"}),
          want_status="done")
assert b["artifacts"].get("branded") is True
assert b["artifacts"].get("branded_final") == f"/jobs/{job}/artifacts/final.bgc.mp4"
assert _digest(job, "final.mp4") == d_final, "UGC master must be left untouched"
bfv = c.get(f"/jobs/{job}/artifacts/final.bgc.mp4")
assert bfv.status_code == 200 and len(bfv.content) > 1000, "final.bgc.mp4 not served / empty"
assert _digest(job, "final.bgc.mp4") != d_final, "branded master must differ from UGC"
vstills = b["artifacts"].get("branded_stills")
assert vstills and len(vstills) == 3, vstills
assert all(u.endswith(".bgc.png") for u in vstills)
brv2 = c.post(f"/jobs/{job}/brand", json={"profile": "bgc"})
assert brv2.status_code == 200
assert brv2.json()["artifacts"].get("branded_final") == b["artifacts"]["branded_final"]
print("   video brand gate: approve stamps final.bgc.mp4, UGC final.mp4 kept, stills stamped")

# 4d) motion_sample=false toggle: approving stills goes STRAIGHT to approve_assets ---------
b2 = _step("POST /jobs (motion_sample off)", c.post("/jobs", json={
    "brief": "quick draft: panda waves", "options": {"motion_sample": False}}),
    want_gate="approve_script", want_status="awaiting_human")
job2 = b2["job_id"]
c.post(f"/jobs/{job2}/respond", json={"decision": "approve"})            # -> scene_plan
c.post(f"/jobs/{job2}/respond", json={"decision": "approve"})            # -> stills
b2 = _step("respond approve (stills, sample off)", c.post(f"/jobs/{job2}/respond", json={"decision": "approve"}),
           want_gate="approve_assets", want_status="awaiting_human")
assert b2["artifacts"].get("clips"), "with motion_sample off, approving stills must produce the full clips"
assert "motion_sample" not in b2["artifacts"], "motion_sample off must not create a sample clip"
print("   motion_sample=false: stills -> assets directly (no motion gate)")

# 4e) BUDGET HARD CAP: a low max_higgsfield_credits blocks the batch (budget_exceeded); raise to proceed
b3 = _step("POST /jobs (budget cap 20)", c.post("/jobs", json={
    "brief": "capped run", "options": {"motion_sample": False, "max_higgsfield_credits": 20}}),
    want_gate="approve_script", want_status="awaiting_human")
job3 = b3["job_id"]
c.post(f"/jobs/{job3}/respond", json={"decision": "approve"})            # -> scene_plan
c.post(f"/jobs/{job3}/respond", json={"decision": "approve"})            # -> stills
# approving stills would animate the full batch (~54 credits) > cap 20 -> HARD BLOCK, nothing generated
b3 = _step("respond approve (stills, over budget)", c.post(f"/jobs/{job3}/respond", json={"decision": "approve"}),
           want_gate="budget_exceeded", want_status="awaiting_human")
assert "clips" not in b3["artifacts"], "budget hold must NOT generate clips"
assert not list(store.artifacts_dir(job3).glob("clip_*.mp4")), "budget hold must generate no clip files"
assert "cap of 20" in (b3.get("question") or ""), "budget hold should state the approved cap"
print("   budget hold: batch blocked at cap 20, zero clips generated (hard pre-generation block)")
# raise the cap -> generation proceeds to approve_assets, credits recorded
b3 = _step("respond raise cap -> 100", c.post(f"/jobs/{job3}/respond",
           json={"decision": "approve", "max_higgsfield_credits": 100}),
           want_gate="approve_assets", want_status="awaiting_human")
assert b3["artifacts"].get("clips"), "raising the cap must let generation proceed"
assert any(a.get("credits") for a in b3["artifacts"]["asset_manifest"]["assets"]), "manifest records credits"
print("   cap raised to 100 -> clips generated, Higgsfield credits recorded in manifest")

# cancel path: a capped job cancelled at the budget gate -> failed, no further spend
jc = c.post("/jobs", json={"brief": "cancel me", "options": {"motion_sample": False, "max_higgsfield_credits": 5}}).json()["job_id"]
c.post(f"/jobs/{jc}/respond", json={"decision": "approve"})              # -> scene_plan
c.post(f"/jobs/{jc}/respond", json={"decision": "approve"})              # -> stills
c.post(f"/jobs/{jc}/respond", json={"decision": "approve"})              # -> budget_exceeded
bc = _step("respond cancel (budget)", c.post(f"/jobs/{jc}/respond", json={"decision": "cancel"}),
           want_status="failed")
assert "cancel" in (bc.get("question") or "").lower(), "cancel should report a cancellation"
print("   budget cancel -> job failed cleanly, no clips")

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

# 6) panda-carousel: truncated sibling — script → scene_plan → stills → done
b4 = _step("POST /jobs (panda-carousel)", c.post("/jobs", json={
    "brief": "6-slide IG carousel: eSIM before you fly",
    "pipeline": "panda-carousel",
    "options": {"aspect_ratio": "4:5", "language": "zh"},
}), want_gate="approve_script", want_status="awaiting_human")
assert b4.get("pipeline") == "panda-carousel"
job4 = b4["job_id"]
assert isinstance(b4["artifacts"].get("script"), dict)
assert b4["artifacts"].get("preview") == [f"/jobs/{job4}/artifacts/script.md"]
c.post(f"/jobs/{job4}/respond", json={"decision": "approve"})            # -> scene_plan
b4 = _step("carousel scene_plan", c.get(f"/jobs/{job4}"),
           want_gate="approve_scene_plan", want_status="awaiting_human")
sp4 = b4["artifacts"].get("scene_plan")
assert isinstance(sp4, dict) and sp4.get("scenes")
validate_artifact("scene_plan", sp4)
assert b4["artifacts"].get("preview") == [f"/jobs/{job4}/artifacts/scene_plan.md"]
assert all(s.get("captions", {}).get("zh") and s.get("captions", {}).get("en")
           for s in sp4["scenes"]), "carousel scene_plan must carry bilingual captions"
assert "stills" not in b4["artifacts"]
c.post(f"/jobs/{job4}/respond", json={"decision": "approve"})            # -> stills
b4 = _step("carousel stills", c.get(f"/jobs/{job4}"),
           want_gate="approve_stills", want_status="awaiting_human")
assert b4["artifacts"].get("stills") and len(b4["artifacts"]["stills"]) == 3
assert "preview" not in b4["artifacts"], "carousel stills gate must not carry a contact sheet"
assert "clips" not in b4["artifacts"] and "final" not in b4["artifacts"]
assert not list(store.artifacts_dir(job4).glob("*.mp4")), "carousel must not generate video"
cstills = b4["artifacts"]["stills"]
assert all(_size(job4, s) == (1080, 1350) for s in cstills), "carousel default is 4:5"
cd0, cd1, cd2 = [_digest(job4, s) for s in cstills]
b4 = _step("carousel stills revise mode=edit shot 3", c.post(f"/jobs/{job4}/respond", json={
    "decision": "revise", "mode": "edit", "shots": [3],
    "answer": "remove the peeking pandas; keep the OnePool diagram",
}), want_gate="approve_stills", want_status="awaiting_human")
cstills2 = b4["artifacts"].get("stills")
assert cstills2 and len(cstills2) == 3, "carousel edit must not drop other stills"
assert [_digest(job4, s) for s in cstills2[:2]] == [cd0, cd1]
assert _digest(job4, cstills2[2]) != cd2
assert all(_size(job4, s) == (1080, 1350) for s in cstills2), "carousel edit must keep 4:5"
assert "preview" not in b4["artifacts"], "carousel stills gate must not carry a contact sheet"
print("   carousel stills edit shot 3: 1080x1350 kept, other stills untouched")
# skip is only valid at approve_brand
bad_skip = c.post(f"/jobs/{job4}/respond", json={"decision": "skip"})
assert bad_skip.status_code == 400, bad_skip.text
print("   skip at approve_stills -> 400")
# approve stills -> approve_brand (not done)
b4 = _step("carousel approve stills -> brand",
           c.post(f"/jobs/{job4}/respond", json={"decision": "approve"}),
           want_gate="approve_brand", want_status="awaiting_human")
assert b4["artifacts"].get("stills")
assert isinstance(b4["artifacts"].get("asset_manifest"), dict)
validate_artifact("asset_manifest", b4["artifacts"]["asset_manifest"])
assert "clips" not in b4["artifacts"] and "final" not in b4["artifacts"]
assert b4["artifacts"].get("branded") is False
print("   carousel: script → scene_plan → stills → approve_brand (no clips/final)")

# skip branding -> done UGC, no branded_stills
b4 = _step("carousel skip brand -> done",
           c.post(f"/jobs/{job4}/respond", json={"decision": "skip"}),
           want_status="done")
assert b4.get("gate") is None
assert not b4["artifacts"].get("branded_stills")
assert b4["artifacts"].get("branded") is False
print("   carousel skip brand: done UGC, no branded_stills")

# POST /brand still works after skip (done job)
br = c.post(f"/jobs/{job4}/brand", json={"profile": "bgc"})
assert br.status_code == 200, br.text
bb = br.json()
assert bb["status"] == "done"
assert bb["artifacts"].get("branded") is True
bstills = bb["artifacts"].get("branded_stills")
assert bstills and len(bstills) == 3, bstills
assert all(u.endswith(".bgc.png") for u in bstills)
assert bb["artifacts"].get("stills")
br2 = c.post(f"/jobs/{job4}/brand", json={"profile": "bgc"})
assert br2.status_code == 200 and br2.json()["artifacts"].get("branded_stills") == bstills
print("   carousel /brand after skip: BGC copies, UGC originals kept, idempotent")

# brand before done -> 409
early = c.post("/jobs", json={"brief": "too early", "pipeline": "panda-carousel"}).json()["job_id"]
bad = c.post(f"/jobs/{early}/brand", json={"profile": "bgc"})
assert bad.status_code == 409, bad.text
print("   brand before done -> 409")

# gate-collapse: options.gates omits script -> first gate is scene_plan
b5 = _step("POST /jobs (carousel, skip script gate)", c.post("/jobs", json={
    "brief": "carousel skip script",
    "pipeline": "panda-carousel",
    "options": {"gates": ["scene_plan", "stills"]},
}), want_gate="approve_scene_plan", want_status="awaiting_human")
assert b5.get("pipeline") == "panda-carousel"
print("   carousel gates collapse: skipped approve_script")

# 1:1 is caller-set, not rewritten to 4:5
b6 = _step("POST /jobs (carousel 1:1)", c.post("/jobs", json={
    "brief": "square carousel",
    "pipeline": "panda-carousel",
    "options": {"aspect_ratio": "1:1", "gates": ["scene_plan", "stills"]},
}), want_gate="approve_scene_plan", want_status="awaiting_human")
c.post(f"/jobs/{b6['job_id']}/respond", json={"decision": "approve"})
b6 = _step("carousel 1:1 stills", c.get(f"/jobs/{b6['job_id']}"),
           want_gate="approve_stills", want_status="awaiting_human")
sq = b6["artifacts"].get("stills") or []
assert sq and all(_size(b6["job_id"], s) == (1080, 1080) for s in sq), "1:1 must be 1080x1080"
print("   carousel 1:1: 1080x1080 stills")

# 7) panda-image: scene_plan → one still → edit → fresh → done + /brand
b7 = _step("POST /jobs (panda-image)", c.post("/jobs", json={
    "brief": "One square still: panda at the airport holding a phone with full signal.",
    "pipeline": "panda-image",
    "options": {"language": "zh"},
}), want_gate="approve_scene_plan", want_status="awaiting_human")
assert b7.get("pipeline") == "panda-image"
job7 = b7["job_id"]
sp7 = b7["artifacts"].get("scene_plan")
assert isinstance(sp7, dict) and len(sp7.get("scenes") or []) == 1, "image plan is exactly 1 scene"
validate_artifact("scene_plan", sp7)
assert b7["artifacts"].get("preview") == [f"/jobs/{job7}/artifacts/scene_plan.md"]
assert "script" not in b7["artifacts"]
assert "stills" not in b7["artifacts"]
c.post(f"/jobs/{job7}/respond", json={"decision": "approve"})
b7 = _step("image stills", c.get(f"/jobs/{job7}"),
           want_gate="approve_stills", want_status="awaiting_human")
istills = b7["artifacts"].get("stills") or []
assert len(istills) == 1, istills
assert _size(job7, istills[0]) == (1080, 1080), "image default is 1:1"
assert "preview" not in b7["artifacts"], "image stills gate must not carry a contact sheet"
assert not list(store.artifacts_dir(job7).glob("*.mp4")), "image must not generate video"
id0 = _digest(job7, istills[0])
b7 = _step("image stills revise mode=edit", c.post(f"/jobs/{job7}/respond", json={
    "decision": "revise", "mode": "edit", "shots": [1],
    "answer": "warm sunset; keep layout",
}), want_gate="approve_stills", want_status="awaiting_human")
istills_e = b7["artifacts"].get("stills") or []
assert len(istills_e) == 1
assert _digest(job7, istills_e[0]) != id0
assert _size(job7, istills_e[0]) == (1080, 1080)
assert "preview" not in b7["artifacts"], "image stills gate must not carry a contact sheet"
id1 = _digest(job7, istills_e[0])
b7 = _step("image stills revise mode=fresh", c.post(f"/jobs/{job7}/respond", json={
    "decision": "revise", "mode": "fresh", "shots": [1],
    "answer": "new composition; do not reuse the PNG",
}), want_gate="approve_stills", want_status="awaiting_human")
istills_f = b7["artifacts"].get("stills") or []
assert len(istills_f) == 1
assert _digest(job7, istills_f[0]) != id1
assert _size(job7, istills_f[0]) == (1080, 1080)
b7 = _step("image approve stills -> brand",
           c.post(f"/jobs/{job7}/respond", json={"decision": "approve"}),
           want_gate="approve_brand", want_status="awaiting_human")
assert len(b7["artifacts"].get("stills") or []) == 1
validate_artifact("asset_manifest", b7["artifacts"]["asset_manifest"])
assert "clips" not in b7["artifacts"] and "final" not in b7["artifacts"]
b7 = _step("image revise brand stays",
           c.post(f"/jobs/{job7}/respond", json={"decision": "revise", "answer": "not yet"}),
           want_gate="approve_brand", want_status="awaiting_human")
assert b7["artifacts"].get("branded") is False
id_ugc = _digest(job7, Path(b7["artifacts"]["stills"][0]).name)
b7 = _step("image approve brand -> done",
           c.post(f"/jobs/{job7}/respond", json={"decision": "approve"}),
           want_status="done")
assert b7.get("gate") is None
assert b7["artifacts"].get("branded") is True
ibstills = b7["artifacts"].get("branded_stills")
assert ibstills and len(ibstills) == 1 and ibstills[0].endswith(".bgc.png")
assert b7["artifacts"].get("stills")
assert _digest(job7, Path(b7["artifacts"]["stills"][0]).name) == id_ugc
print("   image: scene_plan → 1 still → edit → fresh → approve_brand (revise stays, approve stamps)")

print("\n[PASS] FULL DIFY GATE FLOW: start -> script -> scene_plan(text) -> stills(no video) -> "
      "motion_sample(1 clip) -> assets(media) -> final -> approve_brand -> done  (+ motion_sample=false toggle)")
print("   + panda-carousel: script → scene_plan → stills → approve_brand (skip) → done")
print("   + panda-image: scene_plan → one still → edit → fresh → approve_brand (revise, approve) → done")
print("   job dir:", store.job_dir(job))
