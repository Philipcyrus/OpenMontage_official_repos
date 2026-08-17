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
| `awaiting_human` | a gate is open — read `gate` | display the artifact, collect approve/revise (or skip at `approve_brand`) |
| `done` | finished | show UGC stills and, if `branded`, the BGC copies |
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
  approve_script → approve_scene_plan → approve_stills → approve_brand → done
  ```

See §6 for the full `options` reference.

---

## 4. Per-gate display — bind to these exact fields ⚠️

This is the fix for the **"the engine sent no preview"** message. The launcher already sends the
content **inline** in the `GET /jobs/{id}` response — the Display node just has to read it out.
Branch on the `gate` field:

| `gate` | Show this from the GET response | Notes |
|---|---|---|
| `approve_script` | **`artifacts.script`** (inline JSON) | One section per slide. Slide copy is in `artifacts.script.sections[].text`. Render the sections — **do not** call `/artifacts/…`. |
| `approve_scene_plan` | **`artifacts.scene_plan`** (inline JSON) | TEXT plan, no media yet. Carries **per-slide bilingual captions (zh + en)** and `metadata.aspect_ratio`. |
| `approve_stills` | the still images | These are **media**: `artifacts.stills` is a list of filenames — fetch each via `GET /jobs/{id}/artifacts/{name}` and show inline. |
| `approve_brand` | UGC stills again | Collect **approve** (stamp BGC copies), **skip** (keep UGC), or **revise** (ask again). Branding does not flow through animation. |
| `budget_exceeded` | `question` (the credit warning) | Conditional — see §5. |

**Rule of thumb:** at `approve_script` and `approve_scene_plan` the content is **inline JSON in the
poll response**. At `approve_stills` and `approve_brand` you fetch files via `/artifacts/{name}`.

---

## 5. Respond node — approve & revise

Endpoint: `POST /jobs/{id}/respond`. Then go back to polling.

### Approve (any gate)
```json
{ "decision": "approve" }
```
Approving `approve_stills` opens **`approve_brand`** (not `done`). At that gate:

```json
{ "decision": "approve" }   // stamp BGC copies, then done
{ "decision": "skip" }      // done with UGC only
{ "decision": "revise" }    // stay at approve_brand; UGC unchanged
```

`skip` is **only** valid at `approve_brand` — other gates return `400`. Dify must collect this
choice before treating the carousel as finished.

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

## 7. Branding — `approve_brand`, then optional `/brand`

Branding is a **launcher gate** after stills (not an engine stage, not Higgsfield). Show the UGC
stills and collect approve / skip / revise as in §5.

`POST /jobs/{id}/brand` remains for jobs already `done` (skip-then-brand-later):
```json
POST /jobs/{id}/brand
{ "profile": "bgc" }
```
- Only `bgc` is implemented. `409` if `gate` is still `approve_brand` — use `/respond` instead.
- The original UGC stills in `artifacts.stills` are left untouched; branded copies are written and
  listed under **`artifacts.branded_stills`** (fetch via `/artifacts/{name}`).
- Idempotent — safe to call more than once.
- Branding does not flow through animation.

---

## 8. Endpoint quick reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | start a carousel job (`pipeline: "panda-carousel"`) → first gate |
| `GET`  | `/jobs/{id}` | poll: `{ status, stage, gate, question, artifacts }` |
| `POST` | `/jobs/{id}/respond` | `{ decision: approve\|revise\|skip, … }` → resume (`skip` only at `approve_brand`) |
| `POST` | `/jobs/{id}/brand` | `{ profile: "bgc" }` — brand approved stills (job must already be `done`) |
| `GET`  | `/jobs/{id}/artifacts/{name}` | download a **media** file (a still / branded still) |
| `GET`  | `/jobs/{id}/cost` | per-job Higgsfield credits + time report |

---

## 9. Two ways to expose carousel in Dify

Both are fine — (a) is simpler to reason about:

- **(a) A second workflow** cloned from the video one, with `pipeline` hardcoded to
  `panda-carousel` and the motion/audio gates removed (carousel has script / scene_plan / stills /
  `approve_brand`, plus the conditional `budget_exceeded`).
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

# …repeat poll+respond for approve_scene_plan, then approve_stills → approve_brand

# 4. branding choice (approve stamps, skip keeps UGC, revise stays)
curl -s -X POST localhost:8501/jobs/$CID/respond -H 'Content-Type: application/json' \
  -d '{"decision":"skip"}'
```
