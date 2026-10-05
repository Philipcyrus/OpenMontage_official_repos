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

Read `asset_manifest.metadata.timeline_contract`; it is the authoritative effective timeline
once assets are approved. It may assign unequal scene lengths. Stay within ±5% of
`timeline_contract.target_duration_seconds` — the measured ElevenLabs dialogue length, which may
be longer or shorter than the original brief (honor the logged `dialogue_priority` decision).
If status is `pacing_revision_required` because a single scene still exceeds the provider max,
do not compose until that copy/speed issue is resolved. A mismatch with the brief alone is never
a blocker.

**Subshot scenes** (the scene has `asset_manifest.metadata.scene_subshots.<scene_id>` and its
timeline row has `subshot_content_seconds`): call
`lib.i2v_duration.place_scene_subshots(scene_subshots, effective_scene_start_seconds=<row
effective_start_seconds>, effective_duration_seconds=<row effective_duration_seconds>,
validated_offsets=<per-section offsets from §2c>)`. Emit **one cut per returned subshot**, in
order, with `scene_id`, `subshot_id`, `subshot_kind`, `section_id`, `speaker`,
`in_seconds=source_in_seconds`, `out_seconds=source_in_seconds + duration_seconds`, and
`effective_duration_seconds=duration_seconds`:
- `speaking` → `source` = that subshot's clip asset, `audio_lipsync: true`,
  `audio_end_seconds` from the subshot, `source_duration_seconds` = the clip's measured length.
  The unused tail past `duration_seconds` is discarded — never held over the next line.
- `fill` → `source` = the approved still (or the closed-mouth fill clip when one was made),
  `audio_lipsync: false`. Narrator lines, lead-ins and the scene tail are fills.

**Single-clip scenes** (narrator-only HOLD, text_card, legacy jobs): one cut per clip; keep
`in_seconds=0`, set `out_seconds` no later than the measured source media end, and record
`source_duration_seconds`, allocated `effective_duration_seconds`, and `tail_hold_seconds`.
Compose uses the effective duration: the renderer may clone the final frame after source motion
ends or trim only the post-speech tail.

Never change clip speed, never trim through `audio_end_seconds`, and never trim a clip's head
except by a validated per-subshot `source_in_seconds`.

Carry `render_runtime` from the existing decision / scene_plan metadata **unchanged** (silent
swap = governance violation).

### 2. Audio and frame pre-conform (compose inputs)

From `asset_manifest.metadata.edit_decisions_for_compose` (or equivalent notes):

- **Mute/discard** the native AAC track baked into Higgsfield / Kling i2v clips before
  mixing narration + music.
- **All-top crop** off-spec deliveries to the job master canvas from
  `scene_plan.metadata.aspect_ratio` / `options.aspect_ratio` (default `9:16` → `1080x1920`;
  e.g. `16:9` → `1920x1080`). Example: a slightly off 1076×1928 vertical delivery → `1080x1920`.
  Do **not** use a centred cover-crop (preserves caption-band clearance). Record the target
  resolution for compose (`panda_render` `resolution`).

### 2b. Multi-voice narration bed

Do **not** assume a single narration file. For every narration asset / script section:

1. Add an `audio.narration.segments[]` entry with `asset_id`, its effective `start_seconds`,
   optional `end_seconds`, `speaker`, `section_id`, and (for subshot scenes) `subshot_id`.
2. **Subshot scenes:** take the VO placements straight from `place_scene_subshots(...)
   ["voice_tracks"]` — each line starts at its own subshot (plus that line's validated delay),
   in sequence. Script timestamps fix the order of lines, not their clocks: a line that runs
   longer than its script slot pushes the next line later instead of playing over it.
   **Single-clip scenes:** place the VO at `timeline_contract.scene.effective_start_seconds`
   plus its scene-local offset via `lib.i2v_duration.effective_audio_start`, passing
   `previous_audio_end_seconds` for every line after the first; it raises instead of letting
   two voices overlap.
3. At compose, pass **all** VO files as `panda_render` `audio.voice_tracks`:
   `[{ "path": "<vo file>", "at_s": <start_seconds>, "duration_s": <measured>, "section_id": … }, …]`
   under the music bed. Voices never overlap: `panda_render` rejects overlapping tracks, and
   that rejection is a timing bug to fix here, not a reason to set `allow_voice_overlap`.
4. Recompute everything from the immutable inputs on resume — `metadata.scene_subshots` (built
   from measured VO and the scene-plan section/scene timestamps) plus the current effective scene
   start — never from a previously shifted `start_seconds`.
5. If only one VO file exists (legacy single-speaker jobs), `audio.voice_path` alone is fine.

### 2c. Validated lip-sync timing corrections

Read `asset_manifest.metadata.lip_sync_qa.subshots.<scene_id>.<section_id>` (legacy jobs:
`lip_sync_qa.scenes.<scene_id>`). A correction belongs to **one speaking subshot** — one speaker's
line — and is applied only when all are true:

- that subshot's review has `validated_audio_offset_seconds`;
- its selected result is `pass` after the local timing re-check; and
- attempt 1 contains `evidence.expected_audio_offset_seconds`.

Calculate `delta = validated_audio_offset_seconds - attempt_1.evidence.expected_audio_offset_seconds`
and pass it as `validated_offsets[section_id]` to `place_scene_subshots`. A positive delta delays
that voice inside its own subshot (later subshots move later if it grows); a negative delta trims
the head of that clip via `source_in_seconds`. Record `scene_id`, `section_id`, `subshot_id`, and
the signed `lip_sync_offset_applied_seconds` on the changed segment only. Never shift a sibling
line, the narrator, the whole scene, music, unrelated scenes, unresolved/inconclusive results,
or any segment lacking validated evidence. For a legacy single-clip scene, the same rule applies
to that scene's one speaking line:
`start_seconds = effective_scene_start + original_scene_local_offset + delta`.
Never apply a delta cumulatively on resume: always recompute from immutable scene-plan
section/scene timestamps (via `metadata.scene_subshots`) plus the current effective scene start.

### 3. Target-duration and VO safety

Prefer the full-scene allocation in `asset_manifest.metadata.timeline_contract`; use legacy
`vo_duration_map` only for old jobs that lack it. The allocator has already selected supported
i2v durations and bounded post-speech holds within the band around its measured-dialogue target.
Mute native clip AAC and lay the ElevenLabs bed as today — do not keep Higgsfield native audio.
**AUDIO LIPSYNC clips**
(Seedance native speech with `generate_audio:true`, noted `[speech:native]` in
`generation_summary`) carry Seedance's raw voice in their AAC; each was re-voiced into the cast
voice by `elevenlabs_voice_changer` with identical timing. Mute the clip and lay that re-voiced
file at its subshot (it is the subshot's `vo_path`; it starts at clip time 0) — never ship the
raw native voice, and do not skip the voice tracks.

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
- Every narration asset is listed in `audio.narration.segments` at the start of its own subshot
  (or, for single-clip scenes, its effective scene start plus scene-local offset), with
  `speaker` and `section_id`
- Every on-screen line has its own speaking cut; narrator lines, lead-ins and tails are fill
  cuts; no two `voice_tracks` overlap
- Only the scene allocator, sequential subshot placement, and that line's own QA-validated
  offset alter a narration's global timestamp; no correction moves a sibling line
- Effective timeline stays within ±5% of `timeline_contract.target_duration_seconds` (may be
  longer or shorter than the original brief under dialogue-duration priority) and no hold covers active speech
- Native clip audio muted in the edit plan; frame pre-conform noted for compose
- Same headless turn reaches compose `awaiting_human` — never a bare question exit
