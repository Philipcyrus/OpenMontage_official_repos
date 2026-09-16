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

### 1. Lock cut points from the scene plan

One primary cut per scene clip in `asset_manifest`. Carry `render_runtime` from the existing
decision / scene_plan metadata **unchanged** (silent swap = governance violation).

### 2. Audio and frame pre-conform (compose inputs)

From `asset_manifest.metadata.edit_decisions_for_compose` (or equivalent notes):

- **Mute/discard** the native AAC track baked into Higgsfield / Kling i2v clips before
  mixing narration + music.
- **All-top crop** off-spec deliveries (e.g. 1076×1928 → 1080×1920), not a centred
  cover-crop (preserves caption-band clearance).

### 2b. Multi-voice narration bed

Do **not** assume a single narration file. For every narration asset / script section:

1. Add an `audio.narration.segments[]` entry with `asset_id`, `start_seconds` (from the
   section), optional `end_seconds`, and `speaker` when known.
2. At compose, pass **all** VO files as `panda_render` `audio.voice_tracks`:
   `[{ "path": "<vo file>", "at_s": <start_seconds> }, …]` under the music bed.
3. Prefer sequential timing inside a shot; overlapping `at_s` windows mix (true multi-track).
4. If only one VO file exists (legacy single-speaker jobs), `audio.voice_path` alone is fine.

### 3. VO / slot overrun (PACING RISK)

Prefer **TTS-first pre-aligned slots** from assets: read
`asset_manifest.metadata.vo_duration_map` (per scene `vo_seconds`, `i2v_duration`,
`hold_extend_seconds`). When `hold_extend_seconds > 0`, extend that scene's on-screen hold by
at least that amount so the full VO plays after the i2v clip ends. Mute native clip AAC and lay
the ElevenLabs bed as today — do not keep Higgsfield native audio. **AUDIO LIPSYNC clips**
(`metadata.audio_lipsync: true` / Seedance with `generate_audio:false`) are silent or discardable
AAC; mouths were driven by the same VO file you lay here — still mute + lay VO (do not skip the
bed thinking native audio carries brand voice).

If `known_issues` / PACING RISK still flags narration that overruns its visual slot (map missing
or incomplete):

- **DEFAULT:** extend that scene's on-screen hold so the **locked** CTA / VO copy finishes
  (total runtime may exceed the brief's nominal seconds).
- Log the choice in `decision_log` (`category` such as `pacing` / subject naming the scene).
- Do **not** shorten locked copy.
- Do **not** wait for an a/b/c answer — GATE 4 (`approve_assets`) already meant proceed.

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
- Every narration asset is listed in `audio.narration.segments` with `start_seconds` (and
  `speaker` when multi-voice)
- VO overrun resolved by extending hold (or N/A if all VO fits); prefer
  `vo_duration_map` hold extends from TTS-first assets when present
- Native clip audio muted in the edit plan; frame pre-conform noted for compose
- Same headless turn reaches compose `awaiting_human` — never a bare question exit
