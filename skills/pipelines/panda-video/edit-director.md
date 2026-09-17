# Edit Director — Panda Video Pipeline

Build `edit_decisions` from the approved scene plan + asset manifest, then continue into
compose in the **same** headless leg. This is **not** the hybrid footage-led edit skill
(`skills/pipelines/hybrid/edit-director.md`).

## Headless / Dify contract (HARD)

- `edit` has `human_approval_default: false` in `pipeline_defs/panda-video.yaml`. The
  checkpoint protocol is binding: write `status="completed"` and continue. **Do not** end
  your turn on a question. **Do not** invent a gate.
- The next human pause is compose's `approve_final` only. Stdout questions are invisible to
  Dify — stopping without an `awaiting_human` checkpoint leaves the job stuck
  (`status=running`, `gate=null`).
- If AGENT_GUIDE "Ask Before Major Changes" conflicts with an ungated stage on this
  pipeline, **the ungated manifest wins**: pick the pipeline default, log it in
  `decision_log`, and proceed.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/edit_decisions.schema.json` | Artifact validation |
| Prior | `scene_plan`, `asset_manifest`, `script` | Timeline + media |
| Next | `skills/pipelines/panda-video/compose-director.md` | Assemble final.mp4 |

## Process

### 1. Build cuts from the audio-driven timeline

One primary cut per scene clip in `asset_manifest`. Read
`asset_manifest.metadata.timeline_contract`; it is the authoritative effective timeline once
assets are approved. It may assign unequal scene lengths, but its total must remain within ±5%
of the user's requested duration. If its status is `pacing_revision_required` or
`within_target_band` is false, do not compose unless the human explicitly approved a duration
exception recorded in `decision_log`.

For each cut, keep `in_seconds=0`, set `out_seconds` no later than the measured source media end,
and record `source_duration_seconds`, allocated `effective_duration_seconds`, and
`tail_hold_seconds`. Compose uses the effective duration: the renderer may clone the final frame
after source motion ends or trim only the post-speech tail. Never trim the head, change clip
speed, or trim through `audio_end_seconds`.

Carry `render_runtime` from the existing decision / scene_plan metadata **unchanged** (silent
swap = governance violation).

### 2. Audio and frame pre-conform (compose inputs)

From `asset_manifest.metadata.edit_decisions_for_compose` (or equivalent notes):

- **Mute/discard** the native AAC track baked into Higgsfield / Kling i2v clips before
  mixing narration + music.
- **All-top crop** off-spec deliveries (e.g. 1076×1928 → 1080×1920), not a centred
  cover-crop (preserves caption-band clearance).

### 2b. Multi-voice narration bed

Do **not** assume a single narration file. For every narration asset / script section:

1. Add an `audio.narration.segments[]` entry with `asset_id`, its effective `start_seconds`,
   optional `end_seconds`, and `speaker` when known.
2. At compose, pass **all** VO files as `panda_render` `audio.voice_tracks`:
   `[{ "path": "<vo file>", "at_s": <start_seconds> }, …]` under the music bed.
3. Derive the immutable scene-local source offset:
   `relative_at_s = section.start_seconds - scene.start_seconds`. Then place compose VO at
   `timeline_contract.scene.effective_start_seconds + relative_at_s`. The effective scene may
   move because earlier scenes have unequal audio-driven durations; moving picture and VO
   together preserves the exact offset used by the Seedance reference bed. Overlapping windows
   mix (true multi-track). Use `lib.i2v_duration.effective_audio_start` so resume always
   recomputes from source timestamps rather than a previously shifted result.
4. If only one VO file exists (legacy single-speaker jobs), `audio.voice_path` alone is fine.

### 2c. Validated lip-sync timing corrections

Read `asset_manifest.metadata.lip_sync_qa.scenes`. Apply an offset only when all are true:

- the scene has `validated_audio_offset_seconds`;
- its selected result is `pass` after the local timing re-check; and
- attempt 1 contains `evidence.expected_audio_offset_seconds`.

For narration segments belonging to that scene, calculate
`delta = validated_audio_offset_seconds - attempt_1.evidence.expected_audio_offset_seconds`,
then set `start_seconds = effective_scene_start + original_scene_local_offset + delta`. Record `scene_id` and the
signed `lip_sync_offset_applied_seconds` on each changed segment. Do not shift clips, music,
unrelated scenes, unresolved/inconclusive results, or any segment lacking validated evidence.
Never replace the source timestamp cumulatively on resume: always recompute from immutable
scene-plan section/scene timestamps plus the current effective scene start.

### 3. Target-duration and VO safety

Prefer the full-scene allocation in `asset_manifest.metadata.timeline_contract`; use legacy
`vo_duration_map` only for old jobs that lack it. The allocator has already selected supported
i2v durations and bounded post-speech holds while preserving the requested total-duration band.
Mute native clip AAC and lay the ElevenLabs bed as today — do not keep Higgsfield native audio.
**AUDIO LIPSYNC clips**
(Seedance with `generate_audio:false`, noted in `generation_summary` / `metadata.lip_sync_qa`)
are silent or discardable AAC; mouths were driven by the same VO file you lay here — still mute
+ lay VO (do not skip the bed thinking native audio carries brand voice).

For a legacy job, a missing/incomplete map, or any discovered VO overrun, preserve the complete
VO and stop for a pacing revision rather than silently making the master substantially short or
long. Do not shorten locked copy, retime a generated lip-sync clip, or create a hold while the
on-screen mouth should still be articulating.

### 4. Captions and overlays

Carry burned captions / promo overlays from `scene_plan` `overlay_notes` / `captions` into
`edit_decisions` so `panda_render` can apply them at compose. Do not bake new text into
clips here.

### 5. Checkpoint and continue

1. Write `edit_decisions` and checkpoint `edit` with `status="completed"` (ungated).
2. Immediately run compose per `skills/pipelines/panda-video/compose-director.md`.
3. Stop only when compose is `awaiting_human` for `approve_final` (`final.mp4` on disk).

## Success criteria

- `edit_decisions` validates; `render_runtime` unchanged from prior lock
- Every narration asset is listed in `audio.narration.segments` at its effective scene start plus
  immutable scene-local offset (and `speaker` when multi-voice)
- Only the scene allocator and QA-validated offset alter a narration's global timestamp; its
  relationship to the speaking clip remains unchanged
- Effective timeline stays within ±5% of the requested duration and no hold covers active speech
- Native clip audio muted in the edit plan; frame pre-conform noted for compose
- Same headless turn reaches compose `awaiting_human` — never a bare question exit
