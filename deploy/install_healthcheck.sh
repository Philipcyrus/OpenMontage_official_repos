#!/usr/bin/env bash
# Install or update the daily Panda health check for THIS Unix user on THIS box.
#
#   deploy/install_healthcheck.sh            # daily at 02:30 UTC (08:00 IST)
#   deploy/install_healthcheck.sh "40 2"     # "MINUTE HOUR" in UTC, e.g. to stagger box 2
#
# Run it as the user that owns the Claude login (ec2-user on box 1, admin on box 2), never
# with sudo. It is idempotent: it creates ~/.config/panda-healthcheck.env once (never
# overwrites it), backs up the crontab, and replaces only this check's own crontab line.
# Other cron jobs and crontab-wide settings are left alone. It does not run the check; the
# next steps it prints do. Nothing here needs a launcher restart.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEDULE="${1:-30 2}"
ENV_FILE="$HOME/.config/panda-healthcheck.env"
STATE_DIR="$HOME/.local/state/panda-healthcheck"
LOG_FILE="$HOME/panda-healthcheck.log"

if [ "$(id -u)" -eq 0 ]; then
  echo "run this as the user that owns the Claude login, not root" >&2
  exit 2
fi
if ! [[ "$SCHEDULE" =~ ^[0-9]{1,2}\ [0-9]{1,2}$ ]]; then
  echo "schedule must be \"MINUTE HOUR\" in UTC, e.g. \"30 2\"" >&2
  exit 2
fi
case "$REPO$HOME" in
  *" "*) echo "paths containing spaces are not supported in the cron line" >&2; exit 2 ;;
esac
for tool in python3 flock crontab; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing $tool on PATH" >&2; exit 2; }
done
PYTHON="$(command -v python3)"
FLOCK="$(command -v flock)"

mkdir -p "$HOME/.config" "$STATE_DIR"
if [ -e "$ENV_FILE" ]; then
  echo "kept existing $ENV_FILE"
else
  (umask 077 && cp "$REPO/deploy/panda-healthcheck.env.example" "$ENV_FILE")
  echo "created $ENV_FILE (mode 600)"
fi

# cron starts with a bare PATH. Give the check the start block's order: Node 22 from nvm, the
# user-local claude, then the static ffmpeg. The check still prefers the running launcher's
# own PATH whenever it can read it, so this is only the fallback.
NODE22="$(ls -d "$HOME"/.nvm/versions/node/v22*/bin 2>/dev/null | sort -V | tail -n 1 || true)"
CRON_PATH="${NODE22:+$NODE22:}$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
LINE="$SCHEDULE * * * PATH=$CRON_PATH $FLOCK -n /tmp/panda-healthcheck-$(id -un).lock $PYTHON $REPO/deploy/panda_healthcheck.py >> $LOG_FILE 2>&1"

if CURRENT="$(crontab -l 2>&1)"; then
  :
elif printf '%s' "$CURRENT" | grep -qi "no crontab"; then
  CURRENT=""
else
  echo "could not read the current crontab, so it was not changed: $CURRENT" >&2
  exit 1
fi
BACKUP="$STATE_DIR/crontab.before.$(date -u +%Y%m%dT%H%M%SZ)"
printf '%s\n' "$CURRENT" > "$BACKUP"
KEPT="$(printf '%s\n' "$CURRENT" | grep -v -F 'deploy/panda_healthcheck.py' || true)"
if [ -n "$KEPT" ]; then
  printf '%s\n%s\n' "$KEPT" "$LINE" | crontab -
else
  printf '%s\n' "$LINE" | crontab -
fi

echo "crontab backed up to $BACKUP"
echo "installed:"
echo "  $LINE"
if printf '%s\n' "$CURRENT" | grep -q '^PATH=.*\.nvm'; then
  echo "note: your crontab still has a crontab-wide PATH= line from the old example;" \
       "it was left as is because other jobs may rely on it."
fi
cat <<EOF

Next steps:
  1. Edit $ENV_FILE:
     set PANDA_HEALTH_WEBHOOK_URL (or PANDA_HEALTH_SNS_TOPIC_ARN), PANDA_HEALTH_PUBLIC_URL
     and PANDA_HEALTH_BOX_NAME.
  2. Send one test alert:
       $PYTHON $REPO/deploy/panda_healthcheck.py --test-alert
  3. Run the full check once now (one small Claude Haiku turn; no media is generated):
       $PYTHON $REPO/deploy/panda_healthcheck.py --no-alert
EOF
