#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${DATA_DIR:-/data/vocalyze}"

if [ -z "${ENCRYPTION_KEY:-}" ]; then
  export ENCRYPTION_KEY="$(python -m app.core.crypto)"
  echo "[vocalyze] no ENCRYPTION_KEY provided — generated an ephemeral one for this container."
fi

exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-7860}"
