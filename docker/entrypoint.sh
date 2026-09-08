#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/home/vocalyze/data}"
mkdir -p "$DATA_DIR"

if [ -z "${ENCRYPTION_KEY:-}" ]; then
  export ENCRYPTION_KEY="$(python -m app.core.crypto)"
  echo "[vocalyze] No ENCRYPTION_KEY was provided — generated an ephemeral one."
  echo "[vocalyze] Jobs stored under this key become unreadable when the container restarts."
  echo "[vocalyze] Set ENCRYPTION_KEY in the host's secrets to keep them across restarts."
fi

echo "[vocalyze] asr=${ASR_BACKEND:-mock} diarization=${DIARIZATION_BACKEND:-mock} summariser=${SUMMARIZER_BACKEND:-mock}"

# A real model is downloaded on first use, not at build time — the weights are
# gigabytes and licence acceptance is per-account. Say so, so a slow first
# request reads as a download rather than a hang.
if [ "${ASR_BACKEND:-mock}" != "mock" ] || [ "${DIARIZATION_BACKEND:-mock}" != "mock" ]; then
  echo "[vocalyze] Real models selected. The first transcription downloads weights and will be slow."
fi

exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-7860}"
