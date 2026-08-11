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
  (default), animate ONE hero still into a single sample clip so the motion/animation is approved
  before the full batch, then STOP. Skipped when `motion_sample` is off.
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

### 2. PHASE 1 — generate STILLS ONLY, then STOP (GATE 3, approve_stills)
Generate ONE keyframe still per scene that needs an image — on-brand (`styles/panda.yaml`) and
character-consistent (reuse the panda master Element from `config/panda-elements.json` so the
mascot is identical across scenes). **Generate NO video and NO audio yet.** Then write the
assets checkpoint with `status='awaiting_human'` **and `partial_progress={"phase": "stills"}`**
and STOP (end your turn). The launcher surfaces this as the **approve_stills** gate.
Optionally also write a **contact sheet** of the stills as a **review aid only** (never a scene
still, never in `scene_plan`).

> Why stills-first: image generation is cheap; image→video is expensive. Approving the look
> (on-model panda, composition) here prevents wasted video spend on a bad still.

On "request revision" at this gate, regenerate only the flagged scenes and re-checkpoint (keep
`partial_progress.phase="stills"`). Do **not** proceed to video until the stills are approved.

### 3. PHASE 2 — MOTION SAMPLE (one hero clip), then STOP (GATE 3.5, approve_motion_sample)
**Only when the `motion_sample` job option is on (default).** After the stills are approved,
animate ONE representative **hero** still (the most important scene, else scene 1) into a **single**
sample clip via the Higgsfield MCP bridge (`higgsfield_mcp_video`, image_to_video). This is the
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
  params) as the approved sample — see `skills/meta/higgsfield-mcp-bridge.md`.
- **Narration**: `elevenlabs_tts` per script section with the resolved voice id.
- **Music** (if requested): `music_gen` (ElevenLabs Music), kept under the VO.
Then write the assets checkpoint `status='awaiting_human'` **without** any phase marker and STOP.
The launcher surfaces this as the **approve_assets** gate. On "request revision" here, regenerate
only the flagged shots (`response.shots`).

### 5. Character consistency
The panda mascot must look identical across every still/clip. Always pass the panda master
Element id from `config/panda-elements.json`; use the customer Element for the customer. Never
invent a new panda.

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
- Stills/clips on-brand and character-consistent (panda Elements)
- Narration covers all script sections; music (if any) sits under the VO
- Checkpoint left in `awaiting_human` for the gate
