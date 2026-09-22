#!/usr/bin/env bash
# One-time server setup (Ubuntu 24.04, ARM or x86). Run on the server from the repo root:
#   bash deploy/setup-server.sh
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update -q
# ffmpeg: render; libfribidi0/libraqm0: Arabic shaping for Pillow; sqlite3/rsync: ops.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq ffmpeg libfribidi0 libraqm0 sqlite3 rsync curl
sudo timedatectl set-timezone Africa/Cairo

if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv sync
uv run python -c "from src import textshape; assert textshape.ensure(), 'raqm missing'; print('Arabic shaping: ok')"
uv run pytest -q
mkdir -p data/logs
echo "Setup done. Start the services with: uv run python -m src.main install-services"
