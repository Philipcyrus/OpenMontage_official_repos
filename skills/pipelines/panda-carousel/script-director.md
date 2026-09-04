# Script Director — Panda Carousel Pipeline

> Slide copy, not voiceover. Each script section is one carousel slide. GATE 1.

## When To Use

You have an approved (or internally completed) `brief` for a Panda Mobile stills carousel.
Write the **on-slide copy** as a schema-valid `script`, then **STOP for human approval**
(unless the job option `gates` omits `script` — then write `completed` with
`human_approved=True`, log `category: "approval_policy"` as auto-approved by job option,
and continue to `scene_plan` in the same turn).

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/script.schema.json` | Artifact validation |
| Prior artifact | `brief` | Slide count, language, CTA, concept |

This is **not** the hybrid source-footage script director. There is no VO, no source beats,
no TTS performance contract to design.

## Process

### 1. One section per slide
Slide count comes from the brief (typically 4–8, default 6). Each section:
- `id` — `slide-1` … `slide-N` (or `hook` / `cta` for the ends)
- `label` — `hook` | `content` | `cta`
- `text` — the **on-slide copy** in the primary language (short; a headline + optional
  one-line body). This is what the still will show, not what a narrator would say.
- `start_seconds` / `end_seconds` — dummy sequential slots (1s per slide) to satisfy the
  script schema. Carousels have no runtime.

### 2. Copy hierarchy (mandatory)
- **Slide 1 = hook.** One punchy line. Curiosity or a sharp claim. No logo-dump.
- **Middle slides = content.** One idea per slide. Do not cram a paragraph onto a still.
- **Last slide = CTA.** Clear next step (e.g. grab the eSIM / scan / shop).

### 3. Bilingual intent
Write `text` in the job's primary language. Put the other language in
`enhancement_cues` or a short note on the section so the scene-plan director can fill
`captions.zh` and `captions.en`. Do not skip the second language — Dify/hand-posting
needs both.

### 3b. Character lock on slides
If on-slide copy or notes refer to a human / traveller / customer or panda / mascot, those
are the locked Elements from `config/panda-elements.json` — not free-invented talent. Do not
write character-design prose into slide copy; leave identity to the scene plan + assets
(CHARACTER LOCK in `skills/meta/higgsfield-mcp-bridge.md`).

### 4. Quality gate
- [ ] Section count matches the planned slide count
- [ ] Hook / content / CTA hierarchy is obvious from `label`s
- [ ] Copy is short enough to read on the planned still (ratio from `options.aspect_ratio`)
- [ ] CTA is a real action, not a vague closer
- [ ] Any human/panda mention is treated as the locked brand characters, not new designs

### 5. Write the script + STOP (GATE 1)
Persist a schema-valid `script` (`version: "1.0"`, `title`, `total_duration_seconds` =
slide count in dummy seconds, `sections: [...]`). Checkpoint `status = awaiting_human`
unless auto-approve-script is on. Do **not** proceed to `scene_plan` until approved
(or auto-approved).

## Handoff
`scene_plan` turns each section into one slide scene with bilingual `captions` and a
stills-only `required_assets` list.

## Success criteria
- Schema-valid `script` — one section per slide, hook → content → CTA
- Copy is on-slide text, not narration
- Checkpoint left in `awaiting_human` (or `completed` + auto-approved when `gates` omits script)
