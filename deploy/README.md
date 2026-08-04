# Deploying the Panda launcher on EC2

Get the launcher running on the box and reachable at `dev.om.mvnoc.ai`, so Dify can call it.
The launcher is the API front door; it drives the OpenMontage agent through the 4 gates.

## Steps (on the EC2 box)
```bash
# 1) clone the fork (permanent path — NOT /tmp)
sudo mkdir -p /opt/panda && sudo chown "$USER" /opt/panda
git clone https://github.com/Philipcyrus/OpenMontage-private.git /opt/panda/OpenMontage-prod
cd /opt/panda/OpenMontage-prod
git checkout panda-video-scaffold

# 2) install (system deps + venv + launcher deps + smoke test)
bash deploy/install.sh

# 3) configure env
nano .env          # set DIFY_TOKEN (long random), DIFY_RUNNER=mock, DIFY_DATA_DIR=/opt/panda/data

# 4) free port 8501 — retire the old montage-svc (replaced by this engine)
sudo systemctl disable --now montage-svc   # skip if it isn't a systemd service

# 5) run the launcher as a service (listens on 8501)
sudo cp deploy/panda-launcher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now panda-launcher
systemctl status panda-launcher --no-pager

# 6) reverse proxy — NO CHANGE NEEDED. dev.om.mvnoc.ai already forwards to 8501;
#    the launcher now answers there instead of montage-svc.

# 7) verify it's live
curl -s -H "X-Dify-Token: <token>" https://dev.om.mvnoc.ai/health
#   -> {"status":"ok","runner":"mock"}
```

## Operating the launcher (start / stop / restart)

The launcher loads its **code and `.env` at startup**, so you MUST restart it after ANY of:
code change, `git pull`, or editing `.env` (e.g. `DIFY_RUNNER=mock` → `claude`, or the token).

Actual clone path on the current box: `~/OpenMontage-Repos/OpenMontage_official_repos`.

**nohup (dev / manual):**
```bash
cd ~/OpenMontage-Repos/OpenMontage_official_repos

# stop
pkill -f "uvicorn dify_launcher"

# start (loads .env, since plain uvicorn does NOT auto-read it)
# --host 0.0.0.0 so the reverse proxy (separate namespace) can reach it.
# SECURITY: the EC2 security group MUST restrict inbound 8501 to the proxy only.
set -a; source .env; set +a
nohup .venv/bin/uvicorn dify_launcher.app:app --host 0.0.0.0 --port 8501 > ~/panda-launcher.log 2>&1 &

# verify + watch log
curl -s -H "X-Dify-Token: $DIFY_TOKEN" http://127.0.0.1:8501/health
tail -f ~/panda-launcher.log
```
Restart = stop then start. Switch runner: edit `DIFY_RUNNER` in `.env`, then restart.

**systemd (production):**
```bash
sudo systemctl restart panda-launcher     # after code/.env change
sudo systemctl status  panda-launcher --no-pager
journalctl -u panda-launcher -f           # live logs
```

## Then connect Dify
Point Dify at the base URL and follow `dify_launcher/DIFY_INTEGRATION.md`:
- `BASE_URL = https://dev.om.mvnoc.ai`   (root — proxy already forwards to 8501)

## Two things to know
1. **Runner:** `DIFY_RUNNER=mock` proves the whole Dify handshake (fakes script/gen, but
   REALLY renders a clean video). Switch to `claude` only after the real `ClaudeCodeRunner`
   + Claude Code + OpenRouter + the Higgsfield MCP are wired on the box.
2. **Storage:** local under `DIFY_DATA_DIR` (default `./data`). Artifacts + job state live
   there; `data/jobs/` is gitignored. Swap for S3 later (Phase 5) with no API change.

## Files here
| file | purpose |
|---|---|
| `install.sh` | system deps + venv + launcher deps + import/render smoke test |
| `panda-launcher.service` | systemd unit (uvicorn on 127.0.0.1:8501) |
| `nginx-panda.conf` | reverse-proxy block (subpath or subdomain) |
| `requirements-launcher.txt` | minimal deps for launcher + render (mock) |
