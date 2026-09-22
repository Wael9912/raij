#!/usr/bin/env bash
# Push code from this Mac to the server and restart the services there.
#   deploy/push.sh                 code only (the server's data/ and keys are never overwritten)
#   deploy/push.sh --with-data     one-time migration: also copies data/, .env, client_secret.json and
#                                  generated media. Stop the Mac services first (uninstall-services)!
# Server + key come from .env: RAIJ_SERVER=ubuntu@1.2.3.4  RAIJ_SSH_KEY=~/.ssh/raij.key
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER=$(grep '^RAIJ_SERVER=' .env | cut -d= -f2-)
KEY=$(grep '^RAIJ_SSH_KEY=' .env | cut -d= -f2- | sed "s|^~|$HOME|")
[ -n "$SERVER" ] || { echo "Set RAIJ_SERVER in .env"; exit 1; }
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
DEST="$SERVER:social-media-automation/"

rsync -az --delete -e "$SSH" \
  --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .git \
  --exclude data/ --exclude .env --exclude 'client_secret*.json' \
  --exclude assets/stock/ --exclude assets/generated/ --exclude assets/music/ \
  ./ "$DEST"

if [ "${1:-}" = "--with-data" ]; then
  rsync -az -e "$SSH" --exclude logs/ --exclude locks/ data/ "$DEST/data/"
  rsync -az -e "$SSH" .env client_secret.json "$DEST"
  rsync -az -e "$SSH" assets/generated/ "$DEST/assets/generated/"
  [ -d assets/music ] && rsync -az -e "$SSH" assets/music/ "$DEST/assets/music/"
  # This Mac's paths/ssh settings don't belong in the server's .env.
  $SSH "$SERVER" "cd social-media-automation && sed -i '/^RAIJ_SERVER=/d;/^RAIJ_SSH_KEY=/d' .env"
fi

$SSH "$SERVER" 'cd social-media-automation && export PATH=$HOME/.local/bin:$PATH && uv sync -q && \
  if systemctl list-unit-files raij-bot.service >/dev/null 2>&1 && [ -f /etc/systemd/system/raij-bot.service ]; then \
    uv run python -m src.main install-services; fi'
echo "Pushed to $SERVER"
