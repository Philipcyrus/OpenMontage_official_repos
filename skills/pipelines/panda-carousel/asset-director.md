# Asset Director — Panda Carousel Pipeline

> Stills only. Approving the full stills set is **terminal** — the job completes.
> Optional **hero look-lock** gate first (default on). No motion sample, clips, TTS,
> music, edit, or compose.

## When To Use

You have an approved `scene_plan` (one scene per slide, stills-only `required_assets`,
bilingual `captions`) and the approved `script`. Generate stills (hero look-lock first when
enabled), record them in `asset_manifest`, then STOP for human approval of the full set.
After stills approval the launcher marks the assets stage `completed` and the job is `done`.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifacts | `scene_plan`, `script` | What to generate + on-slide copy |
| Style | `styles/panda.yaml` | On-brand look (image prompt prefix, negatives, anchors) |
| Elements | `config/panda-elements.json` | Panda/customer Element ids |
| Tools | `image_selector` (Higgsfield MCP `generate_image`) | Stills only |

## Process

### 1. Inventory
Walk every scene. Each `required_assets` entry of type `image` is one still task
(`scene_id`, description, Element ids, captions, aspect ratio from
`scene_plan.metadata.aspect_ratio` — default `4:5`).

### 2. PHASE 1 — ONE HERO STILL, then STOP (GATE 2.5, approve_hero_still)
**Only when the `hero_still` job option is on (default on; pass `false` to opt out).**

Pick the hero slide: scene with `hero_moment: true`, else slide 1. Generate ONLY that still.
**Follow** `skills/meta/higgsfield-mcp-bridge.md`: **CHARACTER LOCK**, **STILLS 2-TAKE**,
**2D MEDIUM LOCK**. Checkpoint `status='awaiting_human'` AND top-level
`partial_progress={"phase":"hero_still","hero_scene_id":"…","look_notes":[]}` and STOP.

On revise: `fresh` | `edit` on that one still; append `answer` to `look_notes`; keep
`phase:"hero_still"`. Do **not** generate other slides until the look is approved.

### 3. PHASE 2 — remaining STILLS under LOOK LOCK, then STOP (GATE 3, approve_stills)
After the hero is approved (or immediately when `hero_still` is off), generate remaining
slides (or all slides if look-lock was skipped):

- Keep the approved hero PNG.
- **LOOK LOCK:** `media_import` the hero as a style reference (not a composition start-frame);
  match palette / character / lighting / medium / wardrobe; bake `look_notes`; per-slide
  action/framing from `scene_plan`.
- Attach Element IDs; max 2 paid `generate_image` per slide; take 2 = i2i of take 1.
- **Aspect ratio:** `scene_plan.metadata.aspect_ratio` / job `options.aspect_ratio` (default `4:5`).
- **Slides that read:** bake primary-language copy from `scene.captions`.
- Honor `max_higgsfield_credits` (budget_hold + STOP if the batch would exceed).

Generate **NO video and NO audio.** Then:
1. Write a schema-valid `asset_manifest` **now** (this pipeline has no later media phase).
   Every still: `id`, `type: "image"`, `path`, `source_tool`, `scene_id`, plus
   `credits` / `credits_source: "actual"` / `provider: "higgsfield"` from `get_cost`.
2. Write the assets checkpoint `status='awaiting_human'` **and
   `partial_progress={"phase": "stills"}`** and STOP (end your turn).

The launcher surfaces this as `approve_stills`. On approval it completes the stage and
the job is done — do **not** continue to motion, TTS, edit, or compose.

On "request revision", honor `mode` (`fresh` | `edit`; infer if omitted — see
`skills/meta/higgsfield-mcp-bridge.md`). Each flagged slide gets a **new** 2-take budget;
still honor LOOK LOCK from the approved hero:

- **fresh:** `generate_image` from text + Element IDs only (media slot). Do not pass the old PNG.
- **edit:** load the flagged slide from disk, `media_import` it (not
  `media_import_url`), then `generate_image` with that `media_id` and a
  preservation prompt. Same aspect ratio. If the model rejects the source
  still, surface a blocker — do not silently switch to fresh.

Revise only the flagged slides (`response.shots`). Replace those files + their
`asset_manifest` rows; leave other slides untouched. Re-checkpoint with
**top-level** `partial_progress={"phase":"stills"}` (not nested under `metadata`).

### 4. Character consistency
Always attach the panda master Element id in the media slot; use the customer Element
for the customer. See CHARACTER LOCK in `skills/meta/higgsfield-mcp-bridge.md`.

### 5. Do not brand here
Stills stay **UGC** (no wordmark overlay). Branding is `POST /jobs/{id}/brand` after the
job is `done` — a PIL stamp of the BGC wordmark onto copies. Keep the clean originals.

## Success criteria
- One still per slide on disk, listed in `asset_manifest` with `path` + `scene_id` + credits
- Hero look-lock honored when `hero_still` is on
- Social ratio honored; copy readable; panda on-model
- Checkpoint left in `awaiting_human` with the correct phase (`hero_still` or `stills`)
- No video, audio, edit_decisions, or render produced
