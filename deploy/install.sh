#!/usr/bin/env bash
# Prepare the Panda launcher on the EC2 box. Run AFTER cloning the repo, e.g.:
#   git clone https://github.com/Philipcyrus/OpenMontage-private.git /opt/panda/OpenMontage-prod
#   cd /opt/panda/OpenMontage-prod && bash deploy/install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR"
echo "== installing in $APP_DIR =="

# 1) system deps (ffmpeg + python). CJK font is already BUNDLED in the repo
#    (vendor/brand/fonts/msyhbd.ttc), so no font install is strictly required.
if command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip ffmpeg google-noto-sans-cjk-ttc-fonts || true
elif command -v apt >/dev/null 2>&1; then
  sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip ffmpeg fonts-noto-cjk || true
fi

# 2) python venv + launcher deps
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r deploy/requirements-launcher.txt

# 3) env file
if [ ! -f .env ]; then
  cp .env.example .env
  echo ">> created .env from .env.example — EDIT IT (set DIFY_TOKEN, DIFY_RUNNER, keys)."
fi

# 4) smoke test the render path + launcher import
. .venv/bin/activate
python - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path('vendor').resolve()))
from montage_svc.config import has_ffmpeg
from dify_launcher.app import app  # noqa
print("ffmpeg on PATH:", has_ffmpeg())
print("launcher imports: OK")
PY

echo
echo "== done =="
echo "Next:"
echo "  1) edit .env  (DIFY_TOKEN, DIFY_RUNNER=mock|claude, DIFY_DATA_DIR)"
echo "  2) sudo cp deploy/panda-launcher.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now panda-launcher"
echo "  3) add deploy/nginx-panda.conf to your reverse proxy for dev.om.mvnoc.ai"
echo "  4) test:  curl -s -H \"X-Dify-Token: \$DIFY_TOKEN\" https://dev.om.mvnoc.ai/panda/health"
