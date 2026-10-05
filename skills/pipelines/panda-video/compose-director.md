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
| Tools | `screen_overlay` | User screenshots laid over their scenes (only when the job has them) |

## Process

0. **User screenshots** (only when the prompt has a USER SCREENSHOTS block listing screenshot
   scenes): build the exact `panda_render` scene list first, with `scene_id` on every item. Call
   `screen_overlay` with `{"mode": "compose", "project_id": "<job>", "scenes": <that list>,
   "resolution": <same as panda_render>, "language": "<zh|en>", "transition": <the same transition
   object you pass panda_render>}` and pass `panda_render` the `scenes` it returns — screenshot
   scenes now point at `overlay/<scene_id>.mp4`. Passing the same `transition` matters: the tool
   writes `overlay/timeline.json` (where every scene and screenshot lands in the assembled video)
   and the launcher checks each screenshot there; with the wrong transition it can only report the
   check as unavailable. Never skip the call, never place the screenshots any other way, and if it
   fails stop and escalate (no render without the screenshots).

   If it refuses with a timing error, a cut is shorter than the placement the user asked for. Do not
   lengthen, move or drop the placement to get past it: fix the scene plan's `show` / `at_s` (or the
   cut) so the screenshot fits, then compose again.

1. **Route** on `edit_decisions.render_runtime` (table above). For `ffmpeg`, call `panda_render`
   with the approved clips (+ VO/music) at the `ugc` profile (CLEAN, no branding). Pass
   `resolution` from the job canvas (`scene_plan.metadata.aspect_ratio` /
   `options.aspect_ratio`: default `9:16` → `1080x1920`, `16:9` → `1920x1080`, etc.) — do not
   hardcode vertical. Emit **one `panda_render` scene per edit cut** — a subshot scene becomes
   several consecutive scenes (its speaking clips and its closed-mouth fills, stills included).
   Set each `scenes[].duration_s` from its cut's `effective_duration_seconds` (not the
   downloaded clip length), and pass `target_duration_s` plus `duration_tolerance_fraction=0.05`
   from `asset_manifest.metadata.timeline_contract`. Also pass each scene's measured
   `source_duration_s`, allocated `audio_end_s`, `audio_lipsync` flag, and `source_in_s` (the
   cut's `in_seconds`; non-zero only for a validated negative per-subshot offset) so the renderer
   rejects any trim or frozen hold that would intersect active lip-synced speech. Use a hard
   `cut` transition between subshots of the same scene — an xfade would blend two mouths and eat
   into a line. `panda_render` accepts at most 60 scenes; if a long job exceeds that, merge
   adjacent fill cuts that use the same still (never merge speaking cuts).

   Pass every narration segment as `audio.voice_tracks` (`path`, `at_s`, `duration_s`, and
   `section_id` from `edit_decisions.audio.narration.segments`); single-VO jobs may still use
   `audio.voice_path`. Voice tracks are sequential: `panda_render` refuses overlapping tracks, and
   that refusal means edit placed two lines on top of each other — fix the edit, never set
   `allow_voice_overlap` to get past it. Mute / discard native AAC on Higgsfield clips (the
   voice tracks are the dialogue). AUDIO LIPSYNC Seedance clips are native speech
   (`generate_audio:true`): their AAC is Seedance's raw voice and must never reach the master —
   lay the re-voiced ElevenLabs file (`elevenlabs_voice_changer`, same timing) for each line, and
   the ElevenLabs TTS file for narrator lines. Legacy clips with `generate_audio:false` are
   silent; lay their ElevenLabs files the same way.
   Use narration `start_seconds` exactly as edit wrote them: the edit stage already combined the
   allocated effective scene start, sequential subshot placement, and any locally validated
   per-line lip-sync delta. Do not reapply, remove, or cumulatively add offsets during compose.
   Honor only post-speech `tail_hold_seconds` from the timeline contract. For
   `remotion`/`hyperframes`, call
   `video_compose` with the matching runtime; pass `proposal_packet` if present so the tool's
   swap-detection runs.
2. **Verify** the output exists and passes ffprobe (duration within ±5% of
   `timeline_contract.target_duration_seconds` — which may be longer or shorter than the original
   brief under dialogue-duration priority — resolution matching the job canvas, has audio). A target-duration
   validation failure is not a warning: correct the
   scene/transition math and render again before writing the final checkpoint.
3. **Write `render_report` and `final_review`.** Copy the asset manifest QA summary into optional
   `final_review.checks.lip_sync_check`, including reviewed scenes and subshots (per speaker),
   applied offsets, affected scene/subshot ids, and warnings. A result still unresolved after attempt 2 uses `status:"warning"` and
   `recommended_action:"present_to_user"` both inside `lip_sync_check` and at final-review top
   level; it does not cause another automatic retry or block the final gate. Include
   `asset_manifest` and `final_review` in the compose checkpoint artifacts so the launcher can
   surface the warning.
4. Checkpoint `awaiting_human` for the final gate (approve_final).

## Success criteria
- Output matches `edit_decisions.render_runtime` (no silent swap)
- CLEAN/unbranded master; `final.mp4` exists and passes ffprobe at the job canvas resolution
- Final duration is within ±5% of `timeline_contract.target_duration_seconds` (dialogue-priority
  targets may be longer or shorter than the original brief)
- Scene durations are unequal when audio pacing calls for it; no active lip-synced speech is
  padded, trimmed, or retimed
- No two voice tracks overlap; every speaking cut carries only its own speaker's line
- Every unresolved lip-sync result names its scene at approve_final; no hidden pass and no loop
- Checkpoint left in `awaiting_human` for the final gate
