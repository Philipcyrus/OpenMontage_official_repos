# panda-carousel

Stills-only sibling of `panda-video`. Dify (or curl) sends `"pipeline": "panda-carousel"`;
the job stops after stills. No motion sample, clips, TTS, music, edit, or compose.

**Do not bind a test launcher to :8501** (live Dify). Use **:8600** and isolated data dirs.

Full API: [`DIFY_INTEGRATION.md`](DIFY_INTEGRATION.md). Verified run notes used to live only
in [`CAROUSEL_TEST.md`](CAROUSEL_TEST.md); the recorded walks are below.

---

## Gates

```
POST /jobs  { "pipeline": "panda-carousel", "brief": "…", "options": { … } }
   → approve_script          (skip with options.gates: ["scene_plan","stills"])
   → approve_scene_plan      TEXT plan + bilingual captions; no media yet
   → approve_stills          one PNG per slide (UGC)
   → approve_brand           approve stamps BGC copies; skip keeps UGC; revise stays
   → done
```

Stills stay UGC. Branding is a launcher overlay at `approve_brand` (not baked into
generation, does not flow through animation). `POST /jobs/{id}/brand` remains for
skip-then-brand-later (job already `done`).

---

## `options.aspect_ratio`

The **caller** sets the slide canvas. Omit it → **`4:5`**. The launcher and directors
pass the value through to Higgsfield `generate_image` (they do **not** rewrite it to
`4:5` / `1:1` only).

| `options.aspect_ratio` | Mock placeholder size | Typical use |
|---|---|---|
| `4:5` (default) | 1080×1350 | Instagram portrait feed |
| `1:1` | 1080×1080 | Rednote / WeChat / IG square |
| `9:16` | 1080×1920 | Stories / Reels stills |
| `3:4` | 1080×1440 | Taller portrait |
| `16:9` | 1920×1080 | Landscape |
| `4:3` | 1440×1080 | Landscape 4:3 |
| `WIDTHxHEIGHT` (e.g. `1080x1080`) | that exact size | explicit canvas |

Unknown `W:H` is scaled with **1080 on the short side**. Real Higgsfield output may differ
slightly from the mock sizes (e.g. 1024×1024 for 1:1) depending on the image model.

```json
{
  "brief": "6-slide IG carousel: grab a Panda Mobile eSIM before you fly",
  "pipeline": "panda-carousel",
  "options": { "aspect_ratio": "4:5", "language": "zh" }
}
```

---

## Stills revise (`approve_stills`)

```json
{"decision":"revise","mode":"edit","shots":[1],"answer":"remove extra pandas; keep the headline"}
{"decision":"revise","mode":"fresh","shots":[1],"answer":"different composition, panda on the right"}
```

| `mode` | Effect |
|---|---|
| `edit` | Image-to-image the flagged stills (import the PNG; keep composition; apply the note) |
| `fresh` | `generate_image` from text + panda Element IDs only — do not pass the old PNG |
| omitted | Infer: `shots` + local-change language → edit; regenerate/redo/new → fresh; else fresh |

`shots` are **1-based**. Motion / clips / final gates stay regenerate-only.

---

## Isolated test launcher (:8600)

Production Dify stays on **:8501**. For local walks:

```bash
# mock
DIFY_RUNNER=mock DIFY_DATA_DIR="$(pwd)/data/carousel-mock" \
  python3 -m uvicorn dify_launcher.app:app --host 127.0.0.1 --port 8600

# claude (source .env; isolated dirs)
set -a && source .env && set +a
export DIFY_RUNNER=claude DIFY_ASYNC=1
unset DIFY_TOKEN
export DIFY_DATA_DIR="$(pwd)/data/carousel-claude"
export OPENMONTAGE_PROJECTS_DIR="$(pwd)/projects-carousel-claude"
export PATH="$HOME/.nvm/versions/node/v22.23.2/bin:$HOME/.npm-global/bin:$PATH"
.venv/bin/python -m uvicorn dify_launcher.app:app --host 127.0.0.1 --port 8600
```

Claude `POST /jobs` returns `status: running` — poll `GET /jobs/{id}` until
`awaiting_human` / `done` / `failed`. Automated handshake (no server):

```bash
python3 dify_launcher/test_dify_flow.py
python3 dify_launcher/test_claude_adapter.py
```

Do not commit `data/carousel-*`, `data/storyboard-*`, or `projects-*-claude/` (local artifacts).

---

## Recorded scenarios (2026-08-13)

### 1. Mock HTTP — `job_db15e2f6a780`

| | |
|---|---|
| Runner / port | `mock` / 8600 |
| Options | `aspect_ratio: 4:5`, `language: zh` |
| Artifacts | `data/carousel-mock/jobs/job_db15e2f6a780/artifacts/` |
| Stills | `still_00.png` … `still_02.png` — **1080×1350** placeholders |
| Branded | `still_00.bgc.png` … — same size, wordmark top-right |
| Video | none |

Proves pipeline selector, bilingual captions, terminal stills, brand overlay idempotent.
Does **not** prove Higgsfield look. That recorded run branded via `POST /brand` after `done`;
current Dify flow collects the choice at `approve_brand`.

### 2. Claude 4-slide OnePool — `job_722bb5dbeb4f`

| | |
|---|---|
| Runner / port | `claude` (async) / 8600 |
| Options | `aspect_ratio: 1:1`, `language: zh`, `max_higgsfield_credits: 50` |
| Launcher | `data/carousel-claude/jobs/job_722bb5dbeb4f/artifacts/` |
| Engine | `projects-carousel-claude/job_722bb5dbeb4f/` |
| UGC stills | `slide-1.png` … `slide-4.png` — **1024×1024** (1:1) |
| Higgsfield | **6.0 credits** (`nano_banana_flash` × 4 × 1.5) |
| Time | ~13m 29s |

QA: slide 3 peeking pandas (mascot was 1+4 only); slide 1 baked a fake lockup;
BGC pill can overlap zh headlines.

### 3. Claude dual-mode 1-slide OnePool — `job_e9e6a924f2e3`

Skip script (`options.gates: ["scene_plan","stills"]`). Same OnePool hook copy.
`aspect_ratio: 1:1`. Walk copies (do not commit):

| File | Mode | What happened |
|---|---|---|
| [`data/carousel-claude-walk/01-original.png`](../data/carousel-claude-walk/01-original.png) | generate | panda **centered**, two homes + yellow data ribbon, 2048×2048 |
| [`data/carousel-claude-walk/02-edit.png`](../data/carousel-claude-walk/02-edit.png) | `mode=edit` `shots:[1]` | image-to-image (`source_media_id` recorded); layout kept |
| [`data/carousel-claude-walk/03-fresh.png`](../data/carousel-claude-walk/03-fresh.png) | `mode=fresh` `shots:[1]` | new composition, panda **on the right**; no old PNG |

Higgsfield **6 credits** (`nano_banana_2` × 3 × 2). Engine still:
`projects-carousel-claude/job_e9e6a924f2e3/assets/images/slide-1.png`.

The launcher marked the job `failed` after the fresh leg (Claude session limit after
generation). PNGs + `asset_manifest` on disk are the proof that edit vs fresh worked.

### 4. Claude 2-slide dual-mode — `job_f73ed248065a`

Skip script. Brief: exactly 2 slides (hook + CTA), OnePool eSIM.
`aspect_ratio: 1:1`, `language: zh`, cap 20. Job reached **`done`**.
Walk copies (do not commit):

| File | Mode | What happened |
|---|---|---|
| [`data/carousel-claude-walk/04-s1-original.png`](../data/carousel-claude-walk/04-s1-original.png) | generate | hook: panda **centered** at airport arrivals, blue-sky windows, 1024×1024 |
| [`data/carousel-claude-walk/04-s2-original.png`](../data/carousel-claude-walk/04-s2-original.png) | generate | CTA: panda **centered**, phone with QR, white bg |
| [`data/carousel-claude-walk/04-s1-edit.png`](../data/carousel-claude-walk/04-s1-edit.png) | `mode=edit` `shots:[1]` | sunset theme; same pose/layout/headline; `media_id` `4db9ac3e-…` as start image. Slide 2 bytes **unchanged** |
| [`data/carousel-claude-walk/04-s2-fresh.png`](../data/carousel-claude-walk/04-s2-fresh.png) | `mode=fresh` `shots:[2]` | new CTA: hero QR card, panda **on the right** pointing; old PNG **not** passed. Slide 1 edit **kept** |

Engine: `projects-carousel-claude/job_f73ed248065a/assets/images/slide-{1,2}.png`.
Four `nano_banana_2` stills × 2 credits = **8 credits** (cost_report.json shows 4 because it sums current manifest rows, not superseded gens). Active time **23m 1s**. Production `:8501` was not used.
