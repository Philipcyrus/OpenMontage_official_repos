# Script Director - Hybrid Pipeline

## When To Use

This stage maps the story across source-led beats and support-led beats. You are deciding where the source carries the message and where support assets clarify it.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/script.schema.json` | Artifact validation |
| Prior artifact | `state.artifacts["idea"]["brief"]` | Anchor medium and deliverable mix |
| Tools | `transcriber`, `scene_detect`, `audio_enhance` | Optional source analysis |

## Process

### 1. Mark Source-Led Versus Support-Led Beats

For each section, state whether it is:

- carried by source dialogue or footage,
- carried by narration,
- carried by diagrams or overlays,
- carried by text only.

### 2. Use Source Speech When It Is Better Than Rewriting

If the supplied footage already contains strong lines, use `transcriber` and keep the authenticity. Do not replace good source material with unnecessary narration.

### 3. Use Support Only To Clarify

Support-led beats should answer:

- what is not visible,
- what needs summarizing,
- what needs emphasis,
- what changes for a different platform.

### 4. Use Metadata For Structure

Recommended metadata keys:

- `anchor_sections`
- `support_sections`
- `narration_sections`
- `required_support_assets`

### 4b. Brand speaker casting (Panda / VOICE CAST)

When the leg prompt includes a **VOICE CAST** map (panda-video), each script section SHOULD set
`speaker` to one of `customer` | `panda` | `narrator`:

- Match dialogue to the on-screen character when they speak (customer asks, panda answers).
- Use `narrator` for off-screen VO / CTA beds.
- Omit `speaker` only when the whole job uses the job default (`options.narrator`).
- **Multi-voice shots:** put multiple sections whose time windows fall inside one visual shot
  (up to all three brand speakers). Prefer sequential timing; overlapping windows are allowed
  and will mix at compose.
- One language per job — do not mix EN and ZH lines.

### 5. Quality Gate

- source-led beats are clearly marked,
- support-led beats are justified,
- the script does not depend on fake or unavailable assets without saying so,
- the structure can produce the intended deliverables.

### Mid-Production Fact Verification

If you encounter uncertainty during script writing:
- Use `web_search` to verify factual claims before committing them to the script
- Use `web_search` to find reference images for visual accuracy
- Log verification in the decision log: `category="visual_accuracy_check"`

Every factual claim in the script should be traceable to the `research_brief`.
If you make a claim that isn't in the research, do additional research and
add the source. Do not invent statistics, dates, or attributions.

## Common Pitfalls

- Rewriting strong source dialogue into weaker narration.
- Adding diagrams or cards where the footage already explains the point.
- Hiding unsupported requirements until asset generation.

---

## Gate Reminder (Binding)

This stage gates on human approval (`human_approval_default: true`). After review passes:
checkpoint with `status="awaiting_human"`, present the summary (the Backlot board renders
the artifact), and **END YOUR TURN**. Do not start the next stage in the same response.
Approval is per-gate — an earlier "go ahead" does not cover this gate.
