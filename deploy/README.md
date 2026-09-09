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

## Daily Claude + Higgsfield health check (cron)

`deploy/panda_healthcheck.py` is a zero-generation morning canary for the full production path:

1. launcher `GET /health`,
2. `claude auth status --json`, and
3. a bounded Claude Haiku session that discovers and calls the real Claude.ai Higgsfield
   `balance` MCP tool exactly once.

The checker verifies the structured MCP tool-use/result pair rather than trusting the model's
text. It cannot call generation tools, disables Claude auto-memory and session persistence, and
uses bounded turns, budget, retries, and wall time. `balance` consumes no Higgsfield credits; the
Claude canary does consume a small amount of Claude usage (about $0.032 in the 2026-09-08 box
verification; treat that as an observation, not a guaranteed price).

Run it as **`ec2-user`**, not root: Claude OAuth and the Claude.ai Higgsfield connector belong to
that Unix account. Configure alerts outside Git:

```bash
mkdir -p ~/.config ~/.local/state/panda-healthcheck
cp deploy/panda-healthcheck.env.example ~/.config/panda-healthcheck.env
chmod 600 ~/.config/panda-healthcheck.env

# Edit the private file and set PANDA_HEALTH_SNS_TOPIC_ARN or PANDA_HEALTH_WEBHOOK_URL.
# Safe manual verification: runs the real checks but sends no notification.
/usr/bin/python3 deploy/panda_healthcheck.py --no-alert
```

Install the cron entry with `crontab -e` **without `sudo`**. This example runs daily at **07:00
Pacific**, prevents overlapping checks, and keeps a local audit log. The same entry is available as
`deploy/panda-healthcheck.cron.example`:

```cron
SHELL=/bin/bash
HOME=/home/ec2-user
PATH=/home/ec2-user/.nvm/versions/node/v22.23.2/bin:/home/ec2-user/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
CRON_TZ=America/Los_Angeles

0 7 * * * /usr/bin/flock -n /tmp/panda-healthcheck.lock /usr/bin/python3 /home/ec2-user/OpenMontage-Repos/OpenMontage_official_repos/deploy/panda_healthcheck.py >> /home/ec2-user/panda-healthcheck.log 2>&1
```

`CRON_TZ` is what keeps 07:00 at 07:00 across daylight saving; the box's own clock is UTC, so
without it the entry drifts an hour twice a year. cronie (Amazon Linux) supports it — verify with
`crontab -l` and one dated line in the log after the first run.

The script alerts on the first failure, reminds after `PANDA_HEALTH_REMINDER_HOURS`, and sends one
recovery notification. A healthy daily run is logged but not alerted unless
`PANDA_HEALTH_NOTIFY_SUCCESS=true`.

### Reporting the result into Dify

Every run writes its full verdict to `PANDA_HEALTH_STATE_FILE`. Two processes can serve that
file; **point Dify at the second one.**

| Port | Served by | Use |
|---|---|---|
| 8501 | the main launcher (`dify_launcher/app.py`) | convenience + fallback |
| **8502** | **`deploy/panda_health_service.py`** | **what Dify should poll** |

Both expose `GET /health/canary` with the same field names, so switching ports needs no
workflow change. The difference is what happens when the launcher dies.

**Why a second process rather than a route on the launcher.** A monitor that shares a failure
domain with the thing it monitors cannot report that thing's failure. Served from 8501, a dead
launcher gives Dify a bare connection error — indistinguishable from a network problem or a
misfiring HTTP node. The health service has no such blind spot: it imports nothing from the
engine, holds no job state, and answers `LAUNCHER_DOWN` with detail while the launcher is down.
Both read the same file through `dify_launcher/canary.py`, so the two ports cannot disagree.

It answers two questions at once:

- `launcher_live` — a fresh probe of 8501 done *now*, costing nothing
- the stored fields (`status`, `codes`, `results`, …) — cron's daily deep check, which spends a
  real Claude turn proving auth and Higgsfield still work

The deep check stays in cron. Worst case it runs 260s (90s Higgsfield + 60s retry delay + 90s
retry, plus auth and launcher checks), and `DIFY_INTEGRATION.md` §4 tells Dify to cap HTTP nodes
at 30–60s. A synchronous "run the canary now" endpoint would time out first under exactly the
conditions the canary exists to detect.

**Install it:**

```bash
sudo cp deploy/panda-health.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now panda-health
systemctl status panda-health --no-pager

curl -s http://127.0.0.1:8502/health          # this service's own liveness
curl -s http://127.0.0.1:8502/health/canary   # the morning report
```

`Restart=always` means it self-heals. It deliberately has **no** `After=panda-launcher.service`
and no Node on its `PATH` — tying its lifecycle or its dependencies to the launcher's would
rebuild the shared failure domain it exists to break.

Then add a location block on the `dev.om.mvnoc.ai` proxy sending `/health/canary` to `127.0.0.1:8502`
instead of 8501, and open 8502 to the proxy only in the security group (never `0.0.0.0/0`).

**Branch the Dify report on `status`, never on `healthy`.** A response can be
`{"healthy": true, "stale": true}` — that is *yesterday's* verdict plus the news that the checker
stopped running. `status` folds in both freshness and live launcher state: `ok` only when the
launcher is up now **and** the stored check passed **and** it is fresh; otherwise `failed`,
`stale` (older than `PANDA_HEALTH_MAX_AGE_S`, default 26h), or `never_run`. `canary_status`
carries the stored verdict on its own if you want to show them separately.

Both services read `PANDA_HEALTH_STATE_FILE` from their environment, so if you move the state
file, set it in `~/.config/panda-healthcheck.env` **and** the launcher `.env` that both units
load, then restart both.

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

## Troubleshooting

**Dify gets 502 / artifact links fail, but everything looks healthy on the box.**
Check the bind address first:

```bash
sudo ss -lntp | grep 8501     # MUST be 0.0.0.0:8501, NOT 127.0.0.1:8501
```

The proxy runs in a different namespace, so a loopback bind is unreachable from it even though
the launcher is running perfectly. Every check *from the box* still passes — `/health` is `ok`,
`POST /jobs` returns 200, artifacts fetch 200 over `127.0.0.1` — because those never leave the
host. Only an external request shows it:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://dev.om.mvnoc.ai/jobs/<job_id>/artifacts/<name>
```

Fix by restarting with the start block above (`--host 0.0.0.0`). **Do not copy the flags out of
`ps` output** — that is how a wrong bind survives a restart. Copy them from this file.

Same trap, other flags: a restart that skips `source .venv/bin/activate` or the Node 22 lines
also leaves `/health` green while the Remotion/HyperFrames lanes and the `claude` binary resolve
wrongly. Use the whole start block, not part of it.

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
| `panda_healthcheck.py` | cron-safe Claude/Higgsfield/launcher health canary + alerts |
| `panda-healthcheck.env.example` | private health-check configuration template |
| `panda-healthcheck.cron.example` | example entry for `ec2-user`'s crontab |
| `panda_health_service.py` | standalone health reporter on 8502 — answers when the launcher can't |
| `panda-health.service` | systemd unit for that reporter (`Restart=always`) |
| `nginx-panda.conf` | reverse-proxy block (subpath or subdomain) |
| `requirements-launcher.txt` | minimal deps for launcher + render |
