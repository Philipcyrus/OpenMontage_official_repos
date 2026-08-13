# Idea Director — Panda Carousel Pipeline

> **Internal stage — NO human gate.** `pipeline_defs/panda-carousel.yaml` sets
> `idea.human_approval_default: false`, and **the manifest is authoritative**. Do NOT checkpoint
> `awaiting_human` and do NOT end your turn for approval here — write the stage `completed` and
> continue to `script`.

## When To Use

You are starting a Panda Mobile **stills-only social carousel** from the user's `brief`. This is
not a video. There is no motion, VO, music, edit, or compose. The deliverable is a set of
on-brand still slides (typically 4–8, default 6) at the job's `options.aspect_ratio`
(default `4:5`; also `1:1`, `9:16`, `WIDTHxHEIGHT`, …).

This stage folds research + proposal *reasoning* into one internal step: commit the creative
direction from the brief, log the key production decisions, and surface anything missing —
**without a human pause and without multi-concept exploration**.

## Produces
- **`brief`** — the consolidated production brief (below).
- **`decision_log`** — the key decisions made here (slide count, aspect ratio, language, providers).

## Process

### 1. Commit the creative direction (from the brief)
Panda is brief-driven. Restate the carousel concept concretely — do **not** generate 3+ alternative
concepts. If the brief is genuinely ambiguous on a material point (slide count, language, CTA),
note it in `missing_capabilities` rather than inventing options.

### 2. Deliverable & format
Fix `deliverable_mix`:
- **Kind:** stills-only carousel (slides that READ, not frames that move).
- **Slide count:** 4–8, default 6 unless the brief specifies otherwise.
- **Aspect ratio:** honor `options.aspect_ratio` when the job set it (default `4:5`).
  Do not rewrite a caller-set ratio to `4:5`/`1:1`. Pass it through to the scene plan.
- **Language:** primary on-slide language from `options.language` (`zh` or `en`); the scene plan
  will carry **both** zh and en captions regardless.
- **No** duration, narration, music, or render runtime — those are video-pipeline concerns.

### 3. Provider decisions (log them)
- **Visuals:** Higgsfield MCP stills (`generate_image`) with the panda/customer Element IDs from
  `config/panda-elements.json`. Same look as panda-video (`styles/panda.yaml`).
- **No TTS, no music, no video generation, no compose.**
- This pipeline has **no `compose` stage**, so there is no `render_runtime` to choose and no
  Remotion/HyperFrames conversation. Do not log a `render_runtime_selection`.

### 4. Brief metadata
Recommended keys: `concept`, `deliverable_mix` (slide_count, aspect_ratio, language),
`slide_hierarchy` (hook / content / CTA), `missing_capabilities`, `fallback_policy`.

### 5. Quality check (self, no human pause)
- [ ] The concept is a carousel of readable slides, not a video
- [ ] Slide count + aspect ratio (from `options.aspect_ratio`, default `4:5`) + language are explicit
- [ ] Provider decision (Higgsfield stills + panda Elements) is logged
- [ ] Missing capabilities / fallbacks are surfaced early

## Handoff
`script` writes per-slide copy (GATE 1). Cost governance, if a `max_higgsfield_credits` cap is
set on the job, is enforced later at still generation — see `skills/meta/higgsfield-mcp-bridge.md`.

## Gate Reminder (Binding)
**This stage does NOT gate.** For `panda-carousel`, `idea` is internal: write the checkpoint
`completed` and proceed to `script` in the same run. The first human gate is `approve_script`
(unless the job option `gates` omits `script`, in which case auto-complete script per the
launcher prompt and continue to `scene_plan`).
