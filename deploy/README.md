# Deploying the Panda launcher on EC2

Get the launcher running on the box and reachable at `dev.om.mvnoc.ai`, so Dify can call it.
The launcher is the API front door; it drives the OpenMontage agent through the approval gates.

Actual clone path on the current box: `~/OpenMontage-Repos/OpenMontage_official_repos`
(`/home/ec2-user/OpenMontage-Repos/OpenMontage_official_repos`).
Repo: `https://github.com/Philipcyrus/OpenMontage_official_repos.git`.

## First-time install (on the EC2 box)
```bash
# 1) clone the fork (permanent path — NOT /tmp)
git clone https://github.com/Philipcyrus/OpenMontage_official_repos.git \
  ~/OpenMontage-Repos/OpenMontage_official_repos
cd ~/OpenMontage-Repos/OpenMontage_official_repos

# 2) install (system deps + venv + launcher deps + smoke test)
bash deploy/install.sh

# 3) configure env
nano .env          # DIFY_RUNNER=mock|claude, DIFY_DATA_DIR=..., ELEVENLABS_API_KEY=...
                   # DIFY_TOKEN is OPTIONAL — leave EMPTY for no auth (see "Auth" below)

# 4) free port 8501 — retire the old montage-svc (replaced by this engine)
sudo systemctl disable --now montage-svc   # skip if it isn't a systemd service

# 5) (optional) Node 22 for the Remotion / HyperFrames render lanes — see "Node runtime"
# 6) run the launcher — nohup (dev) or systemd (prod), both below
```

## Node runtime (which render lanes need Node)

The compose stage is **runtime-routed** on `edit_decisions.render_runtime`. Node is only needed
for two of the three lanes:

| `render_runtime` | Tool | Node needed? |
|---|---|---|
| `ffmpeg` (default) | `panda_render` (folded montage-svc, in-process) | **No** — pure Python + ffmpeg |
| `remotion` | `video_compose` (React motion graphics) | **Yes — Node ≥ 22** + `remotion-composer` |
| `hyperframes` | `video_compose` (HTML/CSS/GSAP) | **Yes — Node ≥ 22** + Chrome headless |

The box ships **system Node 18** (`/usr/bin/node`), which is too old for Remotion/HyperFrames.
Node 22 is installed **alongside** it via `nvm` (does not replace system Node):

```bash
# one-time: install nvm + Node 22, keep system Node 18 as the machine default
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
export NVM_DIR="$HOME/.nvm"; source "$NVM_DIR/nvm.sh"
nvm install 22            # -> v22.23.2
nvm alias default system  # keep the box default on Node 18; opt in to 22 per-shell

# one-time: enable the two Node lanes
npx hyperframes doctor    # installs Chrome headless; needs its shared libs:
#   sudo dnf install -y nss atk at-spi2-atk cups-libs libdrm libxkbcommon \
#     at-spi2-core libXcomposite libXdamage libXfixes libXrandr mesa-libgbm \
#     pango alsa-lib
cd remotion-composer && npm install && cd ..   # -> REMOTION_READY
```

**Ordering gotcha (important):** the launcher's render lanes shell out to `npx`, so the launcher
process must have **Node 22 first on its PATH**. Activate Node 22 **AFTER** sourcing `.venv` and
`.env` — if you `nvm use 22` first and then source `.env`, a `PATH` line in `.env` (or the venv
activation) can push system Node 18 back in front and the Node lanes will silently break. Always
verify with `node -v` **before** starting. The `ffmpeg` lane (the Panda default) works regardless
of Node version, so the launcher is still fully functional on Node 18 — only Remotion/HyperFrames
require 22.

## Operating the launcher (start / stop / restart)

The launcher loads its **code and `.env` at startup**, so you MUST restart it after ANY of:
code change, `git pull`, or editing `.env` (e.g. `DIFY_RUNNER=mock` → `claude`, or the token).

**nohup (dev / manual) — full sequence with Node 22:**
```bash
cd ~/OpenMontage-Repos/OpenMontage_official_repos

# pull latest (if updating)
git pull

# stop
pkill -f "uvicorn dify_launcher.app:app"; sleep 1

# set up the environment in the RIGHT order: venv + .env FIRST, Node 22 LAST
source .venv/bin/activate
set -a; [ -f .env ] && . ./.env; set +a
export NVM_DIR="$HOME/.nvm"; source "$NVM_DIR/nvm.sh"; nvm use 22
export PATH="$HOME/.nvm/versions/node/v22.23.2/bin:$HOME/.npm-global/bin:$PATH"

# VERIFY before starting — these MUST be correct:
node -v            # expect v22.23.2  (NOT v18.x)
which npx          # expect ~/.nvm/versions/node/v22.23.2/bin/npx
which claude       # expect ~/.npm-global/bin/claude  (needed for DIFY_RUNNER=claude)

# start (--host 0.0.0.0 so the reverse proxy in another namespace can reach it)
# SECURITY: the EC2 security group MUST restrict inbound 8501 to the proxy only.
nohup python -m uvicorn dify_launcher.app:app --host 0.0.0.0 --port 8501 \
  > ~/launcher.log 2>&1 &

# verify + watch log
sleep 2 && curl -s http://127.0.0.1:8501/health
tail -f ~/launcher.log
```
Restart = stop then start. Switch runner: edit `DIFY_RUNNER` in `.env`, then restart.

> If you don't need the Remotion/HyperFrames lanes, you can skip the three Node lines — the
> launcher runs fine on system Node 18 and the default `ffmpeg`/`panda_render` lane is unaffected.

**systemd (production):**
```bash
sudo systemctl restart panda-launcher     # after code/.env change
sudo systemctl status  panda-launcher --no-pager
journalctl -u panda-launcher -f           # live logs
```
The bundled `panda-launcher.service` puts Node 22 (`~/.nvm/.../v22.23.2/bin`) and `claude`
(`~/.npm-global/bin`) on the unit's `PATH` so all three render lanes work under systemd too.
Adjust `User`, `WorkingDirectory`, `EnvironmentFile`, and the nvm version in that file to match
the box before installing it.

## Auth (Dify token — optional, env-toggleable)

Auth is a single **optional** shared secret, `DIFY_TOKEN`, read from `.env` at startup:

- **`DIFY_TOKEN` empty or unset → no token required.** Dify calls the endpoints with no header.
- **`DIFY_TOKEN=<secret>` → every request must send `X-Dify-Token: <secret>`** or gets `401`.

Toggling is just an `.env` edit + restart — no code change. Verify the current mode:
```bash
# no-token mode (DIFY_TOKEN empty):
curl -s http://127.0.0.1:8501/health
# token mode (DIFY_TOKEN set):
curl -s -H "X-Dify-Token: $DIFY_TOKEN" http://127.0.0.1:8501/health
#   -> {"status":"ok","runner":"...","async":true,"montage_door":true}
```
> The `/montage/*` raw-render door has its **own** separate token, `PANDA_TOKEN` (header
> `X-Panda-Token`) — independent of `DIFY_TOKEN`.

## Then connect Dify
Point Dify at the base URL and follow `dify_launcher/DIFY_INTEGRATION.md`:
- `BASE_URL = https://dev.om.mvnoc.ai`   (root — proxy already forwards to 8501)

## Two things to know
1. **Runner:** `DIFY_RUNNER=mock` proves the whole Dify handshake (fakes script/gen, but
   REALLY renders a clean video). Switch to `claude` for the real agent (Claude Code headless
   subscription login in `~/.claude` + the Higgsfield MCP + ElevenLabs).
2. **Storage:** local under `DIFY_DATA_DIR` (default `./data`). Artifacts + job state live
   there; `data/jobs/` is gitignored. Swap for S3 later with no API change.

## Cost & time report (per project)
Every job writes a consumption report — **Higgsfield credits**, **ElevenLabs** characters/seconds,
and **generation time** per stage + total (native units, no USD roll-up). Read it on the box with:
```bash
curl -s http://127.0.0.1:8501/jobs/<job_id>/cost | python -m json.tool   # JSON summary
cat data/jobs/<job_id>/artifacts/cost_report.md                          # human-readable table
```
The report files live at `data/jobs/<job_id>/artifacts/cost_report.{md,json}` (mirrored from the
engine project's `projects/<job_id>/artifacts/`). See `dify_launcher/DIFY_INTEGRATION.md` for the
endpoint contract.

## Files here
| file | purpose |
|---|---|
| `install.sh` | system deps + venv + launcher deps + import/render smoke test |
| `panda-launcher.service` | systemd unit (uvicorn on 8501, Node 22 + claude on PATH) |
| `nginx-panda.conf` | reverse-proxy block (subpath or subdomain) |
| `requirements-launcher.txt` | minimal deps for launcher + render |
