# Asset Director — Panda Video Pipeline

> Best-of-both: upstream's single asset-generation stage (everything recorded in
> `asset_manifest`), PLUS Panda **cost gates**. Human-reviewed phases — optional
> HERO STILL look-lock, then STILLS, then optional motion sample, then full media.

## When To Use

You have an approved `scene_plan` (with `required_assets` per scene) and the approved `script`.
Your job is to generate all media — stills, motion clips, narration, music — honoring Panda
brand + character consistency, recording everything in `asset_manifest`. Phases:
- **PHASE 0 (GATE 2.5 — approve_hero_still):** when the job option `hero_still` is on
  (**default on**; pass `false` to opt out), generate ONLY ONE hero still, then STOP.
- **PHASE 1 (GATE 3 — approve_stills):** generate remaining stills under LOOK LOCK from the
  approved hero (or all stills when `hero_still` is off), then STOP. No video yet.
- **PHASE 2 (GATE 3.5 — approve_motion_sample):** when the job option `motion_sample` is on
  (default **off**; pass `true` to opt in), animate ONE hero still into a single sample clip so
  the motion/animation is approved before the full batch, then STOP. Skipped when `motion_sample`
  is off (the default).
- **PHASE 3 (GATE 4 — approve_assets):** after the motion sample is approved (or straight after
  the stills when `motion_sample` is off), animate the remaining stills + add audio, then STOP.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifacts | `scene_plan`, `script` | What to generate + narration text |
| Style | `styles/panda.yaml` | On-brand look (image prompt prefix, negatives, anchors) |
| Elements | `config/panda-elements.json` | Panda/customer Element ids + narration voice ids |
| Tools | `image_selector`, `higgsfield_mcp_video`, `seedance_video`, `elevenlabs_tts`, `music_gen` | Generation |

## Process

### 1. Inventory required assets
Walk every scene in `scene_plan`. For each `required_assets` entry create an asset task
(`scene_id`, `type`, `description`, `source`, tool). This is the full generation worklist.
Expect **one image** `required_asset` per scene that needs a still — no base-plate + restack
chain as separate generates.

### 2. PHASE 0 — generate ONE HERO STILL, then STOP (GATE 2.5, approve_hero_still)
**Only when the `hero_still` job option is on (default on; pass `false` to opt out).**
Generate ONE still for the `hero_moment` scene (else scene 1). CHARACTER LOCK + 2D MEDIUM +
STILLS 2-TAKE on that still only. Write the assets checkpoint `status='awaiting_human'` with
**top-level** `partial_progress={"phase":"hero_still","hero_scene_id":"<id>","look_notes":[]}`
(not nested under `asset_manifest.metadata`) and STOP. Preview is the single PNG (not the
storyboard grid). On revise: update the one still + append the note to `look_notes`; re-checkpoint
with `phase:"hero_still"` and STOP. Do **not** generate remaining stills or video yet.

### 3. PHASE 1 — generate remaining STILLS under LOOK LOCK, then STOP (GATE 3, approve_stills)
After the hero is approved (or immediately when `hero_still` is off):
- When coming from an approved hero: **KEEP** the approved hero PNG. Generate only the other
  scenes. Import the hero as a **style/look** reference (not a start-frame that copies composition).
  Bake accumulated `look_notes` into every remaining prompt.
- When `hero_still` is off: generate one still per scene as before.
- Per remaining scene: same CHARACTER LOCK + 2D MEDIUM + STILLS 2-TAKE.

**Generate NO video and NO audio yet.** Then write the assets checkpoint with
`status='awaiting_human'` **and top-level `partial_progress={"phase": "stills"}`**
(not nested under `asset_manifest.metadata`) and STOP.
The launcher surfaces this as the **approve_stills** gate (storyboard grid preview).
Do **not** mark the assets stage `completed` at this point — that skips the storyboard gate.

On "request revision" at this gate, honor `mode` (`fresh` | `edit`). Each flagged shot gets a
**new** 2-take budget; still honor LOOK LOCK from the approved hero. Replace only flagged
files + `asset_manifest` rows. Re-checkpoint with **top-level**
`partial_progress={"phase":"stills"}`. Do **not** proceed to video until the stills are approved.

### 4. PHASE 2 — MOTION SAMPLE (one hero clip), then STOP (GATE 3.5, approve_motion_sample)
**Only when the `motion_sample` job option is on (default off; pass `true` to opt in).** After the stills are approved,
animate ONE representative **hero** still (the most important scene, else scene 1) into a **single**
sample clip via the Higgsfield MCP bridge (`higgsfield_mcp_video`, image_to_video). The i2v
prompt must **hold the 2D still** and the same locked characters — do not ask Kling to invent
a new person or make the clip 3D / photoreal. This is the
motion cost gate: the reviewer approves the motion/animation feel (camera, movement, how the panda
moves) **before** committing to the whole batch. Record the sample's Higgsfield **credits** on that
asset (`credits`, `credits_source: "actual"`). Then write the assets checkpoint
`status='awaiting_human'` **AND `partial_progress={"phase": "motion_sample"}`** and STOP. Generate
**no other clips and no audio yet.**

On "request revision" here, regenerate ONLY the sample clip per the feedback (adjust motion prompt /
model / motion params), keep `partial_progress.phase="motion_sample"`, and STOP again. Do not batch
the rest until the motion is approved (max ~3 sample iterations, then escalate).

> If `motion_sample` is off, skip this phase entirely — go straight from approved stills to PHASE 3.

### 5. PHASE 3 — animate remaining stills + audio, then STOP (GATE 4, approve_assets)
After the motion sample is approved (or straight after the stills when `motion_sample` is off):
- **Motion clips**: animate the remaining approved stills into clips via the Higgsfield MCP bridge
  (`higgsfield_mcp_video`, image_to_video), reusing the **same motion approach** (model + motion
  params) as the approved sample — see `skills/meta/higgsfield-mcp-bridge.md`. i2v from the
  on-Element still; hold 2D medium and the locked customer/panda — do not invent a new person
  or make the clip 3D / photoreal.
- **Narration**: `elevenlabs_tts` per script section using the **exact `voice_id` named in
  the VOICE LOCK line of this leg's prompt**. The launcher resolves it from
  `config/panda-elements.json` `voices` by narrator+language and puts the literal id in the
  prompt — you do not look it up, and you do not choose. Narration generated with any other
  voice is a defect; do not ship it.
- **Music** (if requested): `music_gen` (ElevenLabs Music), kept under the VO.
Then write the assets checkpoint `status='awaiting_human'` **without** any phase marker and STOP.
The launcher surfaces this as the **approve_assets** gate. On "request revision" here, regenerate
only the flagged shots (`response.shots`).

### 5. Character and voice consistency
The panda mascot must look identical across every still/clip. Always attach the panda master
Element id from `config/panda-elements.json` in the **media slot**; use the customer Element
for the customer. Never invent a new panda or human. See CHARACTER LOCK in
`skills/meta/higgsfield-mcp-bridge.md`.

**VOICE LOCK — the same rule for narration.** A voice is chosen exactly the way a face is: from
`config/panda-elements.json`, never improvised. The launcher resolves `voices[narrator][language]`
and interpolates the **literal `voice_id`** into every prompt whose leg can call ElevenLabs, so
the id is in front of you at the moment you generate — you never look it up and never pick one.

- Use that id verbatim. Narration in any other voice is a defect, exactly as a still without the
  Element attached is a defect.
- If the prompt says **VOICE LOCK — BLOCKER**, that narrator/language pair has no configured
  voice. Do **not** improvise, do **not** borrow a neighbouring language, and do **not** fall back
  to Higgsfield `seed_audio` to get past it — that swaps the brand voice for a generic preset and
  nobody hears it until the assets gate. Generate everything else, then stop at the gate and name
  the unconfigured pair in the question.
- The one permitted fallback is ElevenLabs **itself** being unavailable — an infrastructure
  failure, never a missing id — and it must be recorded in `decision_log`.

### 6. Build the asset_manifest (in PHASE 3)
Record EVERY generated file canonically: per asset `id`, `type` (`image|video|audio|narration|
music|...`), `path` (relative to the project dir), `source_tool`, `scene_id` (bind each asset to
its scene), plus optional `prompt`/`model`/`cost_usd`/`duration_seconds`. Persist a schema-valid
`asset_manifest` (`version: "1.0"`) as part of the PHASE 2 checkpoint. On approval the stage
completes and the pipeline proceeds to edit/compose.

**Record Higgsfield credits (for the per-project cost report).** For every Higgsfield-generated
asset (stills via `generate_image`, clips via image_to_video), you already run the `get_cost:true`
preflight before spending (see `skills/meta/higgsfield-mcp-bridge.md`). Write that credit number
into the asset's manifest entry as **`credits`** (the number), **`credits_source: "actual"`**, and
**`provider: "higgsfield"`**. This is the ONLY place real credits are captured — do not skip it.
If you ever generate without a get_cost value, still set `credits` to your best estimate and mark
`credits_source: "estimated"`. (ElevenLabs voice/music usage is captured automatically by the
tools — you do not need to record it.)

## Handoff to `edit` / `compose`
`compose` assembles the approved assets into a CLEAN (unbranded) master via `panda_render`.
Branding is a separate, on-demand `panda_brand` step applied only after final approval.

## Success criteria
- Every required asset exists on disk and appears in `asset_manifest` with `path` + `scene_id`
- Stills/clips on-brand and character-consistent (panda Elements attached as media)
- Narration covers all script sections; music (if any) sits under the VO
- Checkpoint left in `awaiting_human` for the gate
