# Scene-Plan Director — Panda Image Pipeline

> Structured **text** plan only — no media. **Exactly one scene** = one still. GATE 1.

## When To Use

You are turning the consolidated `brief` into a **structured scene plan** with **exactly
one scene**. You write the schema-valid `scene_plan` artifact, then **STOP for human
approval**. You do **not** generate the still here. The scene *declares* a still via
`required_assets`. There is no script artifact in this pipeline.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/scene_plan.schema.json` | Artifact validation (text only; optional `captions`) |
| Prior artifact | `brief` | Concept, copy, composition intent |
| Style | `styles/panda.yaml` | On-brand look — captured as PLAN requirements |
| Elements | `config/panda-elements.json` | Panda/customer Element ids |

## Process

### 1. Exactly one scene
Write **one** scene. Set `id` (`slide-1`), `type` (`generated` for an illustrated still;
`text_card` only when the image is typography-led with no mascot), `description`, dummy
`start_seconds`/`end_seconds` (0 / 1). Do **not** add extra scenes.

### 2. A still that READS, not a frame that moves
- **Subject motion:** `static` / N/A — this is a still. Do not plan camera moves or
  character action sequences meant for image-to-video.
- **Spatial framing:** compose for a **paused, readable** layout: headline zone, mascot
  or product, generous negative space. Any copy has to live on the image.
- **Camera:** `shot_language.camera_movement: "static"`. Prefer medium / medium_close.

### 3. Narrative role
Pick one `narrative_role` that matches the brief (`introduce_subject`, `deliver_payload`,
or `call_to_action`). `shot_intent` states WHY this still exists.

### 4. Bilingual captions
Set:

```json
"captions": { "zh": "<Chinese on-image copy>", "en": "<English on-image copy>" }
```

Keep them short. These strings are the source of truth for GATE 2 (bake the primary
language into the still). If the brief has no on-image copy, still set both keys (short
descriptive labels are fine).

### 5. Social ratio as a PLAN requirement
Record the job's aspect ratio in `scene_plan.metadata.aspect_ratio` (from
`options.aspect_ratio`, default `1:1`). Do not rewrite a caller-set ratio.
The assets stage must pass this to `generate_image`.

### 6. Panda identity as PLAN requirements
Name the panda (and customer, if present) Element id from `config/panda-elements.json`.
Descriptions say "the woman" / "the panda", not "a traveller" / "a cute panda". Default
medium is **2D flat** matching the turnaround sheets (`styles/panda.yaml`). Map phrase
aliases per CHARACTER LOCK in `skills/meta/higgsfield-mcp-bridge.md`. Set top-level
`style_playbook` to `panda`. Never render here.

### 7. Declare `required_assets` — one still
The scene lists **exactly one** `{type: "image", description: "...", source: "generate"}`.
The description must be actionable (composition, mascot pose, where the copy sits,
on-model Element id, 2D flat). Do **not** declare intermediate plates, video, narration, or music.

### 8. Coverage checks (before submitting)
- [ ] `scenes.length == 1`
- [ ] The scene has `captions.zh` and `captions.en`
- [ ] `metadata.aspect_ratio` matches the job option (default `1:1`)
- [ ] The `required_asset` is a still feasible with `image_selector` / Higgsfield

### 9. Write the scene_plan + STOP (GATE 1)
Persist a schema-valid `scene_plan`. Checkpoint `status = awaiting_human`. Surface the
plan as text (copy, captions, framing, required still). Do **not** proceed to `assets`
until approved.

## Handoff
The `assets` stage generates the one still at the planned ratio, bakes primary-language
copy into it, and records it in `asset_manifest`. Then it STOPS — that is the last gate.
See `skills/pipelines/panda-image/asset-director.md`.

## Success criteria
- Schema-valid `scene_plan` (text only) — no media files
- Exactly one still-only `required_asset`; bilingual `captions`
- Social ratio recorded; panda Element ids specified
- Checkpoint left in `awaiting_human` for GATE 1 (`approve_scene_plan`)
