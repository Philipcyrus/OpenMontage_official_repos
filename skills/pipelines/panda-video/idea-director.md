# Idea Director — Panda Video Pipeline

> **Internal stage — NO human gate.** `pipeline_defs/panda-video.yaml` sets
> `idea.human_approval_default: false`, and **the manifest is authoritative**. Do NOT checkpoint
> `awaiting_human` and do NOT end your turn for approval here — write the stage `completed` and
> continue to `script`. (This replaces the reused hybrid director, whose "Gate Reminder" assumed a
> gated stage; that does not apply to Panda.)

## When To Use

You are starting a Panda Mobile video from the user's `brief`. Panda videos are **fully generated**
(mascot + brand, via the Higgsfield MCP) — there is no source-footage anchor to plan around. This
stage folds upstream's research + proposal *reasoning* into one internal step: commit the creative
direction from the brief, log the key production decisions, and surface anything missing — **without
a human pause and without multi-concept exploration** (the brief already carries the concept).

## Produces
- **`brief`** — the consolidated production brief (below).
- **`decision_log`** — the key decisions made here (render runtime, providers, fallbacks), each with
  `options_considered` and a rationale.

## Process

### 1. Commit the creative direction (from the brief)
Panda is brief-driven: the user's brief already specifies the concept (a named ad / tutorial / UGC
piece). Restate it concretely — do **not** generate 3+ alternative concepts for selection (that is
upstream's proposal behavior; Panda skips it). If the brief is genuinely ambiguous on a material
point, note it in `missing_capabilities` rather than inventing options.

**Character lock (binding):** any human / traveller / customer / person / "the girl" (and
`phrase_aliases.customer` in `config/panda-elements.json`) maps to
`customer_reference_element_id`. Any panda / mascot / bear (and `phrase_aliases.panda`) maps to
`panda_reference_element_id`. Rewrite the brief so downstream stages never treat those English
words as a character-design prompt. Log a `character_lock` decision in `decision_log` with the
IDs chosen. See CHARACTER LOCK in `skills/meta/higgsfield-mcp-bridge.md`.

**Visual medium (binding):** default is **2D flat illustration** per `styles/panda.yaml`. Override
only if the brief explicitly asks for 3D / photoreal / live-action — then log it in `decision_log`.

### 2. Deliverable & format
Fix `deliverable_mix`: **aspect ratio from `options.aspect_ratio`** (default **`9:16`** when
omitted; pass through caller values such as `16:9`, `1:1`, `4:5`, `WIDTHxHEIGHT` — never rewrite),
target duration, language, and any cutdowns. Record the chosen aspect in `deliverable_mix` so
scene_plan / assets / compose use the same canvas.

### 3. Runtime & provider decisions (log them)
- **Render runtime:** honor the `render_runtime` job option; if `auto`, note the intended lane per
  `skills/pipelines/panda-video/compose-director.md` (default `ffmpeg`/`panda_render` for
  character-mascot clips). Log a `render_runtime_selection` decision in `decision_log`.
- **Providers:** visuals via the Higgsfield MCP with locked Elements attached as media;
  voice/music via ElevenLabs. Note any provider
  fallback policy (e.g. ElevenLabs → Higgsfield audio only if truly unavailable).

### 4. Brief metadata
Recommended keys: `concept`, `deliverable_mix`, `language`, `narrator`, `support_layers`
(narration, music, captions, brand cards), `character_lock` (customer/panda Element ids),
`visual_medium` (default `2d_flat`), `missing_capabilities`, `fallback_policy`.

### 4b. User screenshots (only when the prompt has a USER SCREENSHOTS block)
The user attached screenshots and usually said which one goes in which scene and how to use it.
Their guidance is binding — you record it, you do not re-decide it.
1. Look at every screenshot (read them all in one turn) so you know what each one shows.
2. Write `projects/<job>/inputs/requests.json` — one entry per upload
   (`schemas/artifacts/screen_requests.schema.json`):
   `{"n": 1, "input_id": "in_01", "scenes": [1], "moment": "", "instruction": "zoom on the Pay button", "shows": "checkout page with the total and Pay now", "placement": "beside"}`
   - `n` is the attachment number the user sees; they may also name a screenshot by file name or
     by what it shows.
   - `scenes`: the scene numbers the user gave (1 = first scene). Several screenshots may share a
     scene; one screenshot may be in several scenes.
   - `moment`: when the user named a moment instead of a number ("when we explain activation"),
     copy their words and leave `scenes` empty.
   - No guidance for a screenshot → `"scenes": []` and `"moment": ""`. It is **not used**; never
     place it on your own.
   - `instruction`: how to use it, in the user's words (zoom, highlight, blur, frame, text).
   - `placement`: `"held"` when the brief says the mascot **holds / shows / presents** the screenshot
     (or a phone screen) toward the viewer; otherwise `"beside"` (default).
3. Write the screenshot order into the brief so the script narrates what is on screen in that
   order and the plan can have at least as many scenes as the highest scene number given.
4. Never copy screenshots into `assets/`, never edit them, never send them to Higgsfield.

### 5. Quality check (self, no human pause)
- [ ] The concept is stated concretely and traceable to the brief
- [ ] Deliverable format + duration + language are explicit
- [ ] Aspect ratio matches `options.aspect_ratio` (default `9:16`) and is logged in `deliverable_mix`
- [ ] Human/panda phrases resolved to locked Element IDs; `character_lock` logged
- [ ] Visual medium is 2D flat unless the brief explicitly overrides
- [ ] Render runtime + provider decisions are logged in `decision_log`
- [ ] Missing capabilities / fallbacks are surfaced early

## Handoff
`script` reads the `brief` and writes the narration/beats (GATE 1). Cost governance, if a
`max_credits` cap is set on the job, is enforced later at the Higgsfield generation steps — see
`skills/meta/higgsfield-mcp-bridge.md`.

## Gate Reminder (Binding) — corrected for Panda
**This stage does NOT gate.** Gate ONLY when the active pipeline manifest sets
`human_approval_default: true`; **manifest policy overrides this skill.** For `panda-video`, `idea`
is internal: write the checkpoint `completed` and proceed to `script` in the same run. The first
human gate is `approve_script`.
