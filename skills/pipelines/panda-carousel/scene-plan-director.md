# Scene-Plan Director — Panda Carousel Pipeline

> Structured **text** slide plan only — no media. One scene = one carousel slide. GATE 2.

## When To Use

You are turning the approved `script` (per-slide copy) into a **structured scene plan**.
You write the schema-valid `scene_plan` artifact, then **STOP for human approval**. You
do **not** generate stills here. Each scene *declares* a still via `required_assets`.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/scene_plan.schema.json` | Artifact validation (text only; optional `captions`) |
| Prior artifact | `script` | Per-slide copy |
| Style | `styles/panda.yaml` | On-brand look — captured as PLAN requirements |
| Elements | `config/panda-elements.json` | Panda/customer Element ids |

## Process

### 1. One scene per slide
Transform each script section into **exactly one** scene. Set `id` (`slide-1` …), `type`
(`generated` for illustrated slides; `text_card` only when the slide is typography-led
with no mascot), `description`, `script_section_id`, and dummy `start_seconds`/`end_seconds`
matching the script slots.

### 2. Slides that READ, not frames that move
- **Subject motion:** `static` / N/A — this is a still. Do not plan camera moves or
  character action sequences meant for image-to-video.
- **Spatial framing:** compose for a **paused, readable** layout: headline zone, mascot
  or product, generous negative space. The copy has to live on the image.
- **Camera:** `shot_language.camera_movement: "static"`. Prefer medium / medium_close.

### 3. Copy hierarchy via `narrative_role`
- First slide: `establish_context` or `introduce_subject` (the hook).
- Middle slides: `deliver_payload` / `evidence` / `comparison` as they earn it.
- Last slide: `call_to_action`.
- `shot_intent` states WHY this slide exists in the carousel.

### 4. Bilingual captions (mandatory)
Every scene MUST set:

```json
"captions": { "zh": "<Chinese on-slide copy>", "en": "<English on-slide copy>" }
```

These strings are the source of truth for GATE 3 (bake the primary language into the
still) and for Dify/hand-posting. Keep them short.

### 5. Social ratio as a PLAN requirement
Record the job's aspect ratio in `scene_plan.metadata.aspect_ratio` (from
`options.aspect_ratio`, default `4:5`). Do not rewrite a caller-set ratio.
The assets stage must pass this to `generate_image`.

### 6. Panda identity as PLAN requirements
Name the panda (and customer, if present) Element id from `config/panda-elements.json` on every
slide that shows that role. Descriptions say "the woman" / "the panda", not "a traveller" /
"a cute panda". Default medium is **2D flat** matching the turnaround sheets (`styles/panda.yaml`).
Map phrase aliases per CHARACTER LOCK in `skills/meta/higgsfield-mcp-bridge.md`. Set top-level
`style_playbook` to `panda`. Never render here.

### 7. Declare `required_assets` — stills only
Each scene lists **exactly one** `{type: "image", description: "...", source: "generate"}`.
Descriptions must be actionable (composition, mascot pose, where the copy sits, on-model
Element id, 2D flat). Do **not** declare intermediate plates, video, narration, or music.

### 8. Coverage checks (before submitting)
- [ ] Scene count == script section count; order matches the carousel
- [ ] Hook / content / CTA roles are set; last slide is `call_to_action`
- [ ] Every scene has `captions.zh` and `captions.en`
- [ ] `metadata.aspect_ratio` matches the job option (default `4:5`)
- [ ] Every `required_asset` is a still feasible with `image_selector` / Higgsfield

### 9. Write the scene_plan + STOP (GATE 2)
Persist a schema-valid `scene_plan`. Checkpoint `status = awaiting_human`. Surface the
slide list as text (copy, captions, framing, required still). Do **not** proceed to
`assets` until approved.

## Handoff
The `assets` stage generates one still per scene at the planned ratio, bakes primary-
language copy into the still, and records everything in `asset_manifest`. Then it STOPS
— that is the last gate. See `skills/pipelines/panda-carousel/asset-director.md`.

## Success criteria
- Schema-valid `scene_plan` (text only) — no media files
- One still-only `required_asset` per slide; bilingual `captions` on every scene
- Social ratio recorded; panda Element ids specified
- Checkpoint left in `awaiting_human` for GATE 2
