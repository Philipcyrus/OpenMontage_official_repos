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
| Helper | `lib/i2v_duration.py` (`allocate_scene_durations`) | Allocate unequal audio-driven scene durations within the requested total band |
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
STILLS 2-TAKE on that still only. If it contains both characters, apply the binding PAIR SCALE
LOCK from `config/panda-elements.json` (customer 1.00, panda 0.58 ±0.05, shared ground plane,
upright canonical postures) and reject an off-scale result before the gate. Write the assets
checkpoint `status='awaiting_human'` with
**top-level** `partial_progress={"phase":"hero_still","hero_scene_id":"<id>","look_notes":[]}`
(not nested under `asset_manifest.metadata`) and STOP. Preview is the single PNG (not the
storyboard grid). On revise: update the one still + append the note to `look_notes`; re-checkpoint
with `phase:"hero_still"` and STOP. Do **not** generate remaining stills or video yet.

### 3. PHASE 1 — generate remaining STILLS under LOOK LOCK, then STOP (GATE 3, approve_stills)
After the hero is approved (or immediately when `hero_still` is off):
- When coming from an approved hero: **KEEP** the approved hero PNG. Generate only the other
  scenes. Import the hero as a **style/look** reference (not a start-frame that copies composition).
  Bake accumulated `look_notes` into every remaining prompt. Import the approved hero once and
  reuse that style/look media id across the remaining-stills wave.
- When `hero_still` is off: generate one still per scene as before.
- Preflight take 1 for **all** remaining scenes and enforce the budget before any submit. Then
  submit in waves with at most **4 Higgsfield jobs in flight** and poll each in-flight set
  together. If a submit is rate-limited / 429, reduce the cap to 2 for the rest of the leg.
  Do not serialize one scene's submit → poll before submitting the next scene.
- After all take-1 results return, run take 2 only for unusable results. Per remaining scene:
  same CHARACTER LOCK + 2D MEDIUM + STILLS 2-TAKE.
- For every panda+customer still, repeat the numeric PAIR SCALE LOCK in the generation prompt,
  then visually check ground-to-ear-top panda height against ground-to-head customer height.
  Accept only 0.53–0.63 with both feet on the same ground line and both canonical upright
  postures. Outside-range scale, depth tricks, crouching, or stretched anatomy is unusable and
  qualifies for take-2 i2i correction.

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

If both characters appear, the motion prompt must preserve the approved still's exact PAIR SCALE
LOCK, postures, body proportions, and shared ground plane through the final frame. Any apparent
growth/shrinkage, depth drift, crouch, or camera move that changes the ratio fails the sample.

This is the motion cost gate: the reviewer approves the motion feel (and lipsync when on)
**before** committing to the whole batch. Record the sample's Higgsfield **credits** on that
asset (`credits`, `credits_source: "actual"`), plus clip/narration `duration_seconds` when TTS
ran; when the lipsync path was used, note `[audio_lipsync:true]` in `generation_summary` (asset
rows must not invent an `audio_lipsync` property — schema is `additionalProperties: false`).
Then write the assets checkpoint `status='awaiting_human'` **AND
`partial_progress={"phase": "motion_sample"}`** and STOP. Generate **no other clips** yet.
Sample-scene VO files already on disk are reused in PHASE 3 (do not re-TTS unless revising that
line).

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
2. **Probe + allocate the effective timeline:** for each scene with narration, probe every VO
   (`audio_probe` / `ffprobe`) and calculate its scene-local start/end, including deliberate
   leading silence. After ALL VO is measured, call
   `lib.i2v_duration.allocate_scene_durations` once for the full scene set with:
   - the user's requested total (`script.total_duration_seconds`, falling back to the final
     `scene_plan` end);
   - `tolerance_fraction=0.05`;
   - approved scene-plan durations as pacing weights, not fixed equal slots;
   - measured scene-local audio bounds and the duration list from `models_explore`; and
   - the real transition overlap (normally zero / hard cuts for audio-lipsync scenes).

   An already-approved motion sample is immutable: pass its actual duration as
   `fixed_i2v_duration`; the allocator may assign it a bounded post-speech tail hold but must not
   regenerate it just to consume slack. Persist the complete returned object unchanged as
   `asset_manifest.metadata.timeline_contract`; keep `vo_duration_map` for backward compatibility.
   The allocator selects unequal supported i2v durations that keep the final cut within ±5% of
   the requested total, favoring useful visual breathing room over equal per-shot padding.

   If a scene's audio ends after the provider's maximum duration, retry that TTS once at the
   smallest speed increase needed (never above the existing 1.15 cap), re-probe, then allocate
   again. If it still cannot fit, do not submit that i2v or silently shorten/extend the master:
   checkpoint for `approve_assets` with `timeline_contract.status="pacing_revision_required"`
   and a question naming the scene/copy that needs revision.
3. **Motion clips:** animate remaining approved stills via Higgsfield MCP. Reuse the approved
   sample’s approach when it matches; **lipsync-eligible shots always use `seedance_2_0`** even
   if the sample was HOLD-only.

   **AUDIO LIPSYNC eligible** (`audio_lipsync` on — default — and scene speaker is
   `customer`|`panda`, produces video, not `text_card`):
   - MCP-upload still + one **timing-preserving scene-local VO bed**. For every source VO, compute
     `relative_at_s = section.start_seconds - scene.start_seconds`, then mix with `adelay` +
     `amix` semantics (reuse `tools.video.panda_render._premix_voice_tracks`) so leading silence,
     deliberate pauses, and overlapping dialogue remain on the timeline. Never join the files
     back-to-back. Keep the original section files for compose `voice_tracks` at the same
     absolute script timestamps.
   - `generate_video`: `start_image`, `audio_references`, `generate_audio:false`, the scene's
     allocated `timeline_contract.scenes[].i2v_duration`, aspect from the job.
   - Prompt: 2D + Element LOCK; lip-sync mouth/jaw to the attached audio; no walking / new
     person / photoreal.
   - Manifest row: `model: seedance_2_0`, `duration_seconds` = allocated i2v duration; put VO
     path / media ids and `[audio_lipsync:true]` in `generation_summary`. Do **not** add an
     `audio_lipsync` property on the asset row. After QA, eligibility lives under
     `metadata.lip_sync_qa.scenes.<scene_id>`.
   - On failure: HOLD LOCK fallback (next bullet), log in `decision_log`.

   **HOLD / ineligible** (`narrator`-only, `text_card`, non-speaking, or `audio_lipsync:false`):
   - i2v with **HOLD LOCK** (mouth frozen); 2D + locked characters; snapped `duration`.

   Non-speaking / `text_card` scenes: no TTS; use the allocated effective duration or static
   cards. Do not revert to equal scene lengths.
4. **Submit and poll as waves:** after every speaking scene has its TTS, duration, and optional
   timing-preserving bed ready, preflight **all pending i2v clips** and enforce the credit cap
   against that complete batch before any submit. Submit at most **4 jobs in flight** (2 after a
   rate-limit / 429), checkpoint every `scene_id` → `job_id` immediately under
   `metadata.partial_progress.motion_jobs`, then poll the set together. Ingest successes, retain
   them on partial failure, and report only failed scene ids; never replay the successful portion
   of a wave.
5. **Music** (if requested): start `music_gen` (ElevenLabs Music) after the i2v wave is submitted
   and while those jobs are in flight; keep it under the VO. TTS that drives speaking clips is
   not overlapped with i2v because it must exist before clip submission.

#### Lip-sync QA — mandatory before GATE 4

After all clips are ingested, review every `audio_lipsync:true` customer/panda clip with
`lipsync_qa` against the **exact scene-local VO bed uploaded to Seedance**. Narrator, HOLD,
text-card, and `audio_lipsync:false` clips are `skipped`, not failures.

1. Invoke `lipsync_qa` without `visual_observation`. It validates duration/offsets, detects active
   speech, and extracts dense frames before onset, through speech, and just after speech.
2. Read every returned frame. Re-invoke `lipsync_qa` with one honest `visual_observation`:
   `mouth_visible_ratio`, active/closed sample counts, number of distinct mouth shapes, observed
   mouth-motion onset, `pre_speech_mouth_state`, and an ordered `active_mouth_shapes` label for
   every active sample. Allowed labels are `closed`, `narrow`, `rounded`, `wide`, `teeth`, and
   `unclear`. Preserve frame order; do not summarize a long held smile as several invented shapes.
   Do not infer a pass from metadata or the generation prompt.
3. Apply the returned conservative status:
   - `pass`: mouth is visible in at least 80% of active samples; sustained passages contain at
     least three useful states including some closure, enough ordered transitions, no shape held
     through more than half the samples, and onset within 0.30s of speech.
   - `fail_timing`: articulation exists but mouth onset leads/lags speech by more than 0.30s.
   - `fail_generation`: concrete flat/closed/obscured articulation, continuous-open oscillation,
     two-shape/static-mouth behavior, pre-speech held-open smiles with weak subsequent motion, or
     incomplete clip coverage.
   - `inconclusive`: analysis or evidence is insufficient. Never spend automatically merely
     because the analysis tool failed.
4. Persist `rubric_version:"2.0"` and the report under
   `asset_manifest.metadata.lip_sync_qa.scenes.<scene_id>`. Copy the ordered mouth states and the
   tool-derived change count / longest static run into each attempt's evidence. Frame paths must
   stay under the project directory. The report is evidence for GATE 4 and final review.

#### Bounded correction policy

Handle each first-review failure exactly once:

- **Consistent timing offset (`fail_timing`):** store the tool's measured offset as
  `validated_audio_offset_seconds`, sample again with that expected offset, and keep the original
  clip. This local re-evaluation costs no credits. Only retain the correction when the second
  result passes; otherwise clear it and record an unresolved warning.
- **Poor articulation/visibility (`fail_generation`):** regenerate **only that scene, once**, with
  the same approved still, exact VO bed, model, duration, and Seedance audio reference. Amend only
  the motion prompt to require a face-visible medium close-up and meaningful mouth movement from
  the first speech onset. Run the normal Higgsfield `get_cost:true` and balance/budget checks
  before submit. Immediately checkpoint `scene_id`, attempt `2`, quoted credits, original asset id,
  and the returned job id under `metadata.partial_progress.lip_sync_retry`. Retain the original
  file and manifest row until the retry has been ingested and reviewed.
- **`inconclusive` or tool failure:** spend nothing. Keep the original and emit an unresolved
  warning.

Never retry a `pass`, never regenerate another scene as collateral, and never submit attempt 3.
For multiple failed scenes, each scene may receive one retry, but each submit must independently
pass the existing credit cap. After reviewing attempt 2, select `retry` only if its concrete
visibility, articulation, and absolute onset offset are better; otherwise retain `original`.
Persist both attempts, `retry_count` (0 or 1), retry job/credits, selected take, and any unresolved
warning. A second failure continues to GATE 4 and eventually `approve_final`; it cannot loop or
silently pass.

Static images cannot prove phoneme-perfect visemes. This QA is deliberately conservative: it
catches severe timing/closed-mouth failures and sends uncertain results to the human.

#### Pair scale + posture QA — mandatory for two-character scenes

Review the approved still and representative beginning/middle/end frames from every clip that
contains both panda + customer. Persist `asset_manifest.metadata.character_scale_qa`:

- the locked ratio (`human_height_units:1.0`, `panda_height_ratio:0.58`,
  `ratio_tolerance:0.05`) and shared-ground-plane requirement;
- one entry per two-character scene with `still_status` and `clip_status`
  (`pass` or `warning`) plus concise evidence notes;
- overall `pass`, `warning`, or `not_applicable`.

Pass only when the panda remains approximately 0.53–0.63 of standing customer height,
ground-to-ear-top vs ground-to-head, both feet share one ground line, and both retain their
canonical upright postures. Motion must preserve the approved still's apparent ratio across all
sampled frames. Do not spend a third still take or an extra motion retry solely for this check;
surface any remaining warning with scene ids at GATE 4.

Then write the assets checkpoint `status='awaiting_human'` **without** any phase marker and STOP.
The launcher surfaces this as the **approve_assets** gate. On "request revision" here, regenerate
only the flagged shots (`response.shots`) — if a speaking shot’s VO changes, re-probe and
re-snap `duration` (and re-upload `audio_references`) before re-running i2v.

Approving GATE 4 confirms the audio-driven `timeline_contract`, including unequal scene lengths.
Its output must be within ±5% of the requested total unless the human explicitly approves a
duration exception recorded in `decision_log`. Compose still **lays the same ElevenLabs VO**
under lipsync clips (`generate_audio:false` → silent clip; mouths already match that VO).

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
its scene), plus optional `prompt`/`model`/`cost_usd`/`duration_seconds` /
`generation_summary` / `voice_performance`. Asset rows are schema-strict
(`additionalProperties: false`) — never add `audio_lipsync`, `speaker`, bare `duration`, or
per-row `metadata`.

For narration assets always set `duration_seconds` from the probe and record the script section
via `voice_performance.source_section_id` (and the speaker name in `generation_summary` when
multi-voice). For video clips set `duration_seconds` to the Higgsfield `duration` used; note
lipsync vs HOLD in `generation_summary`. Persist top-level `metadata.vo_duration_map` and
`metadata.timeline_contract` when TTS-first ran, and `metadata.lip_sync_qa` after QA. Persist a
schema-valid `asset_manifest` (`version: "1.0"`) as part of the PHASE 3 checkpoint. On approval
the stage completes and the pipeline proceeds to edit/compose.

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
- For speaking scenes: narration exists **before** i2v; clip `duration_seconds` came from
  the full-scene audio-driven allocation; no active speech exceeds the chosen i2v duration
- `asset_manifest.metadata.timeline_contract` is schema-valid and within the requested ±5% band
  (or the assets checkpoint clearly requests a pacing revision before compose)
- Narration covers all script sections; music (if any) sits under the VO
- Checkpoint left in `awaiting_human` for the gate
- When AUDIO LIPSYNC is on: customer/panda video clips used `seedance_2_0` + `audio_references`
  + `generate_audio:false` (or logged HOLD fallback); lipsync noted in `generation_summary` and
  under `metadata.lip_sync_qa.scenes.<id>` (never an illegal per-row `audio_lipsync` field)
- Every two-character still and clip passes the 0.58 ±0.05 pair-scale, shared-ground-plane, and
  posture check, or its scene-specific warning is persisted and surfaced at GATE 4
- No Kling/Wav2Lip post-hoc; no `generate_audio:true` invented speech for brand VO