# panda-image

Single-still sibling of `panda-carousel`. Dify (or curl) sends `"pipeline": "panda-image"`;
the job produces **exactly one PNG** and stops. No script, motion, clips, TTS, music, or compose.

**Do not bind a test launcher to :8501** (live Dify). Use **:8600** and isolated data dirs.

Full API: [`DIFY_INTEGRATION.md`](DIFY_INTEGRATION.md). Carousel sibling: [`CAROUSEL.md`](CAROUSEL.md).

---

## Gates

```
POST /jobs  { "pipeline": "panda-image", "brief": "…", "options": { … } }
   → approve_scene_plan      TEXT plan: exactly 1 scene; no media yet
   → approve_stills          one PNG (UGC)
   → done
POST /jobs/{id}/brand  { "profile": "bgc" }   # optional wordmark copy
```

The still stays UGC. Branding is the later `/brand` pass, not baked into generation.

---

## `options.aspect_ratio`

The **caller** sets the canvas. Omit it → **`1:1`**. Pass-through to Higgsfield
`generate_image` (do **not** rewrite to `4:5` / `1:1` only). Same table as carousel:

| `options.aspect_ratio` | Mock placeholder size | Typical use |
|---|---|---|
| `1:1` (default) | 1080×1080 | Square post |
| `4:5` | 1080×1350 | Instagram portrait |
| `9:16` | 1080×1920 | Stories / Reels still |
| `3:4` | 1080×1440 | Taller portrait |
| `16:9` | 1920×1080 | Landscape |
| `4:3` | 1440×1080 | Landscape 4:3 |
| `WIDTHxHEIGHT` (e.g. `1080x1080`) | that exact size | explicit canvas |

```json
{
  "brief": "One square still: panda at the airport holding a phone with full signal.",
  "pipeline": "panda-image",
  "options": { "aspect_ratio": "1:1", "language": "zh" }
}
```

---

## Stills revise (`approve_stills`)

Same dual-mode as carousel, on the single still (`shots:[1]`):

```json
{"decision":"revise","mode":"edit","shots":[1],"answer":"warm sunset; keep composition and headline"}
{"decision":"revise","mode":"fresh","shots":[1],"answer":"entirely new composition; do not reuse the PNG"}
```

| `mode` | Effect |
|---|---|
| `edit` | Image-to-image the still (import the PNG; keep composition; apply the note) |
| `fresh` | `generate_image` from text + panda Element IDs only — do not pass the old PNG |
| omitted | Infer: `shots` + local-change language → edit; regenerate/redo/new → fresh; else fresh |

---

## Isolated test launcher (:8600)

Production Dify stays on **:8501**. For local walks:

```bash
# mock
DIFY_RUNNER=mock DIFY_DATA_DIR="$(pwd)/data/image-mock" \
  python3 -m uvicorn dify_launcher.app:app --host 127.0.0.1 --port 8600

# claude (source .env; isolated dirs)
set -a && source .env && set +a
export DIFY_RUNNER=claude DIFY_ASYNC=1
unset DIFY_TOKEN
export DIFY_DATA_DIR="$(pwd)/data/image-claude"
export OPENMONTAGE_PROJECTS_DIR="$(pwd)/projects-image-claude"
export PATH="$HOME/.nvm/versions/node/v22.23.2/bin:$HOME/.npm-global/bin:$PATH"
.venv/bin/python -m uvicorn dify_launcher.app:app --host 127.0.0.1 --port 8600
```

Claude `POST /jobs` returns `status: running` — poll `GET /jobs/{id}` until
`awaiting_human` / `done` / `failed`. Automated handshake (no server):

```bash
python3 dify_launcher/test_dify_flow.py
python3 dify_launcher/test_claude_adapter.py
python3 -m pytest tests/contracts/test_panda_image_pipeline.py -q
```

Do not commit `data/image-*` or `projects-image-claude/` (local artifacts).

---

## Recorded scenarios (2026-08-13)

### Claude dual-mode OnePool — `job_23513f0e535c`

Isolated Claude launcher on **:8600** (`DIFY_DATA_DIR=data/image-claude`,
`OPENMONTAGE_PROJECTS_DIR=projects-image-claude`). Production **:8501** untouched.
`pipeline: panda-image`, `aspect_ratio: 1:1`, `language: zh`, `max_higgsfield_credits: 20`.

Gates: `approve_scene_plan` (exactly 1 scene, bilingual captions, no media) → generate one
still → `mode=edit` `shots:[1]` → `mode=fresh` `shots:[1]` → approve → `done` → `POST /brand`.

| | |
|---|---|
| Runner / port | `claude` (async) / 8600 |
| Launcher | `data/image-claude/jobs/job_23513f0e535c/artifacts/` |
| Engine | `projects-image-claude/job_23513f0e535c/` |
| Credits | 6 Higgsfield (`nano_banana_flash` × 3 stills × 2 credits). No ElevenLabs / video |
| Video | none |

Walk PNGs (gitignored):

| file | step | notes |
|---|---|---|
| [`data/image-claude-walk/01-original.png`](../data/image-claude-walk/01-original.png) | generate | panda **centered** in arrivals hall, phone with full signal, 2048×2048 |
| [`data/image-claude-walk/02-edit.png`](../data/image-claude-walk/02-edit.png) | `mode=edit` `shots:[1]` | sunset/warm light; same pose/layout/headline; `source_media_id` `6e806c7f-…` |
| [`data/image-claude-walk/03-fresh.png`](../data/image-claude-walk/03-fresh.png) | `mode=fresh` `shots:[1]` | new two-column layout, panda **on the right** pointing at phone/QR; old PNG **not** passed (`source_media_id: null`) |
| [`data/image-claude-walk/04-brand.png`](../data/image-claude-walk/04-brand.png) | `POST /brand` `{profile: bgc}` | BGC wordmark on a copy of the fresh still; UGC `slide-1.png` kept |

Engine current still: `projects-image-claude/job_23513f0e535c/assets/images/slide-1.png`.
Superseded generate/edit PNGs live under `history/superseded-stills/`.
`partial_progress.phase` was `"stills"` at the stills gate. Checkpoint
`pipeline_type` was `panda-image`. No `approve_script` gate.
