#!/usr/bin/env bash
# Start Vocalyze. Creates a virtualenv and an encryption key on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  KEY="$(python -m app.core.crypto)"
  # macOS sed needs an argument to -i; GNU sed does not.
  sed -i.bak "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=${KEY}|" .env && rm -f .env.bak
  echo "Wrote .env with a fresh encryption key."
fi

set -a; source .env; set +a
exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --reload
