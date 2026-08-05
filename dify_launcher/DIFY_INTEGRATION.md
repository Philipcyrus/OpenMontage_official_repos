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
- The pipeline **pauses for human approval at 4 gates**: script → storyboard → clips → final.
- Real generation takes **minutes**, so the API is **asynchronous**: you `POST`, then **poll** `GET` until the gate is ready. (See §4 — this is the single most important thing to build correctly.)
- Output at each gate is a set of **real downloadable files** (script, stills, clips, final MP4).

---

## 2. Base URL & auth

| | |
|---|---|
| **Base URL** | `https://dev.om.mvnoc.ai` |
| **Auth** | Header `X-Dify-Token: <token>` on **every** request (value supplied separately). If the server has no token configured, the header is ignored — but assume it's required. |
| **Content type** | `application/json` for all POST bodies. |

A bad/missing token when one is configured → `401`.

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
  "profile": "ugc",
  "options": { "language": "en", "narrator": "panda", "music": "upbeat, light" }
}
```
Returns **immediately**:
```json
{"job_id":"job_xxxx","status":"running","stage":null,"gate":null,"question":"starting…","artifacts":{}}
```
→ **Save `job_id`.** Then poll (§4).

### `GET /jobs/{job_id}` — current state (poll this)
```json
{"job_id":"job_xxxx","status":"awaiting_human","stage":"scene_plan",
 "gate":"approve_storyboard","question":"Approve the storyboard stills…",
 "artifacts":{"stills":["/jobs/job_xxxx/artifacts/scene-1.png", …]}}
```

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

### `GET /jobs/{job_id}/artifacts/{name}` — download a file
e.g. `GET /jobs/job_xxxx/artifacts/final.mp4`. Returns the binary file.
> Artifact paths in responses are **relative** (`/jobs/.../x.png`). Prepend the base URL to fetch/display them.

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
        status == done        → fetch final.mp4
        status == failed      → show `question` (the error)
```

**Do not** set a long HTTP timeout hoping the POST returns the result — it won't. Set HTTP node timeouts to **30–60s**; all calls return quickly. The waiting happens in the poll loop.

---

## 5. The gate sequence (state machine)

```
POST /jobs
   │  (agent: idea + script)
   ▼
approve_script        artifacts: script            approve│revise
   │
   ▼
approve_storyboard    artifacts: stills[]           approve│revise│supply-own-stills
   │
   ▼
approve_clips         artifacts: clips[]            approve│revise (per-shot)
   │
   ▼
approve_final         artifacts: final (MP4)        approve│revise
   │
   ▼
done                  artifacts: final              (branding is a SEPARATE later step)
```

`status` values: `running` (working, keep polling) · `awaiting_human` (a gate — act) · `done` (finished) · `failed` (see `question`).

---

## 6. Job inputs

### `brief` (required)
Plain-language description of the video. **Be specific** — duration, what happens, the line to say. The agent fills gaps if vague. Examples:
- `"10-second vertical clip: panda mascot waves at an airport and says 'Grab a Panda Mobile eSIM before you fly.'"`
- `"30s eSIM tutorial: panda explains how to install an eSIM before travel, friendly and clear."`

### `profile` (optional, default `"ugc"`)
`"ugc"` = clean, **no branding baked in** (recommended). Branding (logo/watermark) is a **separate on-demand step applied after final approval** — never in the base video.

### `options` (optional) — per-job control
| key | values | meaning |
|---|---|---|
| `language` | `"en"` \| `"zh"` | narration language → selects the matching brand voice |
| `narrator` | `"panda"` \| `"customer"` | which character narrates |
| `voice_id` | ElevenLabs voice id (string) | **explicit override** — use this exact voice, ignore the default |
| `music` | mood string, or `false` | background music via ElevenLabs (`"upbeat, light"`), or `false` to skip |

If `voice_id` is omitted, the engine picks the brand voice from config by `narrator`+`language`.

---

## 7. Approve vs. Edit (per gate)

`POST /jobs/{id}/respond`:

| Body | Effect |
|---|---|
| `{"decision":"approve"}` | accept this stage, advance to the next gate |
| `{"decision":"revise","answer":"<what to change>"}` | regenerate this stage honoring the note, stay at the same gate |
| `{"decision":"approve","stills":["/abs/path.png", …]}` | **at storyboard only** — supply your own stills instead of the generated ones |
| `{"decision":"revise","shots":[1,3],"answer":"more motion"}` | **at clips only** — regenerate just those shot indices |

Notes:
- Edits are a **text instruction** the agent acts on (not a manual pixel editor). More specific = closer result.
- After any `respond`, the job goes back to `running` — **poll again**.

---

## 8. Artifacts

Returned under `artifacts` in every state; grouped by kind:

| key | type | when |
|---|---|---|
| `script` | file (or inline under `_checkpoint_artifacts.script`) | after script stage |
| `stills` | list of image paths | after storyboard |
| `clips` | list of video paths | after clips |
| `final` | single MP4 path | after compose |
| `branded` | bool (always `false` in base video) | after compose |
| `_checkpoint_artifacts` | raw structured data (script JSON, asset manifest, render report) | context/debug |

Fetch any with `GET /jobs/{id}/artifacts/{basename}` (prepend base URL to the relative path). Display `stills`/`clips`/`final` to the user at their gates.

---

## 9. Full worked example (curl)

```bash
BASE=https://dev.om.mvnoc.ai; T=YOUR_TOKEN_HERE

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
# -> status:running ; go back to (2). Repeat: storyboard -> clips -> final.

# 4) when status=done, download the video
curl -s -H "X-Dify-Token: $T" $BASE/jobs/job_xxxx/artifacts/final.mp4 -o final.mp4
```

---

## 10. Dify workflow blueprint

Build a **chatflow** (mirrors the existing Mochi v6e pattern with conversation variables):

1. **Start** — HTTP `POST /jobs` with the user's brief + options → store `job_id`, `status` in conversation variables.
2. **Poll loop** — HTTP `GET /jobs/{job_id}`; if `status == running`, wait ~20s and loop; if `awaiting_human`, exit loop.
3. **Present gate** — show `question` and render `artifacts` (display the stills/clip/final to the user).
4. **Collect reply** — user says approve or describes an edit.
5. **Respond** — HTTP `POST /respond` with `{"decision":"approve"}` or `{"decision":"revise","answer":"<user text>"}`.
6. **Repeat** 2–5 until `status == done`, then present `final.mp4`.

**Conversation variables to keep:** `job_id`, last `status`, current `gate`.
**HTTP node timeouts:** 30–60s (calls are instant; the loop does the waiting).

---

## 11. Timing (so nothing looks stuck)
| stage | typical `running` time |
|---|---|
| script | ~1–3 min |
| storyboard (stills) | ~3–8 min |
| clips (image→video) | ~5–15 min |
| compose (final) | seconds |

Long `running` stretches are **normal** — that's why it's async.

---

## 12. Errors & edge cases
| HTTP | meaning | Dify handling |
|---|---|---|
| `200` + `status:"failed"` | a stage errored | show `question` (the error); optionally restart or revise |
| `401` | bad/missing `X-Dify-Token` | fix the header |
| `404` | unknown `job_id` | check the id |
| `409` | responded while still `running`, or not at a gate | keep polling until `awaiting_human` before `respond` |

---

## 13. What the engine guarantees vs. what Dify must do
- **Engine guarantees:** stops at every gate (`awaiting_human`), surfaces the real output files, and honors approve/revise. It never auto-advances past a gate.
- **Dify must:** poll to detect the gate, **display each output to the user**, and collect approve/edit. If the Dify flow auto-approves, the user never sees the step — presenting + gating is the workflow's responsibility.

---

## 14. Scope notes
- **Branding** (logo/watermark/cards) is **not** in the base video — it's a separate post-approval step (`panda_brand`), applied only when explicitly requested.
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
