# Idea Director — Panda Image Pipeline

> **Internal stage — NO human gate.** `pipeline_defs/panda-image.yaml` sets
> `idea.human_approval_default: false`, and **the manifest is authoritative**. Do NOT checkpoint
> `awaiting_human` and do NOT end your turn for approval here — write the stage `completed` and
> continue to `scene_plan`.

## When To Use

You are starting a Panda Mobile **single still** from the user's `brief`. This is not a video
and not a carousel. There is no script stage, motion, VO, music, edit, or compose. The
deliverable is **exactly one** on-brand still at the job's `options.aspect_ratio`
(default `1:1`; also `4:5`, `9:16`, `WIDTHxHEIGHT`, …).

This stage folds research + proposal *reasoning* into one internal step: commit the creative
direction from the brief, log the key production decisions, and surface anything missing —
**without a human pause and without multi-concept exploration**.

## Produces
- **`brief`** — the consolidated production brief (below).
- **`decision_log`** — the key decisions made here (one still, aspect ratio, language, providers).

## Process

### 1. Commit the creative direction (from the brief)
Panda is brief-driven. Restate the image concept concretely — do **not** generate 3+ alternative
concepts. If the brief is genuinely ambiguous on a material point (subject, language, CTA copy),
note it in `missing_capabilities` rather than inventing options.

**Character lock (binding):** any human / traveller / customer / person (and
`phrase_aliases.customer` in `config/panda-elements.json`) maps to
`customer_reference_element_id`. Any panda / mascot (and `phrase_aliases.panda`) maps to
`panda_reference_element_id`. Rewrite the brief so downstream never treats those words as a
character-design prompt. Log `character_lock` in `decision_log` with the IDs. See CHARACTER LOCK
in `skills/meta/higgsfield-mcp-bridge.md`.

**Visual medium (binding):** default **2D flat** per `styles/panda.yaml`. Override only if the
brief explicitly asks for 3D / photoreal / live-action — log it.

### 2. Deliverable & format
Fix `deliverable_mix`:
- **Kind:** one still that READS (paused, readable layout — not a frame that will be animated).
- **Count:** exactly **1**. Never plan a carousel or a sequence.
- **Aspect ratio:** honor `options.aspect_ratio` when the job set it (default `1:1`).
  Do not rewrite a caller-set ratio to `4:5`/`1:1`. Pass it through to the scene plan.
- **Language:** primary on-image language from `options.language` (`zh` or `en`); the scene plan
  will carry **both** zh and en captions regardless.
- **No** duration, narration, music, or render runtime — those are video-pipeline concerns.

### 3. Provider decisions (log them)
- **Visuals:** Higgsfield MCP stills (`generate_image`) with the panda/customer Element IDs from
  `config/panda-elements.json` attached as media. Same look as panda-video (`styles/panda.yaml`,
  2D flat default).
- **No TTS, no music, no video generation, no compose.**
- This pipeline has **no `compose` stage**, so there is no `render_runtime` to choose and no
  Remotion/HyperFrames conversation. Do not log a `render_runtime_selection`.

### 4. Brief metadata
Recommended keys: `concept`, `deliverable_mix` (still_count=1, aspect_ratio, language),
`character_lock`, `visual_medium` (default `2d_flat`), `missing_capabilities`, `fallback_policy`.

### 5. Quality check (self, no human pause)
- [ ] The concept is a single readable still, not a video or a carousel
- [ ] Aspect ratio (from `options.aspect_ratio`, default `1:1`) + language are explicit
- [ ] Human/panda phrases resolved to locked Element IDs; `character_lock` logged
- [ ] Visual medium is 2D flat unless the brief explicitly overrides
- [ ] Provider decision (Higgsfield stills + panda Elements) is logged
- [ ] Missing capabilities / fallbacks are surfaced early

## Handoff
`scene_plan` writes the one-scene TEXT plan (GATE 1). Cost governance, if a
`max_higgsfield_credits` cap is set on the job, is enforced later at still generation —
see `skills/meta/higgsfield-mcp-bridge.md`.

## Gate Reminder (Binding)
**This stage does NOT gate.** For `panda-image`, `idea` is internal: write the checkpoint
`completed` and proceed to `scene_plan` in the same run. There is **no script stage**.
The first human gate is `approve_scene_plan`.
