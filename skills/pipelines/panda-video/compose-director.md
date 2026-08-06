# Compose Director — Panda Video Pipeline

Assemble the approved assets into a CLEAN (unbranded) master. Panda branding (logo/watermark/
cards) is a SEPARATE on-demand `panda_brand` step applied AFTER final approval — never here.

## Runtime routing (MANDATORY first step)

Read **`edit_decisions.render_runtime`** (locked earlier, carried unchanged) and route to the
matching engine. This mirrors upstream OpenMontage's runtime selection; the only Panda-specific
choice is that the **ffmpeg lane uses `panda_render`** (the folded montage-svc render) rather
than a bare concat, so the default output keeps its deterministic, brand-consistent craft.

| `render_runtime` | Tool | Use it for |
|---|---|---|
| `ffmpeg` (default) | **`panda_render`** | Character-mascot clip assembly (Higgsfield stills→clips + VO + music). Deterministic, clean/ugc profile. This is the right default for Panda ads. |
| `remotion` | **`video_compose`** (runtime=remotion) | React motion-graphics: kinetic stat/text cards, charts, word-level caption burn, avatar/lip-sync. |
| `hyperframes` | **`video_compose`** (runtime=hyperframes) | HTML/CSS/GSAP: kinetic typography, product-promo/launch-reel title cards, registry blocks. |

Rules (upstream governance — do NOT break):
- **No silent runtime swap.** If `edit_decisions.render_runtime` is `remotion`/`hyperframes` but
  that engine is unavailable on the box (`video_compose` availability check fails / `npx
  hyperframes doctor` blocker), STOP and escalate per AGENT_GUIDE.md — do NOT quietly fall back
  to ffmpeg. Any change must be a logged `render_runtime_selection` decision.
- **Deterministic compose.** Compose is a TOOL call, never hand-assembled by the agent (an
  agent-driven compose stalled before). `panda_render` and `video_compose` are both deterministic.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/render_report.schema.json` | Artifact validation |
| Prior artifacts | `edit_decisions` (incl. `render_runtime`), `asset_manifest` | Cut logic + media |
| Tools | `panda_render` (ffmpeg lane), `video_compose` (remotion/hyperframes lanes) | Assembly |

## Process

1. **Route** on `edit_decisions.render_runtime` (table above). For `ffmpeg`, call `panda_render`
   with the approved clips (+ VO/music) at the `ugc` profile (CLEAN, no branding). For
   `remotion`/`hyperframes`, call `video_compose` with the matching runtime; pass `proposal_packet`
   if present so the tool's swap-detection runs.
2. **Verify** the output exists and passes ffprobe (correct duration, resolution, has audio).
3. **Write `render_report`** (which runtime + tool was used, output path, checks) and checkpoint
   `awaiting_human` for the final gate (approve_final).

## Success criteria
- Output matches `edit_decisions.render_runtime` (no silent swap)
- CLEAN/unbranded master; `final.mp4` exists and passes ffprobe
- Checkpoint left in `awaiting_human` for the final gate
