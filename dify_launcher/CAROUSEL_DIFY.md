# Panda Carousel — Dify Wiring Guide

**Audience:** the Dify workflow team.
**What this covers:** how to drive the **stills-only carousel** pipeline from Dify. It reuses the
*same* launcher API and the *same* gate loop as the video workflow — only the `pipeline` field, the
gate list, and a couple of revise options differ.

> If you already have the **video** workflow working, carousel is a small delta: send
> `"pipeline": "panda-carousel"` on start, and walk a shorter gate list. No new base URL, no new
> auth, no new endpoints.

Related docs: [`DIFY_INTEGRATION.md`](DIFY_INTEGRATION.md) (full API), [`CAROUSEL.md`](CAROUSEL.md)
(engineering notes + recorded runs), [`README.md`](README.md) (endpoint table).

---

## 1. Connection

- **Base URL:** the launcher on `:8501`.
- **Auth:** if `DIFY_TOKEN` is set on the launcher, every HTTP node must send header
  `X-Dify-Token: <token>`. If it isn't set, no header is needed.
- All request bodies are JSON (`Content-Type: application/json`).
- **Async model:** with the claude runner, `POST /jobs` and `POST /jobs/{id}/respond` return
  immediately with `status: "running"`. You **poll** `GET /jobs/{id}` until the status becomes a
  gate (`awaiting_human`) or terminal (`done` / `failed`). Do not expect the gate in the POST
  response.

---

## 2. The gate loop (same 4 nodes as video)

```
[Start]   POST /jobs                → { id, status:"running", ... }
   │
   ▼
[Poll]    GET /jobs/{id}  (repeat)  → until status == "awaiting_human"  (a gate)
   │                                   or status in ("done","failed")   (terminal)
   ▼
[Display] read `gate` + the artifact for THIS gate (see §4)  → ask the user approve / revise
   │
   ▼
[Respond] POST /jobs/{id}/respond { decision, ... }  → back to [Poll] for the next gate
```

**Statuses you will see in `GET /jobs/{id}`:**

| `status` | Meaning | What the workflow does |
|---|---|---|
| `running` | engine is working | keep polling |
| `awaiting_human` | a gate is open — read `gate` | display the artifact, collect approve/revise |
| `done` | finished | show final stills; optionally offer branding (§7) |
| `failed` | stopped with an error in `question` | surface the error |

Every gate response has this shape:
```json
{ "id": "job_…", "status": "awaiting_human", "stage": "…", "gate": "…",
  "question": "…", "artifacts": { … } }
```

---

## 3. Start node — what makes it a carousel

```json
POST /jobs
{
  "brief": "6-slide IG carousel: grab a Panda Mobile eSIM before you fly",
  "pipeline": "panda-carousel",
  "options": {
    "aspect_ratio": "4:5",
    "language": "zh"
  }
}
```

- **`pipeline: "panda-carousel"`** is the only required difference from video.
- Carousel gate sequence (shorter than video — no motion/clips/audio):

  ```
  approve_script → approve_scene_plan → approve_stills → done
  ```

See §6 for the full `options` reference.

---

## 4. Per-gate display — bind to these exact fields ⚠️

This is the fix for the **"the engine sent no preview"** message. The launcher sends the
content **inline as JSON** *and* as a fetchable markdown file. Bind **either** (or both).
Branch on the `gate` field:

| `gate` | Show this from the GET response | Notes |
|---|---|---|
| `approve_script` | **`artifacts.script`** (inline JSON) **and/or** **`artifacts.preview`** | Slide/dialogue copy is `artifacts.script.sections[].text`. `preview` is `[…/script.md]` — fetch that file for the file-preview slot. |
| `approve_scene_plan` | **`artifacts.scene_plan`** (inline JSON) **and/or** **`artifacts.preview`** | TEXT plan, no media yet. Carries **per-slide bilingual captions (zh + en)** and `metadata.aspect_ratio`. `preview` is `[…/scene_plan.md]` (this gate only — not leftover script.md). |
| `approve_stills` | the still images | These are **media**: `artifacts.stills` is a list of filenames — fetch each via `GET /jobs/{id}/artifacts/{name}` and show inline. `preview` is **absent**. |
| `budget_exceeded` | `question` (the credit warning) | Conditional — see §5. |

**Rule of thumb:** at `approve_script` and `approve_scene_plan` render the JSON **or** fetch `artifacts.preview`. Only at `approve_stills` (and later video gates) do you fetch stills/clips/final.

---

## 5. Respond node — approve & revise

Endpoint: `POST /jobs/{id}/respond`. Then go back to polling.

### Approve (any gate)
```json
{ "decision": "approve" }
```
Approving `approve_stills` **finishes the carousel** (`status` becomes `done`).

### Revise TEXT (script or scene_plan)
```json
{ "decision": "revise", "answer": "make the captions punchier; add a CTA slide" }
```

### Revise STILLS (`approve_stills`) — `mode` + `shots`
Stills revise supports a **mode** and 1-based slide selection:
```json
{ "decision": "revise", "mode": "edit",  "shots": [1],    "answer": "remove extra pandas; keep the headline" }
{ "decision": "revise", "mode": "fresh", "shots": [1, 3], "answer": "new composition, panda on the right" }
```

| Field | Meaning |
|---|---|
| `mode: "edit"` | image-to-image the flagged slide(s) — keeps composition, applies the note |
| `mode: "fresh"` | regenerate from text + panda Elements (old image not reused) |
| `mode` omitted | inferred from the wording; defaults to `fresh` |
| `shots` | **1-based** slide numbers to revise; omit / empty ⇒ all slides |
| `answer` | the revision note |

> Advanced (optional): the API also accepts a `stills: ["/path/a.png", …]` array to supply your
> own images instead of generating. For carousel, lead with `mode`/`shots`; treat `stills[]` as the
> bring-your-own-image path.

### Conditional gate: `budget_exceeded`
Only appears when the job set `options.max_higgsfield_credits` **and** a stills batch would exceed
it. The run pauses at `gate: "budget_exceeded"` **before** spending — it never overspends silently.
Your poll loop must handle this gate. Options for the user:
- **raise the cap** and continue — start the flow with a higher `max_higgsfield_credits` (a new job),
- **revise** to reduce scope, or
- **cancel**.

If `max_higgsfield_credits` is not set, this gate never appears.

---

## 6. `options` reference (carousel)

| Option | Default | Notes |
|---|---|---|
| `aspect_ratio` | `4:5` | Slide canvas. Also `1:1`, `9:16`, `3:4`, `16:9`, `4:3`, or `WIDTHxHEIGHT`. Pass-through — a caller-set ratio is **not** rewritten. |
| `language` | `en` | Primary baked-in slide language (`zh` etc.). scene_plan still carries zh + en captions. |
| `gates` | `["script","scene_plan","stills"]` | Which gates to surface. **Omit `"script"`** to auto-approve GATE 1 and start at scene_plan. |
| `max_higgsfield_credits` | unset | Credit ceiling. When set, a batch that would exceed it pauses at `budget_exceeded` (§5). |

Aspect-ratio placeholder sizes and the full option semantics are in [`CAROUSEL.md`](CAROUSEL.md).

---

## 7. After `done` — optional branding

Branding is **not** a gate. Once `status == "done"`, if the user wants the branded (BGC) version
with the Panda wordmark stamped onto the slides:
```json
POST /jobs/{id}/brand
{ "profile": "bgc" }
```
- Only `bgc` is implemented.
- The original UGC stills in `artifacts.stills` are left untouched; branded copies are written and
  listed under **`artifacts.branded_stills`** (fetch via `/artifacts/{name}`).
- Idempotent — safe to call more than once.

---

## 8. Endpoint quick reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | start a carousel job (`pipeline: "panda-carousel"`) → first gate |
| `GET`  | `/jobs/{id}` | poll: `{ status, stage, gate, question, artifacts }` |
| `POST` | `/jobs/{id}/respond` | `{ decision: approve\|revise, … }` → resume to next gate |
| `POST` | `/jobs/{id}/brand` | `{ profile: "bgc" }` — brand approved stills (job must be `done`) |
| `GET`  | `/jobs/{id}/artifacts/{name}` | download a **media** file (a still / branded still) |
| `GET`  | `/jobs/{id}/cost` | per-job Higgsfield credits + time report |

---

## 9. Two ways to expose carousel in Dify

Both are fine — (a) is simpler to reason about:

- **(a) A second workflow** cloned from the video one, with `pipeline` hardcoded to
  `panda-carousel` and the motion/audio gates removed (carousel only has 3 gates + the conditional
  `budget_exceeded`).
- **(b) A toggle** in the existing workflow that sets the `pipeline` field and collapses the gate
  loop to the carousel gates.

---

## 10. Minimal end-to-end (curl, for reference)

```bash
# 1. start
CID=$(curl -s -X POST localhost:8501/jobs \
  -H 'Content-Type: application/json' \
  -d '{"brief":"3-slide panda eSIM promo","pipeline":"panda-carousel","options":{"aspect_ratio":"4:5"}}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 2. poll until a gate
curl -s localhost:8501/jobs/$CID | python -m json.tool     # look for gate + artifacts.script

# 3. approve the script
curl -s -X POST localhost:8501/jobs/$CID/respond -H 'Content-Type: application/json' \
  -d '{"decision":"approve"}'

# …repeat poll+respond for approve_scene_plan, then approve_stills → done

# 4. (optional) brand the approved stills
curl -s -X POST localhost:8501/jobs/$CID/brand -H 'Content-Type: application/json' \
  -d '{"profile":"bgc"}'
```
