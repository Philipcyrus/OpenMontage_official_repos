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
Transform each script section into **1–3 scenes** (a distinct visual moment each — avoid one
static scene per long section). Set `id`, `type` (one of the 9 canonical types: `talking_head`,
`broll`, `animation`, `character_scene`, `diagram`, `text_card`, `transition`, `generated`,
`screen_recording`), `description`, `start_seconds`/`end_seconds`, and `script_section_id`.

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
locked IDs — see `phrase_aliases` in `config/panda-elements.json`.

### 6. Narration duration budget (so the VO fits the video)
If the video is narrated, the narration MUST fit the runtime:
1. Total duration = last scene's `end_seconds`.
2. Target narration at **85–90%** of duration (breathing room at intro/outro).
3. Budget **2.0–2.5 words/sec** (calm/reassuring) or **2.5–3.0 words/sec** (energetic).
4. Allocate words per scene proportional to its seconds; keep opening/closing scenes light.
Validate: total words within budget; no scene's narration overflows its slot. (The assets stage's
TTS returns an audio duration — a large overrun means trim the script or extend the closing scene.)

### 7. Declare `required_assets` per scene
For each scene that needs a still, list **exactly one** `{type: "image", description: "...",
source: "generate"}` for that still — **no** intermediate base-plate / restack hops as separate
`required_assets` (those burn the 2-take stills budget). Also declare video / narration / music
as needed. Descriptions must be actionable and name Element IDs (not
"an image of the panda" but "the panda Element waves at the airport gate, medium shot, 2D flat,
on-model"). Every `source: "generate"` asset must be feasible with the assets
tools (`image_selector`, `higgsfield_mcp_video`, `seedance_video`, `elevenlabs_tts`, `music_gen`).

### 8. Coverage, variety & feasibility checks (before submitting)
- [ ] Scenes span the FULL duration (first at 0s, last at total), no gaps > 1s (unless a beat)
- [ ] Every script section maps to ≥ 1 scene; every enhancement cue is addressed
- [ ] No more than 3 consecutive scenes of the same `type`; ≥ 2 types used
- [ ] Exactly one `hero_moment`; pacing alternates high-info and breathing-room scenes
- [ ] Every scene with a still has **exactly one** image `required_asset` (no plate chains)
- [ ] Every human/panda appearance names the locked Element id; 2D medium is explicit
- [ ] Every `required_asset` is feasible with the assets-stage tools
- [ ] Any scene with exact on-screen TEXT (CTA, phone number, price) is `type: "text_card"` — never
      `generated` (image models hallucinate text)

### 9. Self-evaluate (score 1–5; revise anything < 3)
| Criterion | Question |
|---|---|
| Visual storytelling | Does each scene advance the message, not just decorate? |
| Script alignment | Does each scene match what the narrator says at that moment? |
| Brand fidelity | Would every scene look like the same Panda video (style, on-model panda)? |
| Character consistency | Are panda/customer Element ids + actions specified so they stay on-model? |
| Asset feasibility | Can every `required_asset` actually be generated with the tools? |
| Pacing | Natural rhythm? Hero moment placed well? VO fits the runtime? |

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
- Full duration covered with realistic timings and no gaps; VO budget fits the runtime
- Every scene carries the 5 aspects, a `narrative_role`, and feasible `required_assets`
- Exactly one `hero_moment`; panda/customer consistency captured as plan requirements
- Checkpoint left in `awaiting_human` for the gate
