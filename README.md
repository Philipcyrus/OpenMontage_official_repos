<h1 align="center">Panda AI Video Engine</h1>

<p align="center"><strong>Agent-driven video production for Panda Mobile — brief in, branded video out, with human approval at every step.</strong></p>

<p align="center">
  <a href="#what-this-is">What it is</a> &nbsp;·&nbsp;
  <a href="#architecture">Architecture</a> &nbsp;·&nbsp;
  <a href="#the-approval-gates">Gates</a> &nbsp;·&nbsp;
  <a href="#quick-start-ec2">Quick Start</a> &nbsp;·&nbsp;
  <a href="#testing">Testing</a> &nbsp;·&nbsp;
  <a href="#repo-layout">Layout</a>
</p>

---

## What this is

A production system that turns a **brief** into a finished **Panda Mobile** video. A person (via
**Dify**) submits the brief and reviews the work at each stage; an **agent** (Claude Code, driving
the [OpenMontage](https://github.com/calesthio/OpenMontage) pipeline engine) does the production —
script → scene plan (text) → stills → motion clips + audio → assembled video. Generation runs
through the **Higgsfield MCP**; the final render (assembly, CJK captions, audio) is produced in-process by
montage-svc's craft code, **folded into this repo**. Panda **branding** (logo, watermark, cards)
is applied **only on request, after the video is approved** — never baked in.

Everything runs behind **one HTTP service** (the Dify launcher) on **port 8501**.

## Architecture

```
Dify (dev.om.mvnoc.ai)                     ← brief in, reviews + approvals
      │  HTTP  (:8501)
Dify Launcher  (dify_launcher/)            ← the ONLY service; starts/resumes runs, serves gates
      │  starts per job
Claude Code + OpenMontage pipeline         ← the agent: script → scene_plan (text) → stills → assets → compose
      ├─ Higgsfield MCP        → video/image generation (no REST keys)
      ├─ ElevenLabs            → voice + music
      ├─ panda_render          → clean compose, ffmpeg lane (folded montage-svc render, in-process)
      └─ video_compose         → remotion / hyperframes lanes (Node ≥22; runtime-routed)
      │
Local storage (data/jobs/{id})             ← artifacts + checkpoints + cost_report (S3 later, no API change)

Branding = a SEPARATE, on-demand step applied to the approved master (not a gate).
A second entrance, /montage/* (own X-Panda-Token), exposes the raw render core directly.
```

- **LLM:** Claude Code headless — **subscription login** (`~/.claude`) on the box, or OpenRouter via `ANTHROPIC_BASE_URL`. No direct Anthropic API key required.
- **One service, one port (8501):** the engine itself is not a server; the launcher fronts it.
- **montage-svc is folded in** (`vendor/montage_svc`) — no separate render service to run.

## The approval gates

The `panda-video` pipeline pauses for a human at up to six points (`pipeline_defs/panda-video.yaml`):

| # | Gate | Reviewer approves |
|---|------|-------------------|
| 1 | `approve_script` | the script |
| 2 | `approve_scene_plan` | the structured **text** scene plan (no media generated yet) |
| 3 | `approve_stills` | one still per scene — **no video yet**, so a reject here costs nothing |
| 3.5 | `approve_motion_sample` | **one** hero clip — approve the motion/animation **before** the full batch (cost gate; on by default, skippable) |
| 4 | `approve_assets` | the full media set (remaining stills animated into clips + VO + music; revise specific shots) |
| 5 | `approve_final` | the finished (unbranded) video |

Gates 3, 3.5 & 4 are pauses of the **same** `assets` stage (tell them apart by the `gate` field).
The `approve_motion_sample` gate appears only when the `motion_sample` job option is on (**default**);
set `motion_sample:false` for quick drafts. Approve advances; "revise" regenerates that stage (or
just the named shots). Branding is offered **after** gate 5, only if asked — `POST /jobs/{id}/brand`.

**`panda-carousel`** (stills-only sibling): `POST /jobs` with `"pipeline": "panda-carousel"`.
Gates: `approve_script` → `approve_scene_plan` → `approve_stills` → `done`. No motion, clips,
TTS, or compose. Optional `options.gates: ["scene_plan", "stills"]` skips the script gate.
`options.aspect_ratio` is caller-set (default `4:5`; also `1:1`, `9:16`, `WIDTHxHEIGHT`, …).
After `done`, `POST /jobs/{id}/brand` stamps the BGC wordmark onto copies of the stills.
See [`dify_launcher/CAROUSEL.md`](dify_launcher/CAROUSEL.md).

**`panda-image`** (single still): `POST /jobs` with `"pipeline": "panda-image"`.
Gates: `approve_scene_plan` → `approve_stills` → `done`. No script, motion, clips, TTS, or
compose. `options.aspect_ratio` default `1:1`. Dual-mode stills revise (`edit` | `fresh`).
After `done`, `POST /jobs/{id}/brand` stamps the BGC wordmark onto a copy.
See [`dify_launcher/IMAGE.md`](dify_launcher/IMAGE.md).

## Cost & time report

Every job produces a per-project consumption report in native units (no cross-platform USD
roll-up): **Higgsfield credits**, **ElevenLabs** characters/seconds, and **generation time**
per stage + total. Fetch it at `GET /jobs/{id}/cost` (JSON) or download the `cost_report.md`
artifact. See [`dify_launcher/DIFY_INTEGRATION.md`](dify_launcher/DIFY_INTEGRATION.md).

## Quick start (EC2)

```bash
git clone https://github.com/Philipcyrus/OpenMontage_official_repos.git ~/panda-engine
cd ~/panda-engine
bash deploy/install.sh          # system deps + venv + python deps + smoke test
. .venv/bin/activate
cp .env.example .env            # DIFY_RUNNER=mock, DIFY_DATA_DIR, keys; DIFY_TOKEN optional (empty = no auth)

# free port 8501 (retire old montage-svc) then run the launcher
sudo systemctl disable --now montage-svc
uvicorn dify_launcher.app:app --host 127.0.0.1 --port 8501
```

**Budget cap:** set the `max_higgsfield_credits` job option to enforce a hard credit ceiling — the
agent blocks *before* any generation that would exceed it and pauses at a `budget_exceeded` gate
(raise the cap / revise / cancel). Unset = no cap.

**Auth is optional:** leave `DIFY_TOKEN` empty for no token; set it to require `X-Dify-Token`.
**Node:** the default `ffmpeg`/`panda_render` render lane needs no Node; the `remotion` and
`hyperframes` lanes need **Node ≥ 22** (installed via `nvm` alongside system Node 18).
Both are covered in [`deploy/README.md`](deploy/README.md).

Full deploy (systemd + reverse proxy for `dev.om.mvnoc.ai` → 8501): see [`deploy/README.md`](deploy/README.md).
Wiring Dify to the launcher: see [`dify_launcher/DIFY_INTEGRATION.md`](dify_launcher/DIFY_INTEGRATION.md).

## Runners

Set `DIFY_RUNNER`:
- **`mock`** — no LLM/Higgsfield; fakes script + stills but **really renders** a clean video.
  Proves the whole Dify handshake + gates end to end.
- **`claude`** — the real agent: Claude Code headless + OpenRouter + the Higgsfield MCP.

## Testing

```bash
python dify_launcher/test_dify_flow.py        # full 5-gate handshake on the mock runner (real render)
python dify_launcher/test_claude_adapter.py   # the claude runner's checkpoint adapter
make test                                     # upstream engine contract tests
```
Or drive the live API with curl — see [`deploy/README.md`](deploy/README.md).

**panda-carousel** (usage, aspect ratios, dual-mode revise, recorded walks):
[`dify_launcher/CAROUSEL.md`](dify_launcher/CAROUSEL.md).
**panda-image** (single still, same revise + `/brand`):
[`dify_launcher/IMAGE.md`](dify_launcher/IMAGE.md).

## Repo layout

| path | what |
|------|------|
| `pipeline_defs/panda-video.yaml` | the pipeline + the 5 gates |
| `pipeline_defs/panda-carousel.yaml` | stills-only sibling (script → scene_plan → stills → done) |
| `pipeline_defs/panda-image.yaml` | single still (scene_plan → stills → done) |
| `skills/pipelines/panda-video/` | stage skills (scene-plan director, asset director, compose director) |
| `lib/cost_report.py` | per-project cost & time report (Higgsfield credits, ElevenLabs usage, gen time) |
| `tools/video/panda_render.py` | clean compose (folded render) |
| `tools/video/higgsfield_mcp_video.py` | Higgsfield MCP bridge |
| `config/panda-elements.json` | panda + customer character references |
| `styles/panda.yaml` | look of generated imagery |
| `vendor/montage_svc/`, `vendor/brand/` | folded render code + brand assets + bundled CJK font |
| `dify_launcher/` | the HTTP service Dify calls (+ tests, integration guide) |
| `deploy/` | EC2 install script, systemd unit, reverse-proxy config |
| `lib/`, `schemas/`, `tools/`, `skills/` | the OpenMontage engine (upstream) |

## Credits & license

Built on **[OpenMontage](https://github.com/calesthio/OpenMontage)** (agentic video pipeline
engine). Licensed under **AGPLv3** — see [`LICENSE`](LICENSE).
