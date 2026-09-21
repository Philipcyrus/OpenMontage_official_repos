# Scene-Plan Director — Panda Video Pipeline

> Upstream-faithful, Panda-tuned. Mirrors the rigor of `pipelines/explainer/scene-director.md`,
> but for character-mascot brand videos. This stage produces a **structured text scene plan only**
> — it generates **no media**. All stills, clips, voice and music are produced later, in the
> `assets` stage. "A great script with a bad scene plan produces a confusing video."

## When To Use

You are turning the approved `script` into a **structured scene plan** (GATE 2). You write the
schema-valid `scene_plan` artifact, then **STOP for human approval**. You do **not** generate
stills, contact sheets, images, video or audio here, and you have **no generation tools**. Each
scene instead *declares* what it needs via `required_assets`, which the `assets` stage fulfils.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/scene_plan.schema.json` | Artifact validation (text only) |
| Prior artifact | `state.artifacts["script"]["script"]` | The approved narrative / beats / narration |
| Style | `styles/panda.yaml` | On-brand look — captured as PLAN requirements, not generated here |
| Elements | `config/panda-elements.json` | Panda/customer character element ids + narration voice ids |

> No web research needed — the Panda visual language is brand-locked (`styles/panda.yaml`). Plan
> from the brand + script, not from discovered techniques.

## Process

### 1. Analyze the script
Read every section/beat. For each note: the concept/message, the emotional beat (curiosity,
delight, reassurance, urgency, CTA), the enhancement cues the writer embedded, and the time
available (`end_seconds - start_seconds`).

### 2. Decompose into scenes
Transform script sections into scenes (a distinct visual moment each — avoid one static scene
per long section). Set `id`, `type` (one of the 9 canonical types: `talking_head`, `broll`,
`animation`, `character_scene`, `diagram`, `text_card`, `transition`, `generated`,
`screen_recording`), `description`, `start_seconds`/`end_seconds`, and primary
`script_section_id`.

Record the job's canvas on `scene_plan.metadata.aspect_ratio` from `options.aspect_ratio`
(default `9:16`). Do **not** rewrite a caller-set ratio.

**Multi-voice shots:** several script sections (different `speaker`s) may share one scene's time
window. Keep one visual scene; list a separate narration `required_assets` entry per speaking
beat (see §7). Map `character_actions.dialogue` to the matching brand speaker
(`customer` / `panda`; off-screen lines → `narrator`).

### 3. The 5-aspect scene spec (MANDATORY — every scene, all five)
Silent omission is the top failure mode — it produces brittle prompts and reviewer churn. For
diagram/text_card/native scenes, an aspect may be "N/A" but ONLY explicitly.
1. **Subject** — who/what + key visual attributes. Every human/panda appearance MUST name the
   locked Element id from `config/panda-elements.json` (CHARACTER LOCK in
   `skills/meta/higgsfield-mcp-bridge.md`). Descriptions say "the woman" / "the panda", **not**
   "a young traveller" / "a cute cartoon panda". Refuse inventing a new face. Both characters
   are the **same 2D flat drawing** — do not distinguish them as "human in colour vs cartoon."
2. **Subject motion** — actions in temporal order (record as `character_actions` for rigged
   character scenes: `character_id`, `emotion`, ordered `action_sequence`, optional `dialogue`).
3. **Scene** — setting + POV + time of day + overlays (list overlays SEPARATELY in `overlay_notes`,
   never as "foreground"). Default medium is **2D flat** matching the turnaround sheets — no
   photoreal airport / Pixar hall.
4. **Spatial framing** — shot size + position-in-frame + depth (FG/MG/BG) + how they change.
   Whenever panda + customer share a frame, copy the binding `pair_scale_lock` from
   `config/panda-elements.json`: customer standing height = 1.00, panda ear-top height = 0.58
   (acceptable 0.53–0.63), both feet on the same ground plane, panda ear-top around the
   customer's lower chest / upper abdomen. State it in `description`, relevant
   `character_actions[].notes`, and image/video `required_assets`; do not rely on an implied
   phrase such as "panda beside customer."
5. **Camera** — capture in `shot_language`: `shot_size`, `camera_movement`, `lens_mm`,
   `lighting_key`, `depth_of_field`, `color_temperature`. For the default 2D flat look, mark
   `lens_mm` and `depth_of_field` **N/A** — do not write 35mm / DoF / photoreal cinema language
   that pulls the still into 3D.

### 4. Narrative structure (make each scene EARN its place)
For every scene set:
- **`narrative_role`** — one of `establish_context`, `introduce_subject`, `build_tension`,
  `deliver_payload`, `transition`, `emotional_beat`, `evidence`, `comparison`, `resolution`,
  `call_to_action`.
- **`shot_intent`** — WHY this shot exists (its job in the video).
- **`information_role`** — what the viewer learns or feels here.
- **`hero_moment: true`** on the ONE scene that is the visual peak (the brand payoff / the shot
  that most deserves the best still + motion sample). Exactly one per video is a good default.
- **`texture_keywords`** — prefer `clean`, `flat`, `matte`, `bright` for the 2D default; avoid
  `glossy` / photoreal cues unless the brief explicitly overrides.

### 5. Panda identity as PLAN requirements (words, not media)
Specify on-brand look, character consistency and composition **in words**: which character appears,
the on-model 2D appearance, and the reference Element id from `config/panda-elements.json`
that the assets stage MUST attach as media. Set the top-level `style_playbook` to the Panda style.
Never render. Map phrase aliases (human / traveller / customer / panda / mascot / …) to the
locked IDs — see `phrase_aliases` in `config/panda-elements.json`. Treat `pair_scale_lock` as
part of CHARACTER LOCK, not optional art direction. The same ratio, upright postures, and shared
ground plane must appear in every two-character scene regardless of shot size.

### 6. Narration duration budget (so the VO fits the video)
If the video is narrated, the narration MUST fit the runtime:
1. Total duration = last scene's `end_seconds`.
2. Target narration at **85–90%** of duration (breathing room at intro/outro).
3. Budget **2.0–2.5 words/sec** (calm/reassuring) or **2.5–3.0 words/sec** (energetic).
4. Allocate words per scene proportional to its seconds; keep opening/closing scenes light.
Validate: total words within budget; no scene's narration overflows its slot. (Word budget is a
**prior** only. In assets, measured ElevenLabs duration drives the full-scene allocation while
the requested total stays within ±5% — see TTS-first in `asset-director.md`. A large overrun
after TTS requires the bounded speed retry or a pacing revision; do not expect prompt-only lip sync.)

### 7. Declare `required_assets` per scene
For each scene that needs a still, list **exactly one** `{type: "image", description: "...",
source: "generate"}` for that still — **no** intermediate base-plate / restack hops as separate
`required_assets` (those burn the 2-take stills budget). Also declare video / narration / music
as needed. Descriptions must be actionable and name Element IDs (not
"an image of the panda" but "the panda Element waves at the airport gate, medium shot, 2D flat,
on-model"). Every `source: "generate"` asset must be feasible with the assets
tools (`image_selector`, `higgsfield_mcp_video`, `seedance_video`, `elevenlabs_tts`, `music_gen`).

For **each speaking beat**, declare a narration asset with `speaker` (`customer`|`panda`|`narrator`)
and `script_section_id` pointing at that section. One scene may have up to three narration
entries (one per brand speaker). Do not collapse multi-speaker dialogue into a single VO asset.

### 7b. User screenshots (only when the prompt has a USER SCREENSHOTS block)
The user's guidance in `inputs/requests.json` is **binding**: scene N (the N-th scene of this plan)
carries exactly the screenshots assigned to N — none missing, none extra. A `moment` screenshot goes
in the scene covering that moment. A screenshot with no guidance appears nowhere. Plan enough scenes
for the highest assigned number (3–8 s per scene).

For each placement add a **second** item to that scene's `required_assets` — the scene keeps its one
`source: "generate"` still, composed around the screenshot:
`{"type": "image", "source": "provided", "input_id": "in_01", "description": "...", "layout": {...}}`

`layout` follows `schemas/artifacts/screen_layout.schema.json` (worked example:
`docs/user-screenshots-plan.md` §4). Open the screenshot and place things by looking — never guess
coordinates. You decide the layout, following the user's `instruction`:
- `zone` (fractions of the frame): where the screenshot sits. Keep it off the caption strip and the
  logo corner listed in the prompt facts, and big enough to read (the launcher warns below 0.35×).
  App screenshots: a tall zone beside the character. Web pages: a wide zone above or below it.
- `subject_zone`: where the Panda / customer stands — never overlapping the screenshot. Write the
  generated still's description to match ("panda on the left third; right side plain white, empty").
- `frame`: `phone` for app screens, `browser` for web pages, `card` otherwise, `none` only if asked.
- `crop`: only to start on part of a large screenshot.
- `enter` / `exit` / `show`: when it appears. Several screenshots in one scene sit side by side
  (zones don't overlap) or replace each other in the same zone (`show` windows don't overlap).
- `steps` — regions and points are fractions of the WHOLE screenshot; `at_s` is seconds into the
  scene, timed to when the narrator says it:
  - `highlight_box` on the thing the narrator names; `cursor_move` + `click_pulse` for a tap/click.
  - `zoom_to` a region to enlarge it. The region grows to the screen's shape, so pick one about as
    narrow as the screen (a tall phone screen needs a narrow region) or it barely zooms.
  - `blur_region` over personal data — card numbers, ICCID / IMEI, phone numbers, emails, names,
    addresses, account QR codes — even when the user did not ask.
  - `card`: one short key message (≤ 12 CJK characters or 5 English words), `text` with zh and en,
    zone clear of the character and the captions.
- `camera`: `"locked"`.

Also write the scene's `captions` (`{"zh": ..., "en": ...}`) for every scene that carries a
screenshot. The launcher measures the caption with the real renderer to work out how far up the
frame it reaches — a long bilingual caption wraps to three lines and climbs well above the one-line
strip in the prompt facts — and warns when it would cover the screenshot. Without `captions` it can
only assume one line, so the warning arrives later, at the stills or final gate.

`show`, `enter`, `exit` and every `at_s` are checked again at compose against the duration the scene
is really **cut** to. A screenshot placed at 4–5 s in a scene the edit shortens to 3 s never appears,
so compose refuses to render rather than move what the user asked for: keep the timing inside the
shortest the scene could become, or leave `show` out to mean "the whole scene, however long it ends
up". If you shorten a scene on a revise, re-check the layouts in it.

The screenshot is the user's exact text, so the `text_card` rule in §8 does not apply to it. On a
revise that moves a screenshot ("move 4 to scene 6"), update `inputs/requests.json` **and** the layouts.

### 8. Coverage, variety & feasibility checks (before submitting)
- [ ] Scenes span the FULL duration (first at 0s, last at total), no gaps > 1s (unless a beat)
- [ ] Every script section maps to ≥ 1 scene (or is covered as a multi-voice beat on a scene);
      every enhancement cue is addressed
- [ ] Multi-voice scenes list one narration `required_asset` per speaking section (`speaker` +
      `script_section_id`)
- [ ] No more than 3 consecutive scenes of the same `type`; ≥ 2 types used
- [ ] Exactly one `hero_moment`; pacing alternates high-info and breathing-room scenes
- [ ] Every scene with a still has **exactly one** `source: "generate"` image `required_asset` (no plate
      chains; user screenshots with `source: "provided"` are extra items, not stills)
- [ ] Every human/panda appearance names the locked Element id; 2D medium is explicit
- [ ] Every panda+customer scene repeats the 0.58 ±0.05 pair scale, shared ground plane, and
      upright posture lock in description, actions, and required assets
- [ ] Every `required_asset` is feasible with the assets-stage tools
- [ ] Any scene with exact on-screen TEXT (CTA, phone number, price) is `type: "text_card"` — never
      `generated` (image models hallucinate text)

### 9. Self-evaluate (score 1–5; revise anything < 3)
| Criterion | Question |
|---|---|
| Visual storytelling | Does each scene advance the message, not just decorate? |
| Script alignment | Does each scene match what each speaker says at that moment? |
| Brand fidelity | Would every scene look like the same Panda video (style, on-model panda)? |
| Character consistency | Are panda/customer Element ids + actions specified so they stay on-model? |
| Asset feasibility | Can every `required_asset` actually be generated with the tools? |
| Pacing | Natural rhythm? Hero moment placed well? VO fits the runtime? |
| Voice casting | Are multi-speaker beats declared as separate narration assets with `speaker`? |
### 10. Write the scene_plan artifact + STOP for approval (GATE 2)
Persist a schema-valid `scene_plan` (`version: "1.0"`, `style_playbook`, `scenes: [...]`).
Checkpoint `status = awaiting_human`. Surface the **scene list as text** (timings, types,
descriptions, shot_language, narrative_role, hero_moment, character_actions, transitions,
required_assets). Do **not** proceed to `assets` until approved. On "request revision", rewrite the
plan per the feedback and re-checkpoint.

## Handoff to `assets`
The `assets` stage reads this `scene_plan` (+ `script`) and generates every declared asset —
stills, motion clips, narration, music — recording them in `asset_manifest`. A richer, well-specified
plan here means better generation prompts and fewer regenerations at the (expensive) stills / motion
/ assets gates. See `skills/pipelines/panda-video/asset-director.md`.

## Success criteria
- Schema-valid `scene_plan` (text only) — no media files produced by this stage
- `metadata.aspect_ratio` matches the job option (default `9:16`)
- Full duration covered with realistic timings and no gaps; VO budget fits the runtime
- Every scene carries the 5 aspects, a `narrative_role`, and feasible `required_assets`
- Exactly one `hero_moment`; panda/customer consistency captured as plan requirements
- Checkpoint left in `awaiting_human` for the gate
