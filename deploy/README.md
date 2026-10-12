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

**User screenshots also need Node.** A job with `options.media` renders its preview boards and
places the screenshots with `screen_overlay` (Remotion) — onto the clips for video, onto the stills
for carousel and image jobs — on top of the ffmpeg lane.
`POST /jobs` refuses media with a 400 unless Node ≥ 22 is first on the launcher's PATH and
`remotion-composer/node_modules` is installed, and unless `DIFY_FILES_HOSTS` names the Dify file
host (see `.env.example` §1c). Remotion downloads its headless Chrome on the first render — do one
test render after `npm install`. Jobs without screenshots are unaffected by any of this.

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
After restarting, do not rely on `status:"ok"` alone. Compare `/health`:

- `process_started_at` must be newer than the deployment.
- `build_revision` must match `git rev-parse HEAD` (or the explicitly deployed
  `OPENMONTAGE_BUILD_REVISION`).
- `launcher_code_fingerprint` must change when `dify_launcher/app.py` or
  `dify_launcher/runner.py` changed.

This catches the failure mode where the working tree was updated but uvicorn still serves
the previously imported launcher code.

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

# if the pull changed remotion-composer/package-lock.json (e.g. a dependency security
# bump), reinstall exactly from the lockfile — `git pull` alone leaves the old packages in
# node_modules. `npm ci` never rewrites the lockfile, so the checkout stays clean for the
# next pull; it does wipe node_modules/.remotion, so re-fetch the headless browser too.
git diff --quiet ORIG_HEAD HEAD -- remotion-composer/package-lock.json || \
  (cd remotion-composer && npm ci && npx remotion browser ensure)

# start (--host 0.0.0.0 so the reverse proxy in another namespace can reach it)
# SECURITY: the EC2 security group MUST restrict inbound 8501 to the proxy only.
nohup python -m uvicorn dify_launcher.app:app --host 0.0.0.0 --port 8501 \
  > ~/launcher.log 2>&1 &

# verify + watch log
sleep 2 && curl -s http://127.0.0.1:8501/health
git rev-parse HEAD
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

## Daily health check (every layer, cron)

`deploy/panda_healthcheck.py` runs once a day and tests every layer a Dify job crosses. It
generates nothing:

| Layer | What it proves | What it catches |
|---|---|---|
| `launcher` | `/health` is ok, the runner is `claude`, code is not stale, the loaded revision matches the checkout | a restart that skipped `.env` (mock runner); a pull without a restart |
| `launcher_reach` | `/health` answers on the box's private IP, not only on 127.0.0.1 | a `--host 127.0.0.1` restart, which gives Dify 502 while on-box checks pass |
| `public_url` | Dify's BASE_URL reaches this launcher (DNS, TLS, proxy), and the certificate is not about to expire | broken DNS, proxy or security group; an expiring certificate |
| `launcher_env` | the running launcher finds `claude`, has no `ANTHROPIC_*` override, and its `.env` restarts into `claude` | API-key mode, which silently drops the Higgsfield connector |
| `claude_auth` | the launcher's own `claude` binary is logged in | a logged-out or expired Claude login |
| `higgsfield_mcp` | one real Claude → Higgsfield `balance` call works, and credits are above the threshold | a disconnected connector, the subscription usage limit, low credits |
| `elevenlabs` | the API key authenticates, and enough characters are left | an expired key or a quota that is nearly used up |
| `render` | ffmpeg encodes and reads back a 1 s libx264/aac clip; Remotion is ready when screenshot jobs are on | a broken ffmpeg; Node 18 or a missing `remotion-composer` |
| `disk` | free space for jobs, projects, `~/.claude` and the repo | a filling disk |
| `dify` | Dify's file server (screenshot downloads) and any extra URLs answer | Dify unreachable from the box |

Higgsfield `balance` and the ElevenLabs quota read are free. The Claude turn uses Haiku, at most
4 turns, `--max-budget-usd 0.10` and no session persistence. It cost about $0.032 of
subscription usage in the 2026-09-08 box verification (an observation, not a price). The check
verifies the structured tool call and result, never the model's text. It reads secrets only
from the launcher's `.env`, uses them in memory and never prints them.

**Install once per box**, as the user that owns the Claude login (box 1 `ec2-user`, box 2
`admin`), without `sudo`. Nothing here needs a launcher restart:

```bash
cd ~/OpenMontage-Repos/OpenMontage_official_repos    # box 2: cd ~/OpenMontage_official_repos
git pull --ff-only
deploy/install_healthcheck.sh          # daily 02:30 UTC (08:00 IST); box 2: deploy/install_healthcheck.sh "40 2"
```

The installer creates `~/.config/panda-healthcheck.env` (mode 600; it never overwrites an
existing one), backs up the crontab, and replaces only this check's own crontab line. Then edit
that file and set:

- an alert destination: `PANDA_HEALTH_WEBHOOK_URL` (Slack, Discord, or Google Chat with
  `PANDA_HEALTH_WEBHOOK_KIND=generic`) or `PANDA_HEALTH_SNS_TOPIC_ARN` (needs boto3 and
  `sns:Publish` on the instance role);
- `PANDA_HEALTH_PUBLIC_URL`, Dify's BASE_URL for this box (box 1: `https://dev.om.mvnoc.ai`);
- `PANDA_HEALTH_BOX_NAME`, a label for alerts such as `box1-prod`.

Then verify:

```bash
python3 deploy/panda_healthcheck.py --test-alert   # one test message to the alert channel
python3 deploy/panda_healthcheck.py --no-alert     # the full check now; prints the JSON summary
python3 deploy/panda_healthcheck.py --no-alert --skip higgsfield_mcp   # the same, without the Claude turn
```

**Alerts.** The first failure alerts at once. So does any change in what is failing. An unchanged
failure reminds every `PANDA_HEALTH_REMINDER_HOURS`, and the first healthy run sends a recovery.
With `PANDA_HEALTH_NOTIFY_SUCCESS=true` (the template default) a short OK message arrives every
day, so silence means the check itself stopped. Each alert names the failing layer, a code and
the fix. The results also go to `~/panda-healthcheck.log`, and the exit code is non-zero when
unhealthy.

**Notes.**
- Higgsfield is a Claude.ai account connector, not a local MCP server. On
  `HIGGSFIELD_AUTH_FAILED` or `HIGGSFIELD_NOT_DISCOVERED`, reconnect it in the Claude.ai
  connector settings. On `CLAUDE_LOGGED_OUT`, run `claude auth login` as the same user.
- `public_url` calls the box's own public URL from the box. If the security group admits only
  Dify on 443, allow the box's own public IP as well, or leave `PANDA_HEALTH_PUBLIC_URL` empty.
  `launcher_reach` still catches a loopback-only bind.
- An older install's crontab-wide `SHELL=`/`HOME=`/`PATH=` lines are left in place; the new line
  sets its own `PATH`.

## Files here
| file | purpose |
|---|---|
| `install.sh` | system deps + venv + launcher deps + import/render smoke test |
| `panda-launcher.service` | systemd unit (uvicorn on 8501, Node 22 + claude on PATH) |
| `panda_healthcheck.py` | daily all-layers health check (launcher, Claude, Higgsfield, ElevenLabs, render, disk, Dify) + alerts |
| `install_healthcheck.sh` | per-box, per-user installer for the daily check (env file + crontab line) |
| `panda-healthcheck.env.example` | private health-check configuration template |
| `panda-healthcheck.cron.example` | example crontab line (box 1, `ec2-user`) |
| `nginx-panda.conf` | reverse-proxy block (subpath or subdomain) |
| `requirements-launcher.txt` | minimal deps for launcher + render |
