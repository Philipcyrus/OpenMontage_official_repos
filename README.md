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
script → storyboard stills → generated clips → assembled video. Generation runs through the
**Higgsfield MCP**; the final render (assembly, CJK captions, audio) is produced in-process by
montage-svc's craft code, **folded into this repo**. Panda **branding** (logo, watermark, cards)
is applied **only on request, after the video is approved** — never baked in.

Everything runs behind **one HTTP service** (the Dify launcher) on **port 8501**.

## Architecture

```
Dify (dev.om.mvnoc.ai)                     ← brief in, reviews + approvals
      │  HTTP  (:8501)
Dify Launcher  (dify_launcher/)            ← the ONLY service; starts/resumes runs, serves gates
      │  starts per job
Claude Code + OpenMontage pipeline         ← the agent: script → stills → clips → assemble
      ├─ Higgsfield MCP        → video/image generation (no REST keys)
      ├─ ElevenLabs            → voice
      └─ panda_render          → clean compose (folded montage-svc render, in-process)
      │
Local storage (data/jobs/{id})             ← artifacts + checkpoints (S3 later, no API change)

Branding = a SEPARATE, on-demand step applied to the approved master (not a gate).
```

- **LLM:** Claude via **OpenRouter** (`ANTHROPIC_BASE_URL`) — no direct Anthropic key needed.
- **One service, one port (8501):** the engine itself is not a server; the launcher fronts it.
- **montage-svc is folded in** (`vendor/montage_svc`) — no separate render service to run.

## The approval gates

The `panda-video` pipeline pauses for a human at four points (`pipeline_defs/panda-video.yaml`):

| # | Gate | Reviewer approves |
|---|------|-------------------|
| 1 | `approve_script` | the script |
| 2 | `approve_storyboard` | one still per scene (may be user-supplied) |
| 3 | `approve_clips` | the generated motion clips (revise specific shots) |
| 4 | `approve_final` | the finished (unbranded) video |

Approve advances; "revise" regenerates that stage (or just the named shots). Branding is offered
**after** gate 4, only if asked.

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
python dify_launcher/test_dify_flow.py        # full 4-gate handshake on the mock runner (real render)
python dify_launcher/test_claude_adapter.py   # the claude runner's checkpoint adapter
make test                                     # upstream engine contract tests
```
Or drive the live API with curl — see [`deploy/README.md`](deploy/README.md).

## Repo layout

| path | what |
|------|------|
| `pipeline_defs/panda-video.yaml` | the pipeline + the 4 gates |
| `skills/pipelines/panda-video/` | stage skills (storyboard director, …) |
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
