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
   with the approved clips (+ VO/music) at the `ugc` profile (CLEAN, no branding). Set each
   `scenes[].duration_s` from its cut's `effective_duration_seconds` (not the downloaded clip
   length), and pass `target_duration_s` plus `duration_tolerance_fraction=0.05` from
   `asset_manifest.metadata.timeline_contract`. Also pass each scene's measured
   `source_duration_s`, allocated `audio_end_s`, and `audio_lipsync` flag so the renderer rejects
   any trim or frozen hold that would intersect active lip-synced speech. Pass every
   narration segment as `audio.voice_tracks` (`path` + `at_s` from `edit_decisions.audio.narration.segments`);
   single-VO jobs may still use `audio.voice_path`. Mute / discard native AAC on Higgsfield clips
   (TTS-first VO is the dialogue bed; AUDIO LIPSYNC Seedance clips use `generate_audio:false` so
   mouths match that VO while the clip stays silent — still lay the same ElevenLabs files).
   Use narration `start_seconds` exactly as edit wrote them: the edit stage already combined the
   allocated effective scene start, immutable scene-local audio offset, and any locally validated
   lip-sync delta. Do not reapply, remove, or cumulatively add offsets during compose.
   Honor only post-speech `tail_hold_seconds` from the timeline contract. For
   `remotion`/`hyperframes`, call
   `video_compose` with the matching runtime; pass `proposal_packet` if present so the tool's
   swap-detection runs.
2. **Verify** the output exists and passes ffprobe (duration within the requested ±5% band,
   resolution, has audio). A target-duration validation failure is not a warning: correct the
   scene/transition math and render again before writing the final checkpoint.
3. **Write `render_report` and `final_review`.** Copy the asset manifest QA summary into optional
   `final_review.checks.lip_sync_check`, including reviewed scenes, applied offsets, affected scene
   ids, and warnings. A result still unresolved after attempt 2 uses `status:"warning"` and
   `recommended_action:"present_to_user"` both inside `lip_sync_check` and at final-review top
   level; it does not cause another automatic retry or block the final gate. Include
   `asset_manifest` and `final_review` in the compose checkpoint artifacts so the launcher can
   surface the warning.
4. Checkpoint `awaiting_human` for the final gate (approve_final).

## Success criteria
- Output matches `edit_decisions.render_runtime` (no silent swap)
- CLEAN/unbranded master; `final.mp4` exists and passes ffprobe
- Final duration is within `timeline_contract`'s requested ±5% band
- Scene durations are unequal when audio pacing calls for it; no active lip-synced speech is
  padded, trimmed, or retimed
- Every unresolved lip-sync result names its scene at approve_final; no hidden pass and no loop
- Checkpoint left in `awaiting_human` for the final gate
