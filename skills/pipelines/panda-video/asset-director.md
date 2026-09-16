# Asset Director — Panda Video Pipeline

> Best-of-both: upstream's single asset-generation stage (everything recorded in
> `asset_manifest`), PLUS Panda **cost gates**. Human-reviewed phases — optional
> HERO STILL look-lock, then STILLS, then optional motion sample, then full media.

## When To Use

You have an approved `scene_plan` (with `required_assets` per scene) and the approved `script`.
Your job is to generate all media — stills, motion clips, narration, music — honoring Panda
brand + character consistency, recording everything in `asset_manifest`. Phases:
- **PHASE 0 (GATE 2.5 — approve_hero_still):** when the job option `hero_still` is on
  (**default on**; pass `false` to opt out), generate ONLY ONE hero still, then STOP.
- **PHASE 1 (GATE 3 — approve_stills):** generate remaining stills under LOOK LOCK from the
  approved hero (or all stills when `hero_still` is off), then STOP. No video yet.
- **PHASE 2 (GATE 3.5 — approve_motion_sample):** when the job option `motion_sample` is on
  (default **off**; pass `true` to opt in), animate ONE hero still into a single sample clip so
  the motion/animation is approved before the full batch, then STOP. Skipped when `motion_sample`
  is off (the default).
- **PHASE 3 (GATE 4 — approve_assets):** after the motion sample is approved (or straight after
  the stills when `motion_sample` is off), **TTS-first** then duration-driven i2v for remaining
  scenes (+ music), then STOP. When **AUDIO LIPSYNC** is on (default), customer/panda speaking
  clips use Seedance `audio_references` so mouths follow the ElevenLabs VO; narrator/text_card
  stay HOLD/static. Compose still lays the same VO bed.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifacts | `scene_plan`, `script` | What to generate + narration text |
| Style | `styles/panda.yaml` | On-brand look (image prompt prefix, negatives, anchors) |
| Elements | `config/panda-elements.json` | Panda/customer Element ids + narration voice ids |
| Helper | `lib/i2v_duration.py` (`snap_i2v_duration`) | Map measured VO seconds → Higgsfield `duration` + hold extend |
| Tools | `image_selector`, `higgsfield_mcp_video`, `seedance_video`, `elevenlabs_tts`, `audio_probe`, `music_gen` | Generation + VO duration probe |

## Process

### 1. Inventory required assets
Walk every scene in `scene_plan`. For each `required_assets` entry create an asset task
(`scene_id`, `type`, `description`, `source`, tool). This is the full generation worklist.
Expect **one image** `required_asset` per scene that needs a still — no base-plate + restack
chain as separate generates.

### 2. PHASE 0 — generate ONE HERO STILL, then STOP (GATE 2.5, approve_hero_still)
**Only when the `hero_still` job option is on (default on; pass `false` to opt out).**
Generate ONE still for the `hero_moment` scene (else scene 1). CHARACTER LOCK + 2D MEDIUM +
STILLS 2-TAKE on that still only. Write the assets checkpoint `status='awaiting_human'` with
**top-level** `partial_progress={"phase":"hero_still","hero_scene_id":"<id>","look_notes":[]}`
(not nested under `asset_manifest.metadata`) and STOP. Preview is the single PNG (not the
storyboard grid). On revise: update the one still + append the note to `look_notes`; re-checkpoint
with `phase:"hero_still"` and STOP. Do **not** generate remaining stills or video yet.

### 3. PHASE 1 — generate remaining STILLS under LOOK LOCK, then STOP (GATE 3, approve_stills)
After the hero is approved (or immediately when `hero_still` is off):
- When coming from an approved hero: **KEEP** the approved hero PNG. Generate only the other
  scenes. Import the hero as a **style/look** reference (not a start-frame that copies composition).
  Bake accumulated `look_notes` into every remaining prompt.
- When `hero_still` is off: generate one still per scene as before.
- Per remaining scene: same CHARACTER LOCK + 2D MEDIUM + STILLS 2-TAKE.

**Generate NO video and NO audio yet.** Then write the assets checkpoint with
`status='awaiting_human'` **and top-level `partial_progress={"phase": "stills"}`**
(not nested under `asset_manifest.metadata`) and STOP.
The launcher surfaces this as the **approve_stills** gate (storyboard grid preview).
Do **not** mark the assets stage `completed` at this point — that skips the storyboard gate.

On "request revision" at this gate, honor `mode` (`fresh` | `edit`). Each flagged shot gets a
**new** 2-take budget; still honor LOOK LOCK from the approved hero. Replace only flagged
files + `asset_manifest` rows. Re-checkpoint with **top-level**
`partial_progress={"phase":"stills"}`. Do **not** proceed to video until the stills are approved.

### 4. PHASE 2 — MOTION SAMPLE (one hero clip), then STOP (GATE 3.5, approve_motion_sample)
**Only when the `motion_sample` job option is on (default off; pass `true` to opt in).** After the stills are approved,
pick ONE representative **hero** still (the most important scene, else scene 1).

**TTS-first for the sample scene when it is narrated.** Before calling Higgsfield:
1. Generate that scene’s narration via `elevenlabs_tts` (VOICE CAST ids; one file per speaking
   section bound to the sample scene).
2. Probe each file with `audio_probe` (or `ffprobe`); sum section durations for the scene.
3. Confirm allowed durations with MCP `models_explore`, then call
   `snap_i2v_duration(vo_seconds, allowed=…)` from `lib/i2v_duration.py` (or apply the same
   rules: shortest allowed duration ≥ ceil(VO); if VO exceeds model max, use max and note
   `hold_extend_seconds`).
4. Pass that integer as Higgsfield `duration`.

If the sample scene has **no** narration, use the scene-plan slot length snapped the same way
(plan estimate only).

Then animate the hero still into a **single** sample clip via the Higgsfield MCP bridge.
**Motion path depends on AUDIO LIPSYNC** (job option `audio_lipsync`, default **on**):

- **Eligible sample** (speaker `customer`|`panda`, video clip, not `text_card`): model
  `seedance_2_0`; MCP-upload still + VO; `generate_video` with `start_image`,
  `audio_references`=VO, `generate_audio:false`, snapped `duration`. Prompt: 2D + Element LOCK;
  lip-sync mouth/jaw to the attached audio; subtle idle — no walking, no new person, no
  photoreal/3D. Do **not** mouth-freeze. Confirm `models_explore` lists `audio_references`.
  On failure: fall back to HOLD LOCK i2v (below), log in `decision_log`.
- **Ineligible / lipsync off:** HOLD LOCK the 2D still — mouth/face frozen; tiny idle only —
  same locked characters; do not invent a new person or make the clip 3D / photoreal.

This is the motion cost gate: the reviewer approves the motion feel (and lipsync when on)
**before** committing to the whole batch. Record the sample's Higgsfield **credits** on that
asset (`credits`, `credits_source: "actual"`), plus `duration` / narration `duration_seconds`
when TTS ran; set clip metadata `audio_lipsync: true` when that path was used. Then write the
assets checkpoint `status='awaiting_human'` **AND `partial_progress={"phase": "motion_sample"}`**
and STOP. Generate **no other clips** yet. Sample-scene VO files already on disk are reused in
PHASE 3 (do not re-TTS unless revising that line).

On "request revision" here, regenerate ONLY the sample clip per the feedback (adjust motion prompt /
model / motion params; keep the same measured `duration` unless VO was revised), keep
`partial_progress.phase="motion_sample"`, and STOP again. Do not batch the rest until the motion
is approved (max ~3 sample iterations, then escalate).

> If `motion_sample` is off, skip this phase entirely — go straight from approved stills to PHASE 3.

### 5. PHASE 3 — TTS-first, then duration-driven i2v (+ audio lipsync) + music, then STOP (GATE 4)
After the motion sample is approved (or straight after the stills when `motion_sample` is off).
**Order is mandatory for speaking scenes: narration → probe → snap duration → i2v.** Do not
generate motion clips before the VO that drives their length (and mouths) exists.

1. **Narration (all remaining script sections):** `elevenlabs_tts` **per script section** using
   the voice id from the **VOICE CAST** map in this leg's prompt. Resolve id by
   `section.speaker` (or the cast's default speaker when `speaker` is omitted). Output one file
   per section, e.g. `vo-{section_id}-{speaker}.mp3`. Do not merge multi-speaker dialogue into a
   single TTS call. Skip sections already generated for the motion-sample scene unless revising.
   Narration generated with any id not in the cast map is a defect; do not ship it.
2. **Probe + map per scene:** for each scene with narration, sum probed VO durations
   (`audio_probe` / `ffprobe`). Call `snap_i2v_duration` (allowed list from `models_explore`).
   Record per scene in `decision_log` and/or `asset_manifest.metadata.vo_duration_map`:
   `{scene_id, vo_seconds, i2v_duration, hold_extend_seconds}`. When
   `hold_extend_seconds > 0`, note a PACING / hold-extend so edit extends on-screen hold past
   the clip. Prefer updating effective scene slot length in metadata for edit rather than
   rewriting the approved `scene_plan` JSON.
3. **Motion clips:** animate remaining approved stills via Higgsfield MCP. Reuse the approved
   sample’s approach when it matches; **lipsync-eligible shots always use `seedance_2_0`** even
   if the sample was HOLD-only.

   **AUDIO LIPSYNC eligible** (`audio_lipsync` on — default — and scene speaker is
   `customer`|`panda`, produces video, not `text_card`):
   - MCP-upload still + VO (if multi-speaker on one clip: concat VO files in timeline order into
     one temp bed for `audio_references`; keep original section files for compose
     `voice_tracks`).
   - `generate_video`: `start_image`, `audio_references`, `generate_audio:false`, snapped
     `duration`, aspect from the job.
   - Prompt: 2D + Element LOCK; lip-sync mouth/jaw to the attached audio; no walking / new
     person / photoreal.
   - Manifest: `audio_lipsync: true`, `model: seedance_2_0`, VO path / media ids.
   - On failure: HOLD LOCK fallback (next bullet), log in `decision_log`.

   **HOLD / ineligible** (`narrator`-only, `text_card`, non-speaking, or `audio_lipsync:false`):
   - i2v with **HOLD LOCK** (mouth frozen); 2D + locked characters; snapped `duration`.

   Non-speaking / `text_card` scenes: no TTS; use plan timings or static cards.
4. **Music** (if requested): `music_gen` (ElevenLabs Music), kept under the VO.

Then write the assets checkpoint `status='awaiting_human'` **without** any phase marker and STOP.
The launcher surfaces this as the **approve_assets** gate. On "request revision" here, regenerate
only the flagged shots (`response.shots`) — if a speaking shot’s VO changes, re-probe and
re-snap `duration` (and re-upload `audio_references`) before re-running i2v.

Approving GATE 4 **implies** the VO-overrun default for compose: if narration still overruns a
scene slot (PACING RISK — e.g. hold extend not applied, or music-only pad), extend that scene's
on-screen hold so locked CTA copy finishes — do not treat approval as leaving an open a/b/c
question for the edit leg. With TTS-first, most slots should already fit; overrun extend is the
safety net. Compose still **lays the same ElevenLabs VO** under lipsync clips (`generate_audio:false`
→ silent clip; mouths already match that VO).

### 6. Character and voice consistency
The panda mascot must look identical across every still/clip. Always attach the panda master
Element id from `config/panda-elements.json` in the **media slot**; use the customer Element
for the customer. Never invent a new panda or human. See CHARACTER LOCK in
`skills/meta/higgsfield-mcp-bridge.md`.

**VOICE CAST — the narration counterpart of CHARACTER LOCK.** Voices are chosen from
`config/panda-elements.json`, never improvised. The launcher resolves all three brand speakers
for the job language and interpolates the **literal voice ids** into every prompt whose leg can
call ElevenLabs.

- Use the cast map: `section.speaker` → id. Untagged sections use the default speaker named in
  the cast line (`options.narrator`).
- If the prompt says **VOICE CAST — OVERRIDE**, use that single `voice_id` for every line.
- If the prompt says **VOICE CAST — BLOCKER**, one or more speaker/language pairs have no
  configured voice. Do **not** improvise, do **not** borrow a neighbouring language, and do **not**
  fall back to Higgsfield `seed_audio` to get past it. Generate everything else, then stop at the
  gate and name the unconfigured pair(s) in the question.
- The one permitted fallback is ElevenLabs **itself** being unavailable — an infrastructure
  failure, never a missing id — and it must be recorded in `decision_log`.

### 7. Build the asset_manifest (in PHASE 3)
Record EVERY generated file canonically: per asset `id`, `type` (`image|video|audio|narration|
music|...`), `path` (relative to the project dir), `source_tool`, `scene_id` (bind each asset to
its scene), plus optional `prompt`/`model`/`cost_usd`/`duration_seconds`. For narration assets
also record `speaker` and the script `section` id in metadata when multi-voice, and always set
`duration_seconds` from the probe. For video clips set `duration` / `duration_seconds` to the
Higgsfield `duration` used. Persist `metadata.vo_duration_map` (per-scene snap results) when
TTS-first ran. Persist a schema-valid `asset_manifest` (`version: "1.0"`) as part of the PHASE 3
checkpoint. On approval the stage completes and the pipeline proceeds to edit/compose.

**Record Higgsfield credits (for the per-project cost report).** For every Higgsfield-generated
asset (stills via `generate_image`, clips via image_to_video), you already run the `get_cost:true`
preflight before spending (see `skills/meta/higgsfield-mcp-bridge.md`). Write that credit number
into the asset's manifest entry as **`credits`** (the number), **`credits_source: "actual"`**, and
**`provider: "higgsfield"`**. This is the ONLY place real credits are captured — do not skip it.
If you ever generate without a get_cost value, still set `credits` to your best estimate and mark
`credits_source: "estimated"`. (ElevenLabs voice/music usage is captured automatically by the
tools — you do not need to record it.)

## Handoff to `edit` / `compose`
`compose` assembles the approved assets into a CLEAN (unbranded) master via `panda_render`.
Branding is a separate, on-demand `panda_brand` step applied only after final approval.

## Success criteria
- Every required asset exists on disk and appears in `asset_manifest` with `path` + `scene_id`
- Stills/clips on-brand and character-consistent (panda Elements attached as media)
- For speaking scenes: narration exists **before** i2v; clip `duration` came from
  `snap_i2v_duration` (or equivalent); `hold_extend_seconds` noted when VO exceeds model max
- Narration covers all script sections; music (if any) sits under the VO
- Checkpoint left in `awaiting_human` for the gate
- When AUDIO LIPSYNC is on: customer/panda video clips used `seedance_2_0` + `audio_references`
  + `generate_audio:false` (or logged HOLD fallback); metadata `audio_lipsync: true` on success
- No Kling/Wav2Lip post-hoc; no `generate_audio:true` invented speech for brand VO