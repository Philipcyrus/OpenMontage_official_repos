# Dify Launcher

The **HTTP service Dify talks to.** The OpenMontage engine is not a service — it's an agent
that runs per job. This launcher starts/resumes agent runs and surfaces the five approval
gates so Dify can show them to the user and collect responses. Storage is **local** (a folder
per job); S3/Postgres backends can be swapped in later without touching the API.

```
Dify ──HTTP──▶ Dify Launcher ──▶ runner ──▶ agent/pipeline ──▶ local artifacts
                    ▲                 │
                    └── awaiting_human at each gate ──┘
```

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET  | `/health` | liveness + which runner is active |
| POST | `/jobs` | start a run from `{brief, profile?, options?}` → stops at GATE 1 |
| GET  | `/jobs/{id}` | current `{status, stage, gate, question, artifacts}` |
| POST | `/jobs/{id}/respond` | `{decision: approve\|revise, answer?, stills?}` → resume to next gate |
| GET  | `/jobs/{id}/artifacts/{name}` | download a script / still / final.mp4 / `cost_report.md` |
| GET  | `/jobs/{id}/cost` | per-project cost & time report — Higgsfield credits, ElevenLabs usage, generation time (native units) |

**Gate sequence** (matches `pipeline_defs/panda-video.yaml`):
`start → approve_script → approve_scene_plan → approve_stills → approve_motion_sample → approve_assets → approve_final → done`.
`approve_stills`, `approve_motion_sample`, and `approve_assets` are pauses of the **same** `assets`
stage (tell them apart by the `gate` field). `approve_motion_sample` (one hero clip, approve the
motion before batching) appears only when the `motion_sample` option is on (**default**); it's
skipped when off. Branding is **not** a gate — it's an on-demand step after `approve_final`.

At `approve_scene_plan` the reviewer approves a structured **text** plan — no media yet.

At `approve_stills` / `approve_assets`, Dify may pass user-supplied media instead of generated:
`POST /jobs/{id}/respond {"decision":"approve","stills":["/path/a.png","/path/b.png"]}`.

At the **assets** gate, every generated shot is reviewed together; revise specific shots:
`POST /jobs/{id}/respond {"decision":"revise","shots":[1,4]}` regenerates only those.

## Runners (env `DIFY_RUNNER`)
- **`mock`** (default) — no LLM, no Higgsfield. Fakes script + scene_plan + stills and REALLY renders a
  clean master via the folded `panda_render`. Lets you test the whole Dify handshake locally.
- **`claude`** — the EC2 path (implemented in `runner.py`). Each start/resume runs Claude Code
  headless (`claude -p`) against the engine repo; OpenMontage's checkpoint-based resume means
  every leg reads the latest checkpoint and continues to the next gate. The runner maps
  checkpoints → gates and mirrors artifacts into the job store. Needs `claude` + OpenRouter env
  + the Higgsfield MCP on the box. Config: `CLAUDE_BIN`, `CLAUDE_EXTRA_ARGS`, `CLAUDE_TIMEOUT_S`,
  `PANDA_PIPELINE_TYPE`, `OPENMONTAGE_PROJECTS_DIR` (see `.env.example`).
  **Verify on the box:** exact `claude` flags, the agent's stop-at-gate behavior, and the
  artifact key/paths the panda-video skills emit (see `_mirror_artifacts`).

## Tests
- `python dify_launcher/test_dify_flow.py` — full 5-gate handshake on the mock runner (real render)
- `python dify_launcher/test_claude_adapter.py` — the claude runner's checkpoint adapter
  (gate mapping, artifact mirroring, sync, approval) against the real `lib/checkpoint`

## Run it
```bash
pip install -r dify_launcher/requirements.txt
# local test (no server, no LLM): full gate flow + a real render
python dify_launcher/test_dify_flow.py
# serve for Dify to call:
DIFY_RUNNER=mock uvicorn dify_launcher.app:app --host 0.0.0.0 --port 8600
```

## Config (env)
- `DIFY_RUNNER` — `mock` (default) | `claude`
- `DIFY_DATA_DIR` — job storage root (default `./data`; `data/jobs/` is gitignored)
- `DIFY_TOKEN` — optional shared secret; **empty = no auth**, set = callers must send `X-Dify-Token`
- `PANDA_TOKEN` — optional secret for the `/montage/*` raw-render door (`X-Panda-Token`), independent of `DIFY_TOKEN`

## Job options (`POST /jobs` `options`)
- `language`, `narrator`, `voice_id`, `music`
- `render_runtime` — `auto` (default) | `ffmpeg` | `remotion` | `hyperframes`. The `ffmpeg`
  lane (`panda_render`) needs no Node; `remotion`/`hyperframes` need **Node ≥ 22** on the box.
- `motion_sample` — `true` (default) | `false`. When on, adds the `approve_motion_sample` gate
  (one hero clip approved before the full batch). Set `false` for quick drafts.

## Connecting Dify
Point Dify's HTTP/tool nodes at this service's base URL:
1. **Start** → `POST /jobs` with the brief; show `question` + the `script` artifact.
2. On user approve/revise → `POST /jobs/{id}/respond`; repeat through `scene_plan → stills → motion_sample → assets → final` (`motion_sample` gate is on by default; set option `motion_sample:false` to skip it).
3. Render the `final` artifact inline; on approve the job is `done`.
4. Fetch `GET /jobs/{id}/cost` (or the `cost_report.md` artifact) for the per-project credits/time report.
5. Panda branding is a separate, on-demand `panda_brand` step applied only after final approval.

## Raw render door (`/montage/*`)
A second, independent entrance to the same vendored render core (compose / overlay / mix-audio),
for direct rendering outside the agent pipeline. Own auth (`X-Panda-Token`), mounted defensively
(a failure there never takes down the launcher). See `DIFY_INTEGRATION.md` §15.
