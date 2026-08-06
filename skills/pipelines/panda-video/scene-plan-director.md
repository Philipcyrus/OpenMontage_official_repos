# Scene-Plan Director — Panda Video Pipeline

> Upstream-faithful. Mirrors `pipelines/explainer/scene-director.md`: this stage produces a
> **structured text scene plan only** — it generates **no media**. All stills, clips, voice
> and music are produced later, in the `assets` stage.

## When To Use

You are turning the approved `script` into a **structured scene plan** (GATE 2). You write the
schema-valid `scene_plan` artifact and then **STOP for human approval**. You do **not**
generate stills, contact sheets, images, video, or audio here, and you have **no generation
tools** available. Each scene instead *declares* what it needs via `required_assets`, which the
`assets` stage fulfils.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/scene_plan.schema.json` | Artifact validation (text only) |
| Prior artifact | `state.artifacts["script"]["script"]` | The approved narrative/beats |
| Style | `styles/panda.yaml` | On-brand look — captured as PLAN requirements, not generated here |
| Elements | `config/panda-elements.json` | Panda/customer character element ids — referenced in `required_assets` |

## Process

### 1. Break the script into scenes
Cover the full script duration with realistic per-scene timings (`start_seconds`/`end_seconds`,
no gaps). For each scene record the schema fields: `id`, `type`, `description`,
`start_seconds`, `end_seconds`, and where relevant `framing`, `movement`, `shot_language`
(shot size / camera movement / lens / lighting / DoF), `character_actions`, `transition_in`,
`transition_out`, `shot_intent`, `narrative_role`.

### 2. Panda identity as PLAN requirements (not media)
Specify the on-brand look, character consistency and composition **in words**: which character
appears (panda vs customer), the panda's on-model appearance, and the reference Element id from
`config/panda-elements.json` that the assets stage must use for consistency. This is guidance
the asset-director will honor — you do not render anything.

### 3. Declare required_assets per scene
For each scene, list the assets the `assets` stage must produce, each as
`{type, description, source}` with `source` one of `generate | source | provided | record`.
Example: a scene needing a generated panda keyframe →
`{"type": "image", "description": "Panda mascot waves at the airport gate, medium shot, on-model per panda Element", "source": "generate"}`.
Every `source: "generate"` asset must be feasible with the assets-stage tools (image_selector,
higgsfield_mcp_video, seedance_video, elevenlabs_tts, music_gen).

### 4. Write the scene_plan artifact + STOP for approval (GATE 2)
Persist a schema-valid `scene_plan` (`version: "1.0"`, `scenes: [...]`). Checkpoint with
`status = awaiting_human`. Surface the **scene list as text** to the reviewer (timings, types,
descriptions, framing, movement, character actions, transitions, required_assets). Do **not**
proceed to `assets` until approved. On "request revision", rewrite the plan per the feedback and
re-checkpoint.

## Handoff to `assets`
The `assets` stage reads this `scene_plan` (+ `script`) and generates every declared asset —
stills, motion clips, narration, music — recording them in `asset_manifest`. See
`skills/pipelines/panda-video/asset-director.md`.

## Success criteria
- Schema-valid `scene_plan` (text only) — no media files produced by this stage
- Full duration covered with realistic timings and no gaps
- Every scene declares feasible `required_assets`
- Panda identity / character-consistency / composition captured as plan requirements
- Checkpoint left in `awaiting_human` for the gate
