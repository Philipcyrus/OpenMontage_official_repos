# Higgsfield MCP Bridge — Agent Protocol

This project generates video through the **agent's Higgsfield MCP connection**,
not the Higgsfield Cloud REST API. The registry provider is
`higgsfield_mcp_video` (provider id `higgsfield_mcp`), backed by
`tools/video/higgsfield_mcp_video.py`. Read this before any asset stage that
produces motion video.

## Why a bridge

OpenMontage tools are Python `BaseTool` subclasses that call provider APIs with
keys. The Higgsfield connection on this machine is an **MCP integration** —
callable only by the agent, not from inside the Python process. So generation is
split: the **agent** calls Higgsfield MCP to make the clip; the
**`higgsfield_mcp_video` tool** ingests the result (download + ffprobe +
provenance) into the project so the `asset_manifest` stays consistent.

`higgsfield_mcp_video` reports `AVAILABLE` whenever `HIGGSFIELD_MCP_BRIDGE=1` is
set in `.env`. That flag means "the agent driving this repo has a working
Higgsfield MCP connection." It is NOT a guarantee the MCP server is reachable —
if an MCP call fails, surface a structured blocker per the AGENT_GUIDE
"Escalate Blockers Explicitly" rule. Do not silently fall back to another
provider.

## MCP tool names

The Higgsfield MCP tools are namespaced (e.g. `mcp__claude_ai_Higgsfield__*`).
The relevant ones:

- `balance` — check available credits before a batch.
- `models_explore` (action `recommend`/`get`) — pick a model, confirm exact
  model id, durations, aspect ratios, and which media roles it accepts.
- `generate_video` — submit a generation. Pass `get_cost:true` first to
  preflight the credit cost without spending.
- `job_status` / `show_generations` — poll until the job is `completed`.
- `reveal_generation` / `job_display` — obtain the finished clip's CDN URL.
- `generate_image` — make a still first when doing image-to-video. Also used for
  stills-only carousel slides, and for GATE 3 **edit** revises (pass a registered
  `media_id` as the start/reference image).
- `media_import_url` — register a **public** web image URL, returns a `media_id` to
  pass as a `medias[]` value (never pass raw https URLs into `medias`). **Cannot**
  fetch `localhost` / launcher artifact URLs.
- `media_import` (local upload) — register a PNG already on disk. Use this for
  GATE 3 **edit** revises: load the still from the absolute path in the revise
  prompt and upload it. Confirm the live tool name with the MCP catalog.

Always confirm the live model catalog with `models_explore` rather than trusting
hardcoded ids. Model guidance from the server: `seedance_2_0` for identity /
cinematic; `kling3_0` for multi-shot, native audio, or motion transfer;
`kling3_0_turbo` for fast text-to-video or single start-frame animation. For
image generation, `models_explore` the stills model and confirm which media role
it uses for a start/reference image before an **edit** revise.

## Stills revise — FRESH vs EDIT (GATE 3)

At `approve_stills` (`panda-video`, `panda-carousel`, and `panda-image`) the human may send
`{"decision":"revise","mode":"fresh"|"edit","shots":[…],"answer":"…"}`. If `mode`
is omitted, infer: `shots` set and the note is a local change (change / fix /
remove / keep / edit) → **edit**; regenerate / redo / new / from scratch /
different scene → **fresh**; no `shots` and ambiguous → **fresh**.

Motion, clips, and final gates stay regenerate-only. Do not image-to-image after
the job is `done`. Panda stills stay on Higgsfield MCP (not FLUX/BFL).

### FRESH

`generate_image` from the revised prompt + panda/customer Element IDs only. Do
**not** pass the previous PNG. Replace only the flagged still files and their
`asset_manifest` rows (new `credits` / `job_id`). Leave other slides untouched.
Then rewrite the assets checkpoint `status='awaiting_human'` with **top-level**
`partial_progress={"phase":"stills"}` (not nested under `metadata`) and STOP.

### EDIT (image-to-image)

1. Load each flagged still from disk (absolute path in the revise prompt —
   typically `projects/<job_id>/assets/images/…`; launcher artifact URLs on
   localhost are not fetchable).
2. Register it with Higgsfield MCP via local upload / `media_import`.
   `media_import_url` cannot fetch localhost.
3. `models_explore` — confirm the image model's start/reference media role.
4. `generate_image` with that `media_id` plus a **preservation prompt**: keep
   composition, character, layout, and typography; apply only `{answer}`. Keep
   Element IDs. Same aspect ratio as the source still.
5. Replace that slide's file + `asset_manifest` row (new `credits` / `job_id`).
   Leave other slides untouched.
6. Checkpoint `awaiting_human` with **top-level**
   `partial_progress={"phase":"stills"}` and STOP.

If the image model rejects a source still, surface a blocker and wait. Do **not**
silently fall back to FRESH.

## CHARACTER LOCK (binding — panda-video / panda-carousel / panda-image)

Any mention of a human or the panda is the locked brand pair from
`config/panda-elements.json`. Never invent a new face, body, mascot, or
"similar traveller."

| Role | Higgsfield Element | Local sheet (style only — do NOT bake the turnaround grid into the still) |
|------|--------------------|----------------------------------------------------------------------------|
| Human / customer | `089ddcec-c375-4299-8a65-6d8b757dd81a` (`customer_reference_element_id`) | `brand/panda-mobile/customer/Customer-V3-Hero-Turnaround-v1.png` |
| Panda / mascot | `4c01c8f9-6cfb-4d8c-9eb9-74cb61462103` (`panda_reference_element_id`) | `brand/panda-mobile/mascot/Panda-Mobile-Smiling-Hero-Turnaround-v1.png` |

Do **not** use `student_ugc_element_id` (still TODO).

**Phrase aliases** that must resolve to those IDs (brief, idea, script, scene plan,
`required_assets`, still prompts, i2v prompts, gate questions) — see also
`phrase_aliases` under `higgsfield` in `config/panda-elements.json`:

- **Human:** human, person, woman, man, girl, guy, traveller / traveler, customer,
  UGC talent, user, character, the girl with the suitcase, "someone at the airport"
- **Panda:** panda, mascot, brand mascot, bear, Panda Mobile character, "cute panda"

**How to pass them (the only lock that works):**

1. Idea rewrites the brief: those phrases become "the customer Element" / "the panda
   Element" plus the IDs. Downstream never treats the English words as a character spec.
2. Scene plan names the Element IDs on every scene that shows that role. Descriptions
   say "the woman" / "the panda", not "a young traveller" / "a cute cartoon panda."
3. Every `generate_image` / image-to-video that shows that role **attaches the Element
   in the MCP media / `image_references` slot** (`models_explore` for the live role
   name). Putting the UUID in the prompt sentence is **forbidden** (it draws the
   six-view turnaround sheet).
4. Prompt prose may describe pose, props, and setting only. Wardrobe fights the sheet
   only on an explicit human revise — take 1 keeps the Element's canonical look.
5. A still or clip of a human or panda **without** that Element in `medias` is a
   defect; do not ship it. Log the IDs on the `asset_manifest` row.

### PAIR SCALE + POSTURE LOCK (binding)

Whenever panda + customer share a frame, apply
`config/panda-elements.json.character_references.pair_scale_lock` in addition to both Element
references:

- standing customer height = **1.00**;
- panda ground-to-ear-top height = **0.58**, acceptable range **0.53–0.63**;
- both characters' feet sit on the **same ground line** — never fake the ratio by moving one
  character into foreground/background;
- panda ear-top aligns around the customer's lower chest / upper abdomen;
- customer remains upright and relaxed with natural proportions;
- panda remains an upright bipedal mascot with broad rounded torso, canonical large head, and
  short planted legs.

Repeat the numeric ratio and ground-plane instruction in every two-character still prompt.
Element attachment alone does not lock relative composition. Review each returned still before
acceptance: outside-range scale, mismatched ground plane, crouching, stretched anatomy, or
toy-sized/oversized panda is unusable and qualifies for the existing take-2 i2i correction.

For image-to-video, use the approved still as `start_image` and explicitly preserve its exact
relative scale, posture, body proportions, and ground plane through the final frame. Camera
movement or depth drift that changes apparent ratio is a failure. Record the still and clip
checks in `asset_manifest.metadata.character_scale_qa`; a remaining take-2 defect is surfaced at
the assets gate, never silently called consistent.

## LOOK LOCK (after approve_hero_still)

When remaining storyboard stills follow an approved hero PNG: `media_import` that hero as a
**style/look** reference (palette, character rendering, lighting, medium, wardrobe). Confirm the
live media role with `models_explore` — do **not** pass it as a start-frame that copies
composition onto every scene. Bake `look_notes` from hero revises into every remaining prompt.

## STILLS 2-TAKE HARD RULE (binding)

Per scene, per stills round (first GATE 3 pass, or a later human `revise` on that scene):

1. **Take 1 wave** — preflight one `generate_image` for every remaining scene and
   enforce the budget against that complete take-1 batch before submitting any job.
   Then submit with at most **4 jobs in flight**. Poll the set together instead of
   waiting for one image before submitting the next. Both characters belong in one
   T2I when the scene needs both. Attach Element IDs in the MCP media slot; never put
   UUIDs in the prompt. After an approved hero, import it once as a style/look
   reference and reuse that media id across the batch.
2. After the take-1 wave returns, review each result. Only an unusable take 1 gets a
   **take 2**, which is i2i of take 1 (one change), never a fresh T2I. Take-2 jobs may
   form their own wave but must never be submitted speculatively before take 1 review.
3. **Stop.** Ship take 2 if it exists, else take 1. Write `approve_stills` and end
   the turn. Flag remaining defects in the gate `question` — do not generate again.
4. A third paid `generate_image` for that scene in this round is a **defect**. Known
   no-ops (slide / reposition) do not get a third try.
5. Offline `still_frame_conform` / HSV color passes do not count. `get_cost:true`
   does not count. Image-to-video is not this cap.
6. Human `revise` on flagged shots starts a **new** 2-take budget for those shots only.

Do **not** plan a "customer-alone base plate, then i2i-add panda" as two
`required_assets` — that burns the whole budget with no room for a reject. One image
`required_asset` per scene. Archive a rejected take 1 as `rejected_*` if take 2 ships.

## 2D MEDIUM LOCK (binding)

Default every panda still and clip to **2D flat illustration** matching the customer
and panda turnaround sheets — same medium for people, mascot, props, and set. Use
`styles/panda.yaml` `image_prompt_prefix` / `image_negative_prompt`.

- No Pixar / CGI / photoreal hall composited with a vector sticker.
- Mixed 3D-human + 2D-panda is a fail.
- i2v prompts must hold the 2D still — do not ask Kling to make the clip 3D / photoreal.
- Override only if the **user brief** explicitly asks for 3D / photoreal / live-action;
  log that in `decision_log`.

## Per-wave generation loop

Preflight every pending clip required by the `scene_plan` before the first submit,
then process those clips in waves. The normal concurrency cap is **4 in-flight
Higgsfield jobs**. If any submit returns a rate-limit / 429 response, lower the cap
to **2** for the rest of that agent leg. Do not change model, prompt, duration, or
quality settings merely to gain concurrency.

0. **TTS-first duration (panda-video)** — generate and probe all scene VO **before** preflight,
   then call `lib.i2v_duration.allocate_scene_durations` once for the full timeline. Set each
   `duration` from its allocation (allowed values from `models_explore`) so scene lengths follow
   audio while the final cut remains within ±5% of the requested total. Preserve scene-local
   audio offsets. See `skills/pipelines/panda-video/asset-director.md`.

### Audio lip-sync (panda-video)

When job option `audio_lipsync` is on (**default**), on-screen `customer`/`panda` speaking clips
use **`seedance_2_0`** with:

- `medias` role **`start_image`** = approved still
- `medias` role **`audio_references`** = that scene’s timing-preserving ElevenLabs
  VO bed (MCP-uploaded)
- **`generate_audio: false`** — do not invent a second audio bed
- Prompt: 2D + Element LOCK; animate mouth/jaw to lip-sync the attached audio (no mouth HOLD)

For a scene with multiple dialogue files, build **one timing-preserving scene-local
VO bed** before upload. Place every source at its script offset relative to scene start
(`relative_at_s = section.start_seconds - scene.start_seconds`), retain leading and
inter-line silence, and mix overlapping lines. Use the same `adelay` + `amix` semantics
as `tools.video.panda_render._premix_voice_tracks`; never join files back-to-back. A
customer line at 0–2s and panda line at 3–5s therefore produces a 5s bed with the
2–3s pause intact.

`kling3_0` has **no** audio input role — do not use it for lipsync shots. Narrator / text_card /
lipsync-off jobs keep HOLD LOCK (mouth frozen) with duration-only alignment.

Compose still **mutes** native AAC (noop when silent) and lays the original ElevenLabs
files at the same script offsets used to make the reference bed. This keeps picture and
brand voice on one timeline. On `audio_references` failure: fall back to HOLD +
duration-only and log it.

1. **Preflight the whole pending batch** — call `generate_video` with
   `{model, prompt, duration, aspect_ratio, count:1, get_cost:true}` (plus medias when
   lipsync) for every pending clip. Sum requested credits, combine them with
   already-spent credits, and apply the BUDGET HARD RULE **before any submit**.
   Retain every clip's preflight credit number for its `asset_manifest` row.
2. **Submit the first wave** — submit up to 4 independent `generate_video` calls in
   parallel (omit `get_cost`). Capture each returned `job_id` keyed by `scene_id`.
   For image-to-video, register the approved still and optional audio bed first,
   then pass those media ids in the model's declared roles.
3. **Checkpoint in-flight work immediately** — write an `assets` checkpoint with
   `status="in_progress"` and `metadata.partial_progress.motion_jobs`, mapping every
   `scene_id` to its `job_id`, creative parameters, credits, and output path. A timeout
   or cold resume must poll these ids; it must not submit duplicate paid generations.
4. **Poll the set** — query `job_status` or `show_generations` for all in-flight ids
   until each reaches `completed`, `failed`, `nsfw`, or `cancelled`. Do not serialize
   submit → poll → ingest one clip at a time.
5. **Reveal + ingest successes** — use `reveal_generation` / `job_display` for each
   completed job, then call `higgsfield_mcp_video.execute({...})`
   with the same creative params **plus**:
   - `video_url`: the CDN URL (or `source_path` if you downloaded it
     yourself),
   - `job_id`: captured at submit,
   - `output_path`: the project asset path, e.g.
     `projects/<name>/assets/video/<scene-id>.mp4`.
   The tool downloads, runs ffprobe, and returns a standard `ToolResult` with
   width/height/duration/codec for the `asset_manifest`.
6. **Handle partial failure without replaying the wave** — keep and ingest every
   success. Record failed scene ids and provider states in the checkpoint / gate
   question; never silently skip them and never resubmit successful jobs. Regenerate
   only failed scenes after the approved recovery decision.
7. **Start the next wave** — when more than 4 clips remain, repeat with the next set
   only after capacity is available.

If you invoke `higgsfield_mcp_video` with no `video_url`/`source_path`, it
returns `success=False` with an `agent_action_required` payload restating these
steps — a safety net, not the intended path.

## Provenance

Record per clip in the `asset_manifest`: `provider: higgsfield_mcp`, the `model`,
the `job_id`, the prompt, the probed dimensions, and the **`credits`** consumed
(from the step-1 `get_cost` preflight) with `credits_source: "actual"`. Higgsfield clips may include
native synced audio — note in `edit_decisions` whether a clip's audio is kept or
replaced by the narration/music mix so the compose stage doesn't double up.

## Cost note

Higgsfield bills in **credits**, not USD. `higgsfield_mcp_video.estimate_cost`
returns a rough USD figure for budget governance only; the authoritative number
is the `get_cost:true` preflight from MCP. Check `balance` before large batches.

## BUDGET HARD RULE (run-level credit ceiling)

When the job sets **`max_higgsfield_credits`** (a per-project credit cap), enforce a **hard
pre-generation block** — credits are the authoritative unit:

1. **Before calling ANY Higgsfield generation** (`generate_image`, `generate_video`, image→video —
   stills, motion sample, OR the full batch), compute:
   - `spent` = sum of `credits` already recorded in `asset_manifest` (this project so far), and
   - `requested` = sum of the `get_cost:true` preflight credits for the batch you are about to submit.
2. If `spent + requested > max_higgsfield_credits`: **DO NOT call the generation tool.** Write the
   `assets` checkpoint `status='awaiting_human'` **AND `partial_progress={"phase":"budget_hold"}`**,
   include the numbers (`cap`, `spent`, `requested`, `projected = spent+requested`) in the checkpoint,
   and **STOP**. The launcher surfaces this as the **`budget_exceeded`** gate.
3. The human then either **raises the cap** (resume carries a new `max_higgsfield_credits`), **revises**
   (regenerate a cheaper/smaller plan), or **cancels**. On resume, **re-run this check** before
   generating — never assume the raise is enough without re-summing.

No cap set (`max_higgsfield_credits` unset) → no ceiling; still record each asset's `get_cost`
credits in `asset_manifest`. This is stop-and-ask, not silent truncation: never quietly drop scenes
to fit a budget — always pause for the human decision.
