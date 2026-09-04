# Asset Director — Panda Carousel Pipeline

> Stills only. GATE 3 is **terminal** — approving the stills completes the job.
> No motion sample, no clips, no TTS, no music, no edit, no compose.

## When To Use

You have an approved `scene_plan` (one scene per slide, stills-only `required_assets`,
bilingual `captions`) and the approved `script`. Generate **one still per slide**, record
them in `asset_manifest`, then STOP for human approval. After approval the launcher
marks the assets stage `completed` and the job is `done`.

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

### 2. Generate STILLS ONLY, then STOP (GATE 3, approve_stills)
Generate ONE still per slide. **Follow the binding rules in
`skills/meta/higgsfield-mcp-bridge.md`:** **CHARACTER LOCK**, **STILLS 2-TAKE HARD RULE**,
and **2D MEDIUM LOCK**. Summary:

- Attach `customer_reference_element_id` / `panda_reference_element_id` from
  `config/panda-elements.json` in the MCP media / `image_references` slot whenever that
  role appears. Never invent a new human or panda. Never put Element UUIDs in the prompt
  sentence. Log the IDs used on each `asset_manifest` row.
- Max **2 paid `generate_image` calls per slide** this round. Take 1 = shipped still.
  If unusable, take 2 = **i2i of take 1** (one change), never a fresh T2I. Then STOP
  and gate — ship take 2 if it exists, else take 1. Flag remaining defects in the gate
  question; do not generate a third time.
- Default medium is **2D flat** per `styles/panda.yaml`.
- Archive a rejected take 1 as `rejected_*` if take 2 ships.

Also:
- On-brand (`styles/panda.yaml`) and character-consistent. Never invent a new panda.
- **Aspect ratio:** use `scene_plan.metadata.aspect_ratio` / the job's `options.aspect_ratio`
  (default `4:5`). Pass it to `generate_image`. Do not rewrite to `4:5`/`1:1` only.
- **Slides that read:** bake the **primary-language** headline/body from
  `scene.captions` into the still. Layout for a paused, readable slide (hook / content /
  CTA), not a cinematic frame that will be animated later.
- Follow `skills/meta/higgsfield-mcp-bridge.md`: `get_cost:true` before spending; honor
  `max_higgsfield_credits` (budget_hold + STOP if the batch would exceed the cap).

Generate **NO video and NO audio.** Then:
1. Write a schema-valid `asset_manifest` **now** (this pipeline has no later media phase).
   Every still: `id`, `type: "image"`, `path`, `source_tool`, `scene_id`, plus
   `credits` / `credits_source: "actual"` / `provider: "higgsfield"` from `get_cost`.
2. Write the assets checkpoint `status='awaiting_human'` **and
   `partial_progress={"phase": "stills"}`** and STOP (end your turn).

The launcher surfaces this as `approve_stills`. On approval it completes the stage and
the job is done — do **not** continue to motion, TTS, edit, or compose.

On "request revision", honor `mode` (`fresh` | `edit`; infer if omitted — see
`skills/meta/higgsfield-mcp-bridge.md`). Each flagged slide gets a **new** 2-take budget:

- **fresh:** `generate_image` from text + Element IDs only (media slot). Do not pass the old PNG.
- **edit:** load the flagged slide from disk, `media_import` it (not
  `media_import_url`), then `generate_image` with that `media_id` and a
  preservation prompt. Same aspect ratio. If the model rejects the source
  still, surface a blocker — do not silently switch to fresh.

Revise only the flagged slides (`response.shots`). Replace those files + their
`asset_manifest` rows; leave other slides untouched. Re-checkpoint with
**top-level** `partial_progress={"phase":"stills"}` (not nested under `metadata`).

### 3. Character consistency
Always attach the panda master Element id in the media slot; use the customer Element
for the customer. See CHARACTER LOCK in `skills/meta/higgsfield-mcp-bridge.md`.

### 4. Do not brand here
Stills stay **UGC** (no wordmark overlay). Branding is `POST /jobs/{id}/brand` after the
job is `done` — a PIL stamp of the BGC wordmark onto copies. Keep the clean originals.

## Success criteria
- One still per slide on disk, listed in `asset_manifest` with `path` + `scene_id` + credits
- Social ratio honored; copy readable; panda on-model
- Checkpoint left in `awaiting_human` with `partial_progress.phase="stills"`
- No video, audio, edit_decisions, or render produced
