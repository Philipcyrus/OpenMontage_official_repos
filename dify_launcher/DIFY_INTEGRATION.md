# Panda AI Video Engine — Dify Integration Guide

**Audience:** the team wiring the Dify workflow.
**What this service is:** an HTTP API that turns a **brief** into a finished, on-brand **Panda Mobile** video. A person (through Dify) submits the brief and **reviews/approves the work at every stage**; a headless AI agent does the production. Dify never talks to the AI directly — it talks to **this launcher**.

---

## 1. The big picture

```
Dify  ──HTTP──▶  Launcher (this API)  ──▶  headless Claude agent
   submit brief        :8501                 ├─ Higgsfield  → images + video
   poll + approve                            ├─ ElevenLabs  → voice + music
                                             └─ panda_render → final assembled MP4
```

- **One base URL**, a handful of endpoints (below).
- The pipeline **pauses for human approval at 6 gates**: script → scene_plan (text) → stills (before video) → assets (all media) → final → **brand**. Branding is a launcher overlay, not an engine stage.
- Real generation takes **minutes**, so the API is **asynchronous**: you `POST`, then **poll** `GET` until the gate is ready. (See §4 — this is the single most important thing to build correctly.)
- Output at each gate is **real reviewable material** — the **script** (dialogue/sections) and the **scene plan** come **inline** as structured JSON (show them directly at their gates), and the visual media (stills, clips, final MP4) come as downloadable files at the asset/final gates.

---

## 2. Base URL & auth

| | |
|---|---|
| **Base URL** | `https://dev.om.mvnoc.ai` |
| **Auth** | **Optional, env-toggleable.** Controlled by `DIFY_TOKEN` on the server. If `DIFY_TOKEN` is **empty/unset** (the current default), auth is **off** — send no header. If `DIFY_TOKEN` is **set**, every request must send header `X-Dify-Token: <that value>` or gets `401`. |
| **Content type** | `application/json` for all POST bodies. |

A bad/missing token *when one is configured* → `401`. When no token is configured, the header is
ignored (harmless to send). The `X-Dify-Token` header in the examples below is only needed if the
server has `DIFY_TOKEN` set — omit it in the default open mode.

---

## 3. Endpoints (reference)

### `GET /health`
Liveness + mode.
```json
{"status":"ok","runner":"claude","async":true,"montage_door":true}
```
`runner:"claude"` = real AI. `runner:"mock"` = placeholder mode (no AI, for wiring tests). `async:true` = poll model (see §4). `montage_door:true` = the direct render door (§15) is mounted.

### `POST /jobs` — start a job
Body:
```json
{
  "brief": "10-second vertical clip: Panda mascot waves at an airport and says 'Grab a Panda Mobile eSIM before you fly.'",
  "pipeline": "panda-video",
  "profile": "ugc",
  "options": { "language": "en", "narrator": "panda", "music": "upbeat, light" }
}
```
`pipeline` is optional (`"panda-video"` default, `"panda-carousel"` for a stills-only carousel,
or `"panda-image"` for a single still).
Returns **immediately**:
```json
{"job_id":"job_xxxx","status":"running","stage":null,"gate":null,"question":"starting…","artifacts":{}}
```
→ **Save `job_id`.** Then poll (§4).

### `GET /jobs/{job_id}` — current state (poll this)
```json
{"job_id":"job_xxxx","status":"awaiting_human","stage":"scene_plan",
 "gate":"approve_scene_plan","question":"Approve the scene plan (text)…",
 "artifacts":{"scene_plan":{"version":"1.0","scenes":[{"id":"scene-1","type":"generated",
   "description":"Panda waves at the airport gate","start_seconds":0,"end_seconds":3, …}]}}}
```
At the `scene_plan` gate the artifact is the **structured text plan** (a JSON object shown for review — **no images**). Media appears later, at the `assets` gate.

### `POST /jobs/{job_id}/respond` — approve or edit the current gate
Body (approve):
```json
{"decision":"approve"}
```
Body (edit / revise):
```json
{"decision":"revise","answer":"make the panda wave bigger and brighter"}
```
Returns immediately with `status:"running"` → poll again for the next gate.

Body (`skip` — **only** at `approve_brand`; other gates → `400`):
```json
{"decision":"skip"}
```
At `approve_brand`: `approve` stamps BGC copies then `done`; `skip` finishes UGC-only; `revise` stays at the gate (no regen). Dify **must collect this choice before treating the job as finished**. Branding does **not** flow through animation.

### `POST /jobs/{job_id}/brand` — stamp BGC wordmark after skip / on already-`done` jobs
Job must already be `done`. While `gate` is `approve_brand`, branding happens only via `/respond` (`409` if you call `/brand` there). Body:
```json
{"profile": "bgc"}
```
Stamps the Panda wordmark onto **copies**. UGC originals stay under `artifacts.stills`
and `artifacts.final`. Branding is a **post-cut overlay** — it does **not** flow through
Higgsfield animation (stills used for i2v stay clean).

- Stills jobs (carousel / image): branded copies are `artifacts.branded_stills`
  (filenames like `still_00.bgc.png`).
- Video jobs: same still copies **plus** `artifacts.branded_final` (`final.bgc.mp4`) —
  the wordmark overlaid on the already-assembled UGC master. `final.mp4` is unchanged.
- `artifacts.branded` becomes `true`.
- Idempotent per output: a second call returns the same `branded_stills` /
  `branded_final` without restamping.
- `409` if the job is not `done`, is still at `approve_brand` (use `/respond`), or if there are no stills and no final to brand.
- `400` if `profile` is anything other than `"bgc"`.
- No agent turn, no Higgsfield spend. Sync is fine (PIL stamp / ffmpeg overlay is seconds).
  Intro/outro cards are not part of this pass.

Dify flow: at `approve_brand`, show UGC stills / `final.mp4` and collect approve / skip / revise.
`/brand` remains for skip-then-brand-later and older workflows.

### `GET /jobs/{job_id}/artifacts/{name}` — download a file
e.g. `GET /jobs/job_xxxx/artifacts/final.mp4`. Returns the binary file.
> Artifact paths in responses are **relative** (`/jobs/.../x.png`). Prepend the base URL to fetch/display them.

### `GET /jobs/{job_id}/cost` — per-project cost & time report
Returns the consumption for the whole project in each platform's **own native units** (no
cross-platform USD total, by design):
```json
{"job_id":"job_xxxx",
 "time":{"stages":[{"stage":"stills","seconds":310.5,"human":"5m 10s"},
                   {"stage":"assets_media","seconds":1180.2,"human":"19m 40s"}],
         "total_active_seconds":1716.1,"total_active_human":"28m 36s"},
 "platforms":{
   "higgsfield":{"unit":"credits","total":22,"actual":16,"estimated":6,"count":3,"items":[…]},
   "elevenlabs":{"unit":"characters","total":860,"calls":2,"source":"actual"},
   "elevenlabs_music":{"unit":"seconds","total":30,"calls":1,"source":"actual"}}}
```
- **Higgsfield → credits** (real, from the agent's `get_cost` preflight; each item tagged `actual`/`estimated`).
- **ElevenLabs → characters** (voice) / **seconds** (music) — real, measured by the tools.
- **time** → active generation time per stage + total. This is machine time only; it **excludes** the human review waits between gates.
- Before any generation has run the endpoint returns `{"cost_report":null,"note":"…"}`.

**Cost is API-only** — it is **not** attached to the normal `GET /jobs/{id}` poll responses (so the
gate flow stays uncluttered). Fetch it explicitly when you want it via this `GET /jobs/{id}/cost`
endpoint, or download the `cost_report.md` / `cost_report.json` artifact by name
(`GET /jobs/{id}/artifacts/cost_report.md`). The report files are refreshed after every leg.

> **Not tracked:** LLM (Claude) cost/tokens, and fal/Seedance/Kling — by request, only
> Higgsfield credits, ElevenLabs usage, and generation time are reported.

---

## 4. The async model — POLL, don't wait on the POST

Because a stage can take minutes, **`POST` returns instantly with `status:"running"`.** You must **poll `GET /jobs/{job_id}`** until the status changes.

```
POST /jobs                    → status: running        (instant)
loop: GET /jobs/{id} every ~20s
        status == running     → keep polling
        status == awaiting_human → STOP polling, show the gate to the user
POST /jobs/{id}/respond       → status: running        (instant)
loop: GET again … repeat for each gate
        status == done        → video: fetch final.mp4 (+ branded_final if approved at approve_brand); carousel: fetch stills (+ branded_stills if approved)
        status == failed      → show `question` (the error)
```

**Do not** set a long HTTP timeout hoping the POST returns the result — it won't. Set HTTP node timeouts to **30–60s**; all calls return quickly. The waiting happens in the poll loop.

---

## 5. The gate sequence (state machine)

Upstream's text `scene_plan` + Panda **cost gates**: the `assets` stage pauses up to **three times** — stills first (cheap), then one motion sample (approve the motion before batching), then the full media.

```
POST /jobs
   │  (agent: idea + script)
   ▼
approve_script         artifacts: script                    approve│revise
   │
   ▼
approve_scene_plan     artifacts: scene_plan (TEXT plan)     approve│revise        ← no media here
   │
   ▼
approve_stills         artifacts: stills[]  (NO video yet)   approve│revise (per-scene)
   │                                                          ↑ approve the look BEFORE paying
   ▼                                                            for image→video
approve_motion_sample  artifacts: motion_sample (ONE clip)   approve│revise        ← only if the
   │                                                            motion_sample option is on
   ▼                                                            (**default off**; pass true to opt in);
                                                                approve the MOTION before the batch
approve_assets         artifacts: stills[]+clips[]+          approve│revise (per-shot)
   │                              asset_manifest (all media)
   ▼
approve_final          artifacts: final (MP4)               approve│revise
   │
   ▼
approve_brand          artifacts: final + stills (UGC)      approve│skip│revise
   │                                                          ← launcher overlay only; not Higgsfield
   ▼
done                   artifacts: final (+ branded_final if approved)
```

**`panda-carousel`** (`"pipeline": "panda-carousel"`). Slide size is `options.aspect_ratio`
(default `4:5` — see [`CAROUSEL.md`](CAROUSEL.md)). Dual-mode stills revise: `mode` `edit` | `fresh`.

```
POST /jobs  { "pipeline": "panda-carousel", ... }
   │
   ▼
approve_script         artifacts: script                    (skippable via options.gates)
   │
   ▼
approve_scene_plan     artifacts: scene_plan + captions     approve│revise
   │
   ▼
approve_stills         artifacts: stills[] (UGC)            approve│revise
   │
   ▼
approve_brand          artifacts: stills[]                  approve│skip│revise
   │
   ▼
done                   artifacts: stills[] + asset_manifest (+ branded_stills if approved)
```

**`panda-image`** (`"pipeline": "panda-image"`). One still. Size is `options.aspect_ratio`
(default `1:1` — see [`IMAGE.md`](IMAGE.md)). Same dual-mode stills revise as carousel.

```
POST /jobs  { "pipeline": "panda-image", ... }
   │
   ▼
approve_scene_plan     artifacts: scene_plan (exactly 1 scene)
   │
   ▼
approve_stills         artifacts: stills[] (one UGC PNG)     approve│revise (edit|fresh)
   │
   ▼
approve_brand          artifacts: stills[]                   approve│skip│revise
   │
   ▼
done                   (+ branded_stills if approved)
```

`status` values: `running` (working, keep polling) · `awaiting_human` (a gate — act) · `done` (finished) · `failed` (see `question`).

> **The assets stage surfaces up to FOUR pauses.** `approve_stills`, `approve_motion_sample`, `budget_exceeded`, and `approve_assets` are `awaiting_human` pauses of the **same** `assets` stage (`stage:"assets"` at all of them). `approve_stills` shows **stills only** (no video — a rejection costs nothing). `approve_motion_sample` shows **one sample clip** so you approve the motion before the full batch — appears only when the `motion_sample` option is on (**default off**; pass `true` to opt in). `budget_exceeded` is **conditional** — it appears only if a generation would push cumulative Higgsfield spend past `max_higgsfield_credits`; the agent blocks *before* spending and you raise the cap / revise / cancel. `approve_assets` shows the full media set. **Tell them apart by the `gate` field — do not rely on `stage` alone.**

---

## 6. Job inputs

### `brief` (required)
Plain-language description of the video. **Be specific** — duration, what happens, the line to say. The agent fills gaps if vague. Examples:
- `"10-second vertical clip: panda mascot waves at an airport and says 'Grab a Panda Mobile eSIM before you fly.'"`
- `"30s eSIM tutorial: panda explains how to install an eSIM before travel, friendly and clear."`

### `pipeline` (optional, default `"panda-video"`)
Which manifest to run. `"panda-video"` is the full video (content gates + `approve_brand`). `"panda-carousel"` is the
stills-only sibling: `approve_script` → `approve_scene_plan` → `approve_stills` → `approve_brand` → `done`.
`"panda-image"` is a single still: `approve_scene_plan` → `approve_stills` → `approve_brand` → `done` (no script).
Persist this per job — mixed video + carousel + image on one launcher is supported.

### `profile` (optional, default `"ugc"`)
`"ugc"` = clean, **no branding baked in** (recommended). Branding is the **`approve_brand` gate**
after the last content gate — a post-cut overlay, never in generation. `skip` keeps UGC;
`POST /jobs/{id}/brand` remains for skip-then-brand-later.

### `options` (optional) — per-job control
| key | values | meaning |
|---|---|---|
| `language` | `"en"` \| `"zh"` | narration language (video) / primary on-slide language (carousel) |
| `narrator` | `"panda"` \| `"customer"` | which character narrates (video) |
| `voice_id` | ElevenLabs voice id (string) | **explicit override** — use this exact voice, ignore the default |
| `music` | mood string, or `false` | background music via ElevenLabs (`"upbeat, light"`), or `false` to skip |
| `render_runtime` | `"auto"` \| `"ffmpeg"` \| `"remotion"` \| `"hyperframes"` | which render engine composes the video. Default `"auto"` |
| `motion_sample` | `false` (default) \| `true` | insert the `approve_motion_sample` gate (video only; default off) |
| `max_higgsfield_credits` | integer, or unset | **hard credit ceiling** for the run |
| `aspect_ratio` | string | stills canvas, passed through to `generate_image`. Carousel default `"4:5"`; **panda-image** default `"1:1"`. Also `9:16`, `WIDTHxHEIGHT`, … |
| `gates` | e.g. `["scene_plan", "stills"]` | carousel only — omit `script` to auto-approve GATE 1 |

If `voice_id` is omitted, the engine picks the brand voice from config by `narrator`+`language`.

**`render_runtime`** (upstream-style engine selection):
- `"auto"` (default) — the engine picks per the decision matrix + what's installed on the box. For character-mascot ads this resolves to `ffmpeg`.
- `"ffmpeg"` — deterministic clean/brand render via `panda_render` (the Panda default; best for character clips).
- `"remotion"` — React motion-graphics (kinetic stat/text cards, charts, caption burn). Needs Node ≥ 22 + the remotion-composer project on the box.
- `"hyperframes"` — HTML/CSS/GSAP (kinetic typography, product-promo title cards). Needs Node ≥ 22 + headless Chrome on the box.
- If a requested runtime isn't available on the box, the job **fails with a clear error** rather than silently downgrading. Leave it `"auto"` unless you specifically want motion-graphics output.

---

## 7. Approve vs. Edit (per gate)

`POST /jobs/{id}/respond`:

| Body | Effect |
|---|---|
| `{"decision":"skip"}` | **at `approve_brand` only** — finish `done` with UGC (`branded: false`). Other gates → `400` |
| `{"decision":"approve"}` | accept this gate, advance to the next. At `approve_brand`, stamps BGC copies then `done` |
| `{"decision":"revise","answer":"<what to change>"}` | regenerate this gate's output honoring the note, stay at the same gate (at `approve_scene_plan` this rewrites the text plan; at `approve_stills` this regenerates stills unless `mode` is `edit`; at `approve_motion_sample` this regenerates only the sample clip; at `approve_brand` this stays at the gate with UGC unchanged — no regen) |
| `{"decision":"revise","shots":[1,3],"answer":"…"}` | at `approve_stills` (those scenes' stills) **or** `approve_assets` (those shots' clips) |
| `{"decision":"revise","mode":"edit","shots":[3],"answer":"…"}` | **at `approve_stills` only** (video, carousel, and image): image-to-image the flagged stills (keep composition; apply the note). `mode:"fresh"` regenerates from text + Element IDs and does not pass the old PNG. Omit `mode` to infer: `shots` + local-change language → edit; regenerate/redo/new → fresh; otherwise fresh |
| `{"decision":"approve","stills":["/abs/path.png", …]}` | **at `approve_stills` / `approve_assets`** — supply your own media instead of generated ones (associated with the asset manifest, not the scene plan) |
| `{"decision":"approve","max_higgsfield_credits":<n>}` | **at `budget_exceeded`** — raise the credit cap and resume generation (the agent re-checks before spending) |
| `{"decision":"cancel"}` | **at `budget_exceeded`** — stop the job; no further Higgsfield credits are spent |

> Approving `approve_stills` on **panda-video** does **not** finish the assets stage — with `motion_sample` off (**default**) the next gate is `approve_assets`; with it on (`motion_sample:true`), stills go to `approve_motion_sample` first. On **panda-carousel** and **panda-image**, approving stills opens **`approve_brand`** (not `done`). Dify must collect approve / skip / revise there before treating the job as finished.

Notes:
- Edits are a **text instruction** the agent acts on (not a manual pixel editor). More specific = closer result.
- `mode` (`fresh` | `edit`) applies only at `approve_stills`. Motion / clips / final revises stay regenerate-only. There is no image-to-image after `done`.
- After any `respond`, the job goes back to `running` — **poll again**.

---

## 8. Artifacts

Returned under `artifacts` in every state; grouped by kind:

| key | type | when |
|---|---|---|
| `script` | **inline JSON object** (`title`, `sections[]` with `text` + `speaker_directions`/`delivery_cues` — the actual dialogue; show it, don't fetch) | at the **script** gate |
| `script_md` | relative URL to `script.md` (same content as markdown) | at the script gate (and later, if the script is still on the job) |
| `scene_plan` | **inline JSON object** (the text plan — show it, don't fetch) | at the scene_plan gate |
| `scene_plan_md` | relative URL to `scene_plan.md` | at the scene_plan gate (and later, if the plan is still on the job) |
| `preview` | list of **one** relative URL — the current text gate’s `.md` | **`approve_script`** → `script.md`; **`approve_scene_plan`** → `scene_plan.md`. Absent at stills/clips/final. Bind this for Dify’s file-preview slot. |
| `stills` | list of image paths | at the **stills** gate (UGC originals; kept after `/brand`) |
| `preview` | list of **one** relative URL | at **`approve_stills`** → `storyboard.png` (shot grid + descriptions). Absent at later gates. Bind Dify’s file-preview slot here. After a per-shot revise, poll again — the PNG is rebuilt. |
| `storyboard_html` | relative URL to `storyboard.html` | same grid as HTML (sibling still filenames). |
| `branded_stills` | list of image paths | after `approve_brand` approve, or later `POST /jobs/{id}/brand` (BGC wordmark copies) |
| `clips` | list of video paths | at the **assets** gate (video pipeline) |
| `asset_manifest` | **inline JSON object** (all generated media: `path`+`scene_id` per asset) | at the **assets** / carousel stills-terminal gate |
| `final` | single MP4 path | after compose (video pipeline) — UGC master, kept after branding |
| `branded_final` | single MP4 path | after `approve_brand` approve or later `/brand` on a video job (`final.bgc.mp4`) |
| `branded` | bool | `false` until branding is applied; then `true` |
| `_checkpoint_artifacts` | raw structured data (render report, decision log) | context/debug |

Structured artifacts (`script`, `scene_plan`, `asset_manifest`) come as **inline JSON objects** — display them directly for review, no fetch needed. **You MUST show `script` at the `approve_script` gate** so the reviewer reads the actual dialogue before approving — do not just show the gate label. (If a pipeline ever emits the script only as a markdown file instead of structured JSON, `script` falls back to a **relative URL** to fetch — but the panda-video script-director emits structured JSON.)

**Dual-surface at text gates:** the same content is also written as `script.md` / `scene_plan.md`. `artifacts.preview` is a one-item list of that file’s URL for the **current** gate (`approve_script` → script.md, `approve_scene_plan` → scene_plan.md). Bind Dify’s file-preview node to `artifacts.preview` (or `script_md` / `scene_plan_md`) so the chat does not show “the engine sent no preview.” Do **not** put these `.md` files in `stills`.

File artifacts (`stills`, `clips`, `final`) come as **relative URLs** — fetch with `GET /jobs/{id}/artifacts/{basename}` (prepend the base URL). At **`approve_stills`**, `artifacts.preview` is `[…/storyboard.png]` (a Backlot-style shot grid with descriptions) — bind the file-preview slot to it. After `{decision:"revise","shots":[n]}`, poll again; the PNG is rebuilt. `preview` is dropped at later gates. Show `stills`/`clips` at the assets gate; show `final` at the final gate.

---

## 9. Full worked example (curl)

```bash
BASE=https://dev.om.mvnoc.ai; T=YOUR_TOKEN_HERE
# NOTE: the -H "X-Dify-Token: $T" header below is only needed if the server has DIFY_TOKEN set.
# In the default open mode (DIFY_TOKEN empty) you can drop every X-Dify-Token header.

# 1) start
curl -s -X POST $BASE/jobs -H "X-Dify-Token: $T" -H "Content-Type: application/json" \
 -d '{"brief":"10-second vertical clip: panda mascot waves at an airport and says grab a Panda Mobile eSIM before you fly","profile":"ugc","options":{"language":"en","narrator":"panda","music":"upbeat, light"}}'
# -> {"job_id":"job_xxxx","status":"running",...}

# 2) poll until awaiting_human (repeat every ~20s)
curl -s -H "X-Dify-Token: $T" $BASE/jobs/job_xxxx
# -> when status=awaiting_human, gate=approve_script, show artifacts.script

# 3) approve (or revise)
curl -s -X POST $BASE/jobs/job_xxxx/respond -H "X-Dify-Token: $T" -H "Content-Type: application/json" \
 -d '{"decision":"approve"}'
# -> status:running ; go back to (2). Repeat: scene_plan -> stills -> assets -> final -> approve_brand
#    (optional: pass motion_sample:true to insert approve_motion_sample after stills).

# 4) at approve_brand, approve (stamp), skip (UGC), or revise (stay). Then when status=done, download the video
curl -s -H "X-Dify-Token: $T" $BASE/jobs/job_xxxx/artifacts/final.mp4 -o final.mp4
```

---

## 10. Dify workflow blueprint

Build a **chatflow** (mirrors the existing Mochi v6e pattern with conversation variables):

1. **Start** — HTTP `POST /jobs` with the user's brief + options → store `job_id`, `status` in conversation variables.
2. **Poll loop** — HTTP `GET /jobs/{job_id}`; if `status == running`, wait ~20s and loop; if `awaiting_human`, exit loop.
3. **Present gate** — show `question` and render `artifacts` (inline `script` / `scene_plan` JSON, or `artifacts.preview` `.md` files at those gates; stills / clips / final at later gates).
4. **Collect reply** — user says approve or describes an edit.
5. **Respond** — HTTP `POST /respond` with `{"decision":"approve"}` or `{"decision":"revise","answer":"<user text>"}`.
6. **Repeat** 2–5 until `gate == approve_brand`, then collect approve / skip / revise. Do **not** treat the job as finished until after that choice. Then present `final.mp4` (and `branded_final` if approved).

**Conversation variables to keep:** `job_id`, last `status`, current `gate`.
**HTTP node timeouts:** 30–60s (calls are instant; the loop does the waiting).

---

## 11. Timing (so nothing looks stuck)
| stage | typical `running` time |
|---|---|
| script | ~1–3 min |
| scene_plan (text plan) | ~1–3 min |
| stills (images only, before video) | ~2–6 min |
| assets (clips + voice/music from approved stills) | ~5–15 min |
| compose (final) | seconds |

Long `running` stretches are **normal** — that's why it's async.

---

## 12. Errors & edge cases
| HTTP | meaning | Dify handling |
|---|---|---|
| `200` + `status:"failed"` | a stage errored | show `question` (the error); optionally restart or revise |
| `401` | bad/missing `X-Dify-Token` | fix the header |
| `404` | unknown `job_id` | check the id |
| `400` | `skip` at a gate other than `approve_brand` | only send `skip` at the brand gate |
| `409` | responded while still `running`, not at a gate, `/brand` at `approve_brand` or before `done` / with nothing to brand | keep polling until `awaiting_human` before `respond`; brand via `/respond` at the brand gate, or `/brand` only after `done` |

---

## 13. What the engine guarantees vs. what Dify must do
- **Engine guarantees:** stops at every gate (`awaiting_human`), surfaces the real output files, and honors approve/revise. It never auto-advances past a gate.
- **Dify must:** poll to detect the gate, **display each output to the user**, and collect approve/edit. If the Dify flow auto-approves, the user never sees the step — presenting + gating is the workflow's responsibility.

---

## 14. Scope notes
- **Branding** (logo/watermark) is **not** baked into generation and does **not** flow
  through animation. After the last content gate the launcher opens `approve_brand`.
  `approve` stamps the Panda wordmark onto **copies**: stills → `artifacts.branded_stills`;
  video master → `artifacts.branded_final` (`final.bgc.mp4`). `skip` finishes UGC-only.
  `revise` stays at the gate. UGC originals stay under `stills` / `final`. After skip,
  `POST /jobs/{id}/brand` with `{"profile":"bgc"}` can still stamp later. Video intro/outro
  cards are not part of this pass.
- **Voice/music** need `ELEVENLABS_API_KEY` configured on the server; otherwise narration falls back to a generic voice and music is skipped.
- **Consistency**: the panda/customer use fixed Higgsfield reference Elements, so the character stays on-model across shots.

---

## 15. Direct render door (`/montage/*`) — OPTIONAL, for driving the renderer yourself

Everything above (§1–§14) is the **agent pipeline**: you give a brief, the AI produces and gates a whole video. That is the main door and is unchanged.

There is a **second, independent door** for callers who already have the media (stills/clips/audio, e.g. from your own generation) and just want the **render engine** — no AI, no gates. It exposes montage-svc's raw abilities: **compose** (stitch clips + audio + captions into an MP4), **overlay** (re-stamp captions on an existing video), **mix-audio** (swap the audio track). Both doors call the **exact same render core**, so output quality is identical.

**Use the agent door (`/jobs`)** when you want the AI to create the video from a brief.
**Use the render door (`/montage`)** when you already have the assets and only need them assembled/edited.

> Choosing one **never affects** the other — they share the render code and the box, nothing else. You can ignore this whole section if you only use the agent pipeline.

### Auth (separate token)
These routes use their **own** header — `X-Panda-Token` (env `PANDA_TOKEN`), independent of the agent door's `X-Dify-Token`. Unset = open. `/montage/health` and `/montage/files/*` are always open.

### The model: import media → render → poll → fetch
A render works on a **run** (a workspace). You import each media file into the run under a short **`media_id`**, then reference those ids in a compose. Renders are async: **POST returns `202` + `job_id`; poll `GET /montage/jobs/{id}` until `done`.**

```
POST /montage/media/import   (once per file)        → {"media_id":"s000", ...}
POST /montage/compose         (reference the ids)   → 202 {"job_id":"mj_..."}
loop: GET /montage/jobs/{id}  every ~5–20s
        status == running|queued → keep polling
        status == done   → fetch output_media_url
        status == failed → read `error`
GET  <output_media_url>                              → the MP4
```

### Endpoints
| method + path | purpose |
|---|---|
| `GET /montage/health` | ffmpeg/fonts/profiles status |
| `POST /montage/media/import` | download a URL into a run: `{"run_id","url","label"}` → `{media_id,local_url,bytes,kind}` |
| `POST /montage/compose` | assemble scenes (+audio, captions, transition) → `202 {job_id}` |
| `POST /montage/overlay` | re-draw captions on an existing render by time window → `202 {job_id}` |
| `POST /montage/mix-audio` | replace the audio on an existing render → `202 {job_id}` |
| `GET /montage/jobs/{id}` | poll: `{status: queued\|running\|done\|failed, progress, output_media_url, error}` |
| `GET /montage/files/...` | download an output (the `output_media_url` from a finished job) |

### `POST /montage/compose` body
```json
{
  "run_id": "myrun",
  "version": 1,
  "profile": "ugc",
  "fps": 30,
  "resolution": "1080x1920",
  "scenes": [
    {"media_id": "s000", "duration_s": 2.5, "captions": {"en": "Panda Mobile"}},
    {"media_id": "s001", "duration_s": 3.0}
  ],
  "transition": {"type": "xfade", "duration_s": 0.4},
  "audio": {"music_media_id": "bgm", "voice_media_id": "vo", "music_db": -18, "voice_db": -6}
}
```
Key fields:
- **`duration_s` per scene is exact** — each clip is trimmed/padded to exactly that length (this is the per-clip timing control). Images become a clip of that length; a longer video is trimmed to it.
- `profile`: `"ugc"` (clean, no logo) or `"bgc"` (brand logo + cards).
- `resolution`/`fps`: `1080x1920` @ 30 for vertical. (60 fps triggers motion interpolation — slower.)
- `transition.type`: `"xfade"` (crossfade) or `"cut"`.
- `audio` ids must be imported first, same as scenes. `music_db`/`voice_db` set levels.
- Captions/overlays are **text** (caption lines, plus `bubble`/`callout` widgets). Overlaying an arbitrary **image** on top of a clip (logo/lower-third/PiP beyond the profile logo) is **not** supported today — an imported image becomes its own full-frame scene. Ask us if you need image overlays; it's a small, scoped addition.

### Worked example (curl)
```bash
BASE=https://dev.om.mvnoc.ai; PT=YOUR_PANDA_TOKEN

# import two clips into run "demo"
curl -s -X POST $BASE/montage/media/import -H "X-Panda-Token: $PT" -H "Content-Type: application/json" \
  -d '{"run_id":"demo","url":"https://cdn.example/clip1.mp4","label":"s000"}'
curl -s -X POST $BASE/montage/media/import -H "X-Panda-Token: $PT" -H "Content-Type: application/json" \
  -d '{"run_id":"demo","url":"https://cdn.example/clip2.mp4","label":"s001"}'

# compose
curl -s -X POST $BASE/montage/compose -H "X-Panda-Token: $PT" -H "Content-Type: application/json" \
  -d '{"run_id":"demo","version":1,"profile":"ugc","fps":30,
       "scenes":[{"media_id":"s000","duration_s":5},{"media_id":"s001","duration_s":5}],
       "transition":{"type":"xfade","duration_s":0.5}}'
# -> {"job_id":"mj_..."}

# poll, then download output_media_url
curl -s -H "X-Panda-Token: $PT" $BASE/montage/jobs/mj_xxxx
curl -s -H "X-Panda-Token: $PT" "$BASE/montage/files/runs/demo/out/final_v1.mp4" -o final.mp4
```

### Notes
- **Idempotent** on `(run_id, kind, version)`: re-POSTing the same compose returns the same `job_id` instead of rendering twice. Bump `version` to render a new cut in the same run.
- Runs persist (outputs stay under `/montage/files/runs/{run_id}/out/`), unlike the agent pipeline's per-job artifacts.
- This door does **no** generation and **no** gating — it's pure assembly/edit. Human review of its output, if any, is entirely up to your workflow.
