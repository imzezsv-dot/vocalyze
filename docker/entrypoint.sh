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

if [ "${ASR_BACKEND:-mock}" = "mock" ]; then
  echo "[vocalyze] ASR is scripted: an upload returns the sample meeting, not the caller's audio."
  echo "[vocalyze] Set ASR_BACKEND=whisper to transcribe real recordings."
else
  echo "[vocalyze] Transcribing real audio with Whisper ${WHISPER_MODEL:-base} on ${WHISPER_DEVICE:-auto}."
fi

# pyannote is downloaded on first use, not at build time: the weights are
# gated and licence acceptance is per-account, so they cannot be baked in.
if [ "${DIARIZATION_BACKEND:-mock}" = "pyannote" ]; then
  echo "[vocalyze] pyannote selected — the first request downloads its weights and will be slow."
fi

exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-7860}"
