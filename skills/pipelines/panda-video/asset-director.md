# Asset Director — Panda Video Pipeline

> Best-of-both: upstream's single asset-generation stage (everything recorded in
> `asset_manifest`), PLUS a Panda **cost gate**. This stage runs in **TWO human-reviewed
> phases** — STILLS FIRST (approve the look before any expensive video), then the full media.

## When To Use

You have an approved `scene_plan` (with `required_assets` per scene) and the approved `script`.
Your job is to generate all media — stills, motion clips, narration, music — honoring Panda
brand + character consistency, recording everything in `asset_manifest`. You do it in **two
phases with a human gate at each**:
- **PHASE 1 (GATE 3 — approve_stills):** generate ONLY the stills, then STOP. No video yet.
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

### 2. PHASE 1 — generate STILLS ONLY, then STOP (GATE 3, approve_stills)
Generate ONE keyframe still per scene that needs an image — on-brand (`styles/panda.yaml`) and
character-consistent. **Follow the binding rules in `skills/meta/higgsfield-mcp-bridge.md`:**
**CHARACTER LOCK**, **STILLS 2-TAKE HARD RULE**, and **2D MEDIUM LOCK**. Summary:

- Attach `customer_reference_element_id` / `panda_reference_element_id` from
  `config/panda-elements.json` in the MCP media / `image_references` slot whenever that
  role appears. Never invent a new human or panda. Never put Element UUIDs in the prompt
  sentence. Log the IDs used on each `asset_manifest` row.
- Max **2 paid `generate_image` calls per scene** this round. Take 1 = shipped still
  (both characters in one T2I if needed). If unusable, take 2 = **i2i of take 1**
  (one change), never a fresh T2I. Then STOP and gate — ship take 2 if it exists, else
  take 1. Flag remaining defects in the gate question; do not generate a third time.
- Default medium is **2D flat** per `styles/panda.yaml`. Do not mix 3D human + 2D panda.
- Archive a rejected take 1 as `rejected_*` if take 2 ships.

**Generate NO video and NO audio yet.** Then write the
assets checkpoint with `status='awaiting_human'` **and `partial_progress={"phase": "stills"}`**
and STOP (end your turn). The launcher surfaces this as the **approve_stills** gate.
Optionally also write a **contact sheet** of the stills as a **review aid only** (never a scene
still, never in `scene_plan`).

> Why stills-first: image generation is cheap; image→video is expensive. Approving the look
> (on-model panda, composition) here prevents wasted video spend on a bad still.

On "request revision" at this gate, honor `mode` (`fresh` | `edit`; infer if
omitted — see `skills/meta/higgsfield-mcp-bridge.md`). Each flagged shot gets a **new**
2-take budget under the same hard rule:

- **fresh:** `generate_image` from text + Element IDs only (media slot). Do not pass the old PNG.
- **edit:** load the flagged still from disk, `media_import` it (not
  `media_import_url` — localhost artifact URLs are not fetchable), then
  `generate_image` with that `media_id` and a preservation prompt (keep
  composition / character / layout / type; apply only the note). Same aspect
  ratio. If the model rejects the source still, surface a blocker — do not
  silently switch to fresh.

Regenerate only the flagged scenes (`shots`). Replace those files + their
`asset_manifest` rows; leave other slides untouched. Re-checkpoint with
**top-level** `partial_progress={"phase":"stills"}` (not nested under
`metadata`). Do **not** proceed to video until the stills are approved.

### 3. PHASE 2 — MOTION SAMPLE (one hero clip), then STOP (GATE 3.5, approve_motion_sample)
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

### 4. PHASE 3 — animate remaining stills + audio, then STOP (GATE 4, approve_assets)
After the motion sample is approved (or straight after the stills when `motion_sample` is off):
- **Motion clips**: animate the remaining approved stills into clips via the Higgsfield MCP bridge
  (`higgsfield_mcp_video`, image_to_video), reusing the **same motion approach** (model + motion
  params) as the approved sample — see `skills/meta/higgsfield-mcp-bridge.md`. i2v from the
  on-Element still; hold 2D medium and the locked customer/panda — do not invent a new person
  or make the clip 3D / photoreal.
- **Narration**: `elevenlabs_tts` per script section with the resolved voice id.
- **Music** (if requested): `music_gen` (ElevenLabs Music), kept under the VO.
Then write the assets checkpoint `status='awaiting_human'` **without** any phase marker and STOP.
The launcher surfaces this as the **approve_assets** gate. On "request revision" here, regenerate
only the flagged shots (`response.shots`).

### 5. Character consistency
The panda mascot must look identical across every still/clip. Always attach the panda master
Element id from `config/panda-elements.json` in the **media slot**; use the customer Element
for the customer. Never invent a new panda or human. See CHARACTER LOCK in
`skills/meta/higgsfield-mcp-bridge.md`.

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
