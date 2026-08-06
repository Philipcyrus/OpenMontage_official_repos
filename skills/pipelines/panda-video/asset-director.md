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
- **PHASE 2 (GATE 4 — approve_assets):** after stills are approved, animate them + add audio,
  then STOP.

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

### 3. PHASE 2 — animate approved stills + audio, then STOP (GATE 4, approve_assets)
Only AFTER the stills are approved:
- **Motion clips**: animate each approved still into a clip via the Higgsfield MCP bridge
  (`higgsfield_mcp_video`, image_to_video) — see `skills/meta/higgsfield-mcp-bridge.md`.
- **Narration**: `elevenlabs_tts` per script section with the resolved voice id.
- **Music** (if requested): `music_gen` (ElevenLabs Music), kept under the VO.
Then write the assets checkpoint `status='awaiting_human'` **without** the `stills` phase marker
and STOP. The launcher surfaces this as the **approve_assets** gate. On "request revision" here,
regenerate only the flagged shots (`response.shots`).

### 4. Character consistency
The panda mascot must look identical across every still/clip. Always pass the panda master
Element id from `config/panda-elements.json`; use the customer Element for the customer. Never
invent a new panda.

### 5. Build the asset_manifest (in PHASE 2)
Record EVERY generated file canonically: per asset `id`, `type` (`image|video|audio|narration|
music|...`), `path` (relative to the project dir), `source_tool`, `scene_id` (bind each asset to
its scene), plus optional `prompt`/`model`/`cost_usd`/`duration_seconds`. Persist a schema-valid
`asset_manifest` (`version: "1.0"`) as part of the PHASE 2 checkpoint. On approval the stage
completes and the pipeline proceeds to edit/compose.

## Handoff to `edit` / `compose`
`compose` assembles the approved assets into a CLEAN (unbranded) master via `panda_render`.
Branding is a separate, on-demand `panda_brand` step applied only after final approval.

## Success criteria
- Every required asset exists on disk and appears in `asset_manifest` with `path` + `scene_id`
- Stills/clips on-brand and character-consistent (panda Elements)
- Narration covers all script sections; music (if any) sits under the VO
- Checkpoint left in `awaiting_human` for the gate
