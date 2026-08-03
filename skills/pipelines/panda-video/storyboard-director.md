# Storyboard Director — Panda Video Pipeline

> **SCAFFOLD.** Starting point adapted from `pipelines/hybrid/scene-director.md` +
> `asset-director.md`. Fill in the TODOs before production use.

## When To Use

You are turning the approved `script` into a **visual storyboard**: exactly **one keyframe
still per scene**. You generate the stills, write them into the `scene_plan` artifact, and
then **STOP for human approval** (this is GATE 2). No motion video is generated here — clips
are produced only after the stills are approved, in the `assets` stage.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/scene_plan.schema.json` | Artifact validation |
| Prior artifact | `state.artifacts["script"]["script"]` | The approved narrative/beats |
| Style | `styles/panda.yaml` | On-brand look of GENERATED stills |
| Elements | `config/panda-elements.json` | Panda character element ids / master refs |
| Tools | `image_selector` (+ still-gen tool — TODO confirm name) | Make one still per scene |

## Process

### 1. One still per scene
For each scene in the script, generate a single keyframe still that represents the scene.
- Apply the brand look from `styles/panda.yaml` (`asset_generation.image_prompt_prefix`,
  negative prompt, consistency anchors).
- Use the panda character references from `config/panda-elements.json` so the panda is
  consistent across scenes. TODO: wire the exact element-id passing convention.

### 2. Character consistency
The panda mascot must look the same in every still. Use the master reference(s) in
`config/panda-elements.json`. TODO: decide still-gen path — Higgsfield `generate_image`
(via MCP) vs an upstream `tools/graphics` model — and record which in the artifact.

### 3. Write the scene_plan artifact
Per scene record: `scene_id`, `beat`, the still file path, the prompt used, and the
character element id(s). Also write a **contact sheet** (all stills tiled) for easy review —
save as `storyboard-contact-sheet.jpg`. TODO: confirm scene_plan schema fields.

### 4. STOP for approval (GATE 2)
Checkpoint with `status = awaiting_human`. Surface the contact sheet + per-scene stills to
the reviewer. Do **not** proceed to `assets` until approved. On "request revision",
regenerate only the flagged scenes and re-checkpoint.

## Handoff to `assets`
The `assets` stage animates each APPROVED still into a clip via the Higgsfield MCP bridge
(`higgsfield_mcp_video`, image_to_video) — see `skills/meta/higgsfield-mcp-bridge.md`.

## Success criteria
- Schema-valid `scene_plan` artifact
- Exactly one still per scene, all on disk
- Contact sheet generated
- Checkpoint left in `awaiting_human` for the gate
