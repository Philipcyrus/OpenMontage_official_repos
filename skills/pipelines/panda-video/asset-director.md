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
  scenes (+ music), then STOP. When **AUDIO LIPSYNC** is on (default), every on-screen
  customer/panda line is its own **speaking subshot** — a Seedance clip that **speaks the quoted
  line itself** (`generate_audio:true`, native lip-sync), re-voiced into the cast voice with
  `elevenlabs_voice_changer` — and narrator lines are closed-mouth fills; narrator-only scenes
  and text_card stay HOLD/static. Compose lays each re-voiced line (and each narrator ElevenLabs
  file) at its subshot.

  Seedance `audio_references` does **not** drive the mouth (job_cfb6fd099504: the mouth moved
  generically, uncorrelated with the attached line). Native speech is the only path that has
  produced real lip-sync, and speech-to-speech keeps its timing exactly while swapping in the
  brand voice.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifacts | `scene_plan`, `script` | What to generate + narration text |
| Style | `styles/panda.yaml` | On-brand look (image prompt prefix, negatives, anchors) |
| Elements | `config/panda-elements.json` | Panda/customer Element ids + narration voice ids |
| Helper | `lib/i2v_duration.py` (`build_scene_subshots`, `allocate_scene_durations`) | Split each scene into sequential per-speaker subshots, then allocate unequal audio-driven scene durations around the measured-dialogue target |
| Tools | `image_selector`, `higgsfield_mcp_video`, `seedance_video`, `elevenlabs_tts`, `elevenlabs_voice_changer`, `audio_probe`, `music_gen` | Generation, native-speech re-voicing + transcript check, VO duration probe |

## Process

### 1. Inventory required assets
Walk every scene in `scene_plan`. For each `required_assets` entry create an asset task
(`scene_id`, `type`, `description`, `source`, tool). This is the full generation worklist.
Expect **one image** `required_asset` per scene that needs a still — no base-plate + restack
chain as separate generates.

### 1b. User screenshots (only when the prompt has a USER SCREENSHOTS block)
- `source: "provided"` items are the user's screenshots. They are **not** asset tasks: never
  generate, edit, animate or upload them, never copy them into `assets/`, never add them to
  `asset_manifest`. `screen_overlay` places them at compose.
- Their scenes still get the normal generated still (hero included) and clip. The prompt facts list,
  per scene, the area to keep plain and where the character stands. Put both into the still prompt
  and the clip prompt (locked camera). **Beside:** leave the screenshot area plain white empty.
  **Held:** leave the phone screen rect solid plain white blank; keep character + phone in the
  subject area; no pan/zoom that moves the blank screen.
- The launcher checks those areas on every still and clip and flags any that are not empty / not
  white enough; regenerate only the flagged one with the area empty (beside) or blank white (held).

### 2. PHASE 0 — generate ONE HERO STILL, then STOP (GATE 2.5, approve_hero_still)
**Only when the `hero_still` job option is on (default on; pass `false` to opt out).**
Generate ONE still for the `hero_moment` scene (else scene 1). CHARACTER LOCK + 2D MEDIUM +
STILLS 2-TAKE on that still only. If it contains both characters, apply the binding PAIR SCALE
LOCK from `config/panda-elements.json` (customer 1.00, panda 0.58 ±0.05, shared ground plane,
upright canonical postures) and reject an off-scale result before the gate. Write the assets
checkpoint `status='awaiting_human'` with
**top-level** `partial_progress={"phase":"hero_still","hero_scene_id":"<id>","look_notes":[]}`
(not nested under `asset_manifest.metadata`) and STOP. **`artifacts.stills` MUST list the hero
PNG basename** (e.g. `["sc1_hero.png"]`). A full schema-valid `asset_manifest` is **not**
required at this phase — it lands at PHASE 3 / `approve_assets`. Preview is the single PNG (not the
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
- Pass `aspect_ratio` from `scene_plan.metadata.aspect_ratio` / `options.aspect_ratio`
  (default `9:16`) to every `generate_image` call. Do not silently switch canvases.
- For every panda+customer still, repeat the numeric PAIR SCALE LOCK in the generation prompt,
  then visually check ground-to-ear-top panda height against ground-to-head customer height.
  Accept only 0.53–0.63 with both feet on the same ground line and both canonical upright
  postures. Outside-range scale, depth tricks, crouching, or stretched anatomy is unusable and
  qualifies for take-2 i2i correction.

**Generate NO video and NO audio yet.** Then write the assets checkpoint with
`status='awaiting_human'` **and top-level `partial_progress={"phase": "stills"}`**
(not nested under `asset_manifest.metadata`) and STOP.
**`artifacts.stills` MUST list every storyboard PNG basename** (including the approved hero).
A full schema-valid `asset_manifest` is required at PHASE 3 / `approve_assets`, not at this
storyboard pause.
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

- **Eligible sample** (speaker `customer`|`panda`, video clip, not `text_card`): generate it
  exactly as a PHASE 3 **speaking subshot** (native speech, below) — for a multi-speaker scene,
  its first speaking subshot only — then re-voice it with `elevenlabs_voice_changer`. Do **not**
  mouth-freeze. On failure: fall back to HOLD LOCK i2v (below), log in `decision_log`.
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
**Order is mandatory: narration TTS → probe → subshots (native estimates) → speaking clips →
re-voice + measure → rebuild subshots → allocate → fill / HOLD clips.** A native speaking clip
sets its own line length, so nothing downstream of it is allocated until it has been measured.

1. **Narration (all remaining script sections):** `elevenlabs_tts` **per script section** using
   the voice id from the **VOICE CAST** map in this leg's prompt. Resolve id by
   `section.speaker` (or the cast's default speaker when `speaker` is omitted). Output one file
   per section, e.g. `vo-{section_id}-{speaker}.mp3`. Do not merge multi-speaker dialogue into a
   single TTS call. Skip sections already generated for the motion-sample scene unless revising.
   Narration generated with any id not in the cast map is a defect; do not ship it.
   On-screen dialogue TTS is kept as the HOLD fallback only; with native speech it never plays.
2. **Probe, split into subshots, generate speaking clips, then allocate the effective timeline:**
   probe every VO (`audio_probe` / `ffprobe`). For every scene with an on-screen
   `customer`/`panda` line (when `audio_lipsync` is on), call
   `lib.i2v_duration.build_scene_subshots(scene_id, sections,
   scene_script_start_seconds=scene.start_seconds, allowed_durations=<models_explore list>)` with
   one entry per section (`section_id`, `speaker`, `measured_seconds`, `script_start_seconds`,
   `script_end_seconds`, `path`, **`text`** = the script line). It lays the lines end to end: each
   starts after the previous line's **measured** end plus the script's pause when that pause is
   ≥ 0.4s (otherwise a 0.25s breath), so a long line pushes the next one later instead of playing
   on top of it. Script timestamps are the order of lines, not their clocks.

   Speaking subshots default to `speech_mode="native"`: their `vo_seconds` / `i2v_duration` are
   an **estimate** of Seedance's own pacing (≈0.25s lead-in + 2.6 words/s — far slower than the
   ElevenLabs reading; `native_speech_estimated: true`). Generate every speaking subshot now
   (step 3, first wave), re-voice each one, then **rebuild** each scene with, per speaking
   section, `generated_i2v_duration` = the clip length actually generated, `measured_seconds` =
   the re-voiced `speech_end_s`, and `path` = the re-voiced file. Persist the rebuilt result
   unchanged under `asset_manifest.metadata.scene_subshots.<scene_id>`.

   After ALL VO is measured (narrator TTS + re-voiced dialogue), call
   `lib.i2v_duration.allocate_scene_durations` once for the full
   scene set. For a subshot scene pass `subshot_content_seconds=<content_duration_seconds>` (it
   is several cuts, so it gets an exact length plus a closed-mouth `tail_fill_seconds`, and its
   `i2v_duration` is null). Single-clip scenes (narrator-only HOLD, text_card) keep
   `audio_end_seconds` + `allowed_durations` as before. Call it with:
   - `target_duration_seconds=ceil(sum of measured VO seconds)` — measured dialogue is the
     runtime; the user's requested total is not a candidate;
   - `tolerance_fraction=0.05`;
   - approved scene-plan durations as pacing weights, not fixed equal slots;
   - measured scene-local audio bounds and the duration list from `models_explore`; and
   - the real transition overlap (normally zero / hard cuts for audio-lipsync scenes).

   An already-approved motion sample is immutable: pass its actual duration as
   `fixed_i2v_duration`; the allocator may assign it a bounded post-speech tail hold but must not
   regenerate it just to consume slack. Persist the complete returned object unchanged as
   `asset_manifest.metadata.timeline_contract`; keep `vo_duration_map` for backward compatibility.

   **Dialogue duration priority:** measured ElevenLabs VO is the runtime. Pass
   `target_duration_seconds=ceil(sum of measured VO seconds)` — never
   `max(requested_total, …)` and never a previous `timeline_contract.minimum_duration_seconds`.
   The requested total and scene-plan durations are pacing weights only. When the target differs
   from the brief, append a `decision_log` entry (`category: "pacing"`, `subject:
   "dialogue_priority"`) and continue i2v — do **not** stop at any gate and do **not** reopen
   `approve_stills`, whether speech runs longer or shorter than the brief. If allocate returns
   `pacing_revision_required` only because each scene was rounded up to a supported clip length,
   re-allocate once with `target_duration_seconds=<that result's output_duration_seconds>` and
   continue. The allocator still selects unequal supported i2v durations, favoring useful visual
   breathing room over equal per-shot padding.

   The **only** pacing stop is provider max: if a scene's audio ends after the provider's
   maximum duration (`DurationAllocationError`), retry that TTS once at the smallest speed
   increase needed (never above the existing 1.15 cap), re-probe, then allocate again. If it
   still cannot fit, do not submit any i2v or silently shorten the master: write
   `timeline_contract.status="pacing_revision_required"` with
   `provider_max_scenes=[<scene ids>]`, checkpoint `approve_assets` (`status="awaiting_human"`,
   **no** `partial_progress.phase="stills"`), and STOP. The launcher asks the human one question
   for those scenes: approve a minimal narration trim (prices, plan names and the CTA verbatim)
   or revise with replacement wording.
3. **Motion clips:** animate remaining approved stills via Higgsfield MCP. Reuse the approved
   sample’s approach when it matches; **lipsync-eligible shots always use `seedance_2_0`** even
   if the sample was HOLD-only.

   **Speaking subshot** (`audio_lipsync` on — default — and `kind: "speaking"`, i.e. an
   on-screen `customer`|`panda` line in a video scene, not `text_card`). Generate **only the
   speaking portion**, one clip per line, with **native speech**:
   - MCP-upload the approved still only. **No `audio_references`** — an attached ElevenLabs line
     does not drive the mouth. Never a scene bed, never narrator audio, never another speaker.
   - `generate_video`: model `seedance_2_0`, `start_image`, **`generate_audio:true`**, the
     subshot's `i2v_duration`, and **`aspect_ratio` from `scene_plan.metadata.aspect_ratio` /
     `options.aspect_ratio`** (default `9:16`). Never silently switch to another canvas; confirm
     the model supports it via `models_explore`. Compose cuts the clip to `duration_seconds`; the
     unused tail is discarded, never held over the next line.
   - Prompt: 2D + Element LOCK; static locked-off camera; the speaker "speaks right away **in
     English** at a brisk, natural conversational pace" (name the job language — without it
     Seedance translated an English line into Mandarin) with a voice description fitting the
     speaker (young woman for `customer`; warm, friendly for `panda`); then **`<Speaker> says,
     in English: "<line>"`** with the line quoted exactly as
     `tools.audio.elevenlabs_voice_changer.spoken_form(text, script.pronunciation_guides)`
     returns it; "**says only this one sentence, word for word, then stops talking; silence
     after the line**" (without it Seedance ad-libbed a second sentence); "the mouth forms every
     word in sync with the speech and closes when the line ends"; the listener "listens with
     lips closed, small blink only"; and "no other voices, no music, no sound effects, no text".
     One line per clip.
   - **Brand words must be respelled in the quote.** Seedance spells capitalised tokens letter
     by letter ("eSIM" was heard as "E-S-I-M"). Every brand token the narrator or a character
     says goes into `script.pronunciation_guides` (`{"word": "eSIM", "phonetic": "e-sim"}`); if
     the script lacks one for a mixed-case or all-caps token, add it and log it in
     `decision_log`. Captions keep the written form.
   - **Re-voice immediately after ingest:** `elevenlabs_voice_changer.execute({source_path:
     <clip>, voice_id: <VOICE CAST id for that speaker>, output_path:
     "assets/audio/native/<subshot_id>.<speaker>_voice.mp3", expected_text: <script line>,
     pronunciation_guides: <script guides>})`. It keeps Seedance's timing exactly (so the mouth
     still matches) and returns `speech_start_s`, `speech_end_s` and `transcript_match`. A
     `transcript_match.ok: false` (dropped words or a garbled brand word in
     `guarded_missing`) is a **`fail_generation`** for that subshot.
   - **The speaking prompt overrides the scene plan's `movement` and mood.** Do not paste the
     scene's action into it. For the whole clip: the speaker faces camera (front or 3/4, both
     eyes and the mouth visible) and stays in place — no walking, turning away, exits, nodding,
     laughing or sweeping gestures; the listener's lips stay **closed** (a closed-lip smile at
     most — never an open grin, laugh or talking shape). Show delight/surprise with eyes, brows
     and small hand motion, not an open-mouth expression. Walking, exits and big gestures belong
     in a closed-mouth fill before or after the line. A cheerful scene is where this goes wrong:
     an open-mouth grin on both characters reads as both talking and swamps the word shapes.
   - Manifest rows: the clip (`id` = the `subshot_id`, `scene_id` = the scene, `model:
     seedance_2_0`, `duration_seconds` = the generated length,
     `voice_performance.source_section_id` = the line's section, `[audio_lipsync:true]
     [speech:native]` plus the quoted line in `generation_summary`) **and** the re-voiced line
     (`type: "narration"`, `source_tool: "elevenlabs_voice_changer"`, `duration_seconds` =
     `speech_end_s`, `voice_performance.source_section_id`, transcript in
     `generation_summary`). Do **not** add an `audio_lipsync` property on any asset row. After
     QA, the review lives under `metadata.lip_sync_qa.subshots.<scene_id>.<section_id>`.
   - On generation failure: HOLD LOCK fallback for that subshot only, using the dialogue TTS
     file; log in `decision_log`.

   **Fill subshot** (`kind: "fill"` — narrator lines inside a dialogue scene, leading silence,
   and the scene's `tail_fill_seconds`). The mouth stays closed and no fill ever speaks:
   - `fill_treatment: "still"` (shorter than one provider duration): use the approved still.
     No generation.
   - `fill_treatment: "still_or_closed_mouth_motion"`: prefer the still; spend one HOLD LOCK
     closed-mouth reaction or neutral-motion clip only when the beat needs visible listening.
     Never a lipsync generation.

   **HOLD / single-clip** (narrator-only scene, `text_card`, non-speaking, or
   `audio_lipsync:false`):
   - i2v with **HOLD LOCK** (mouth frozen); 2D + locked characters; snapped `duration`.

   Non-speaking / `text_card` scenes: no TTS; use the allocated effective duration or static
   cards. Do not revert to equal scene lengths.
4. **Submit and poll as waves:** preflight **all pending i2v clips** (speaking subshots at their
   estimated durations, any closed-mouth fill clips, and HOLD scenes at the planned durations)
   and enforce the credit cap against that complete batch before any submit. Speaking subshots
   go first; fill and HOLD clips are submitted after allocation (their quoted lengths may change
   by one step — re-quote only those).
   Submit at most **4 jobs in flight** (2 after a rate-limit / 429), checkpoint every
   `subshot_id` (or `scene_id` for single-clip scenes) → `job_id` immediately under
   `metadata.partial_progress.motion_jobs`, then poll the set together. Ingest successes, retain
   them on partial failure, and report only failed ids; never replay the successful portion of
   a wave.
5. **Music** (if requested): start `music_gen` (ElevenLabs Music) after the i2v wave is submitted
   and    while those jobs are in flight; keep it under the VO.

#### Lip-sync QA — mandatory before GATE 4

After all clips are ingested, check **every speaker independently**: run `lipsync_qa` once per
speaking subshot (`audio_lipsync:true`) with `video_path` = that subshot's clip, `audio_path` =
that speaker's own VO file (the re-voiced `assets/audio/native/<subshot_id>.*_voice.mp3`, which
carries the clip's own speech timing), `scene_id`, `section_id`, and `speaker`. Never review
against a scene mix: a mouth that moves during the narrator or the other character is not
tracking its own line, and must not pass. Narrator fills, HOLD, text-card, and
`audio_lipsync:false` clips are `skipped`, not failures. The words are checked separately by the
re-voice step's `transcript_match`; record it in the subshot's report notes.

1. Invoke `lipsync_qa` without `visual_observation`. It validates duration/offsets, detects active
   speech in that one line, and extracts frames every 0.25s from before onset to just after the
   last word, each labelled with a `phase` (`pre`, `onset`, `active`, `pause`, `tail`, `post`).
2. Read every returned frame — all of them, not the first few. Re-invoke `lipsync_qa` with one
   honest `visual_observation`: `mouth_visible_ratio` (a face turned away or in profile with the
   mouth hidden counts as not visible), active/closed sample counts, `distinct_mouth_shapes` (a
   smile or grin held unchanged is **one** shape), `tail_active_samples` /
   `tail_closed_mouth_samples` (frames with `phase: tail`), `listener_visible_samples` /
   `listener_open_mouth_samples` (the other character's lips parted in active/tail frames),
   observed mouth-motion onset, and notes. Look at the listener in every frame, not only the
   speaker. Do not infer a pass from metadata or the generation prompt.
3. Apply the returned conservative status:
   - `pass`: mouth is visible in at least 80% of active samples, has at least two clearly distinct
     shapes, is not closed for half the active samples or half the tail samples, the listener's
     lips are parted in under 30% of their visible samples, and the mouth starts within 0.20s of
     speech.
   - `fail_timing`: articulation exists but mouth onset leads/lags speech by more than 0.20s.
   - `fail_generation`: concrete flat/closed/obscured articulation, a mouth that stops before the
     line ends, an open-mouthed listener, or incomplete clip coverage.
   - `inconclusive`: analysis or evidence is insufficient. Never spend automatically merely
     because the analysis tool failed.
4. Persist each report under
   `asset_manifest.metadata.lip_sync_qa.subshots.<scene_id>.<section_id>` with its `subshot_id`,
   `speaker`, and `audio_path` (legacy single-clip scenes keep
   `metadata.lip_sync_qa.scenes.<scene_id>`). Counts and `retry_count` are per subshot. Frame
   paths must stay under the project directory. The report is evidence for GATE 4 and final
   review.

#### Bounded correction policy

Handle each first-review failure exactly once, per speaking subshot:

- **Consistent timing offset (`fail_timing`):** store the tool's measured offset as that
  subshot's `validated_audio_offset_seconds`, sample again with that expected offset, and keep
  the original clip. This local re-evaluation costs no credits. Only retain the correction when
  the second result passes; otherwise clear it and record an unresolved warning. The correction
  belongs to that one line: never shift a sibling line, the narrator, or the whole scene to fix
  one mouth.
- **Poor articulation/visibility or wrong words (`fail_generation`):** regenerate **only that
  subshot, once**, with the same approved still, the same quoted line, model, duration and
  `generate_audio:true`, then re-voice it again. Amend only the motion prompt to require a
  face-visible medium close-up, meaningful mouth movement from the first word to the last, the
  speaker standing still facing camera, and the listener's lips closed (for a transcript
  failure, also respell the misheard word). Run the normal Higgsfield
  `get_cost:true` and balance/budget checks before submit. Immediately checkpoint `scene_id`,
  `subshot_id`, attempt `2`, quoted credits, original asset id, and the returned job id under
  `metadata.partial_progress.lip_sync_retry`. Retain the original file and manifest row until the
  retry has been ingested and reviewed.
- **`inconclusive` or tool failure:** spend nothing. Keep the original and emit an unresolved
  warning.

Never retry a `pass`, never submit attempt 3, and never regenerate another subshot or scene as
collateral. For multiple failed subshots, each may receive one retry, but each submit must
independently pass the existing credit cap. After reviewing attempt 2, select `retry` only if its
concrete visibility, articulation, and absolute onset offset are better; otherwise retain
`original`. Persist both attempts, `retry_count` (0 or 1), retry job/credits, selected take, and
any unresolved warning. A second failure continues to GATE 4 and eventually `approve_final`; it
cannot loop or silently pass.

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
only the flagged shots (`response.shots`) — if a line's wording changes, regenerate only its
speaking subshot with the new quoted line, re-voice it, rebuild that scene's subshots and
re-allocate.

Approving GATE 4 confirms the audio-driven `timeline_contract`, including unequal scene lengths.
Its output must be within ±5% of `timeline_contract.target_duration_seconds` (the measured
dialogue length, logged as `dialogue_priority` when it differs from the brief). Compose mutes
each clip's native audio and **lays each re-voiced line / narrator ElevenLabs file** at its own
subshot (each mouth was generated with that exact speech).

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
lipsync vs HOLD in `generation_summary`. Persist top-level `metadata.vo_duration_map`,
`metadata.scene_subshots`, and `metadata.timeline_contract` when TTS-first ran, and
`metadata.lip_sync_qa` after QA. Persist a
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
- For speaking scenes: narration exists **before** i2v; every on-screen line is its own
  speaking subshot whose clip `duration_seconds` covers that one line; no two lines overlap on
  the scene timeline
- `asset_manifest.metadata.timeline_contract` is schema-valid and within the requested ±5% band
  (or the assets checkpoint clearly requests a pacing revision before compose)
- Narration covers all script sections; music (if any) sits under the VO
- Checkpoint left in `awaiting_human` for the gate
- When AUDIO LIPSYNC is on: each speaking subshot used `seedance_2_0` + `generate_audio:true`
  with only its own quoted line (brand words respelled), was re-voiced into its VOICE CAST id
  with `elevenlabs_voice_changer`, and passed `transcript_match` (or a logged HOLD fallback);
  lipsync noted in `generation_summary` and under
  `metadata.lip_sync_qa.subshots.<scene_id>.<section_id>` (never an illegal per-row
  `audio_lipsync` field); no narrator line was ever spoken by a clip
- Every two-character still and clip passes the 0.58 ±0.05 pair-scale, shared-ground-plane, and
  posture check, or its scene-specific warning is persisted and surfaced at GATE 4
- No Kling/Wav2Lip post-hoc; `generate_audio:true` only on speaking subshots, and that native
  speech never ships un-converted (narrator lines are always ElevenLabs TTS)