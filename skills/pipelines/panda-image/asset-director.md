# Asset Director — Panda Image Pipeline

> One still. GATE 2 is **terminal** — approving the still completes the job.
> No motion sample, no clips, no TTS, no music, no edit, no compose.

## When To Use

You have an approved `scene_plan` (exactly one scene, stills-only `required_assets`,
bilingual `captions`). Generate **one still**, record it in `asset_manifest`, then STOP
for human approval. After approval the launcher marks the assets stage `completed` and
the job is `done`.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifact | `scene_plan` | What to generate + on-image copy |
| Style | `styles/panda.yaml` | On-brand look (image prompt prefix, negatives, anchors) |
| Elements | `config/panda-elements.json` | Panda/customer Element ids |
| Tools | `image_selector` (Higgsfield MCP `generate_image`) | Stills only |

## Process

### 1. Inventory
There is one scene. Its `required_assets` entry of type `image` is the still task
(`scene_id`, description, Element ids, captions, aspect ratio from
`scene_plan.metadata.aspect_ratio` — default `1:1`).

### 2. Generate ONE STILL, then STOP (GATE 2, approve_stills)
- On-brand (`styles/panda.yaml`) and character-consistent (panda master Element from
  `config/panda-elements.json`). Never invent a new panda.
- **Aspect ratio:** use `scene_plan.metadata.aspect_ratio` / the job's `options.aspect_ratio`
  (default `1:1`). Pass it to `generate_image`. Do not rewrite to `4:5`/`1:1` only.
- **A still that reads:** bake the **primary-language** headline/body from
  `scene.captions` into the still. Layout for a paused, readable image, not a cinematic
  frame that will be animated later.
- Follow `skills/meta/higgsfield-mcp-bridge.md`: `get_cost:true` before spending; honor
  `max_higgsfield_credits` (budget_hold + STOP if the still would exceed the cap).

Generate **NO video and NO audio.** Then:
1. Write a schema-valid `asset_manifest` **now**. The still: `id`, `type: "image"`,
   `path`, `source_tool`, `scene_id`, plus `credits` / `credits_source: "actual"` /
   `provider: "higgsfield"` from `get_cost`.
2. Write the assets checkpoint `status='awaiting_human'` **and
   `partial_progress={"phase": "stills"}`** and STOP (end your turn).

The launcher surfaces this as `approve_stills`. On approval it completes the stage and
the job is done — do **not** continue to motion, TTS, edit, or compose.

On "request revision", honor `mode` (`fresh` | `edit`; infer if omitted — see
`skills/meta/higgsfield-mcp-bridge.md`):

- **fresh:** `generate_image` from text + Element IDs only. Do not pass the old PNG.
- **edit:** load the still from disk, `media_import` it (not `media_import_url`), then
  `generate_image` with that `media_id` and a preservation prompt (keep composition /
  character / layout / type; apply only the note). Same aspect ratio. If the model
  rejects the source still, surface a blocker — do not silently switch to fresh.

Replace the still file + its `asset_manifest` row. Re-checkpoint with **top-level**
`partial_progress={"phase":"stills"}` (not nested under `metadata`).

### 3. Character consistency
Always pass the panda master Element id; use the customer Element for the customer.

### 4. Do not brand here
The still stays **UGC** (no wordmark overlay). Branding is `POST /jobs/{id}/brand` after
the job is `done` — a PIL stamp of the BGC wordmark onto a copy. Keep the clean original.

## Success criteria
- One still on disk, listed in `asset_manifest` with `path` + `scene_id` + credits
- Social ratio honored; copy readable; panda on-model
- Checkpoint left in `awaiting_human` with `partial_progress.phase="stills"`
- No video, audio, edit_decisions, or render produced
