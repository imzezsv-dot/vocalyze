#!/usr/bin/env bash
# Start Vocalyze. Creates a virtualenv and an encryption key on first run.
#
#   ./run.sh              scripted models — instant, no downloads, sample meeting
#   ./run.sh --whisper    real Whisper on your own audio (downloads ~500 MB once)
#
# The default is scripted on purpose: it starts in seconds and needs no weights.
# It also means an upload returns the *sample* meeting rather than your
# recording — the badge in the interface says so, and --whisper is the switch.
set -euo pipefail
cd "$(dirname "$0")"

# Read before .env is sourced, so "the user asked for a model" can be told
# apart from "the .env template happens to name one". The template's default is
# large-v3, which on a laptop CPU transcribes several times slower than real
# time — a --whisper run that inherited it would look like a hang.
WHISPER_MODEL_REQUESTED="${WHISPER_MODEL:-}"

WHISPER=0
for arg in "$@"; do
  case "$arg" in
    --whisper) WHISPER=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements-dev.txt

if [ "$WHISPER" = "1" ]; then
  if ! command -v ffmpeg > /dev/null; then
    echo "ffmpeg is required to decode audio: apt install ffmpeg / brew install ffmpeg" >&2
    exit 1
  fi
  echo "Installing Whisper (first run only)..."
  pip install -q faster-whisper
fi

if [ ! -f .env ]; then
  cp .env.example .env
  KEY="$(python -m app.core.crypto)"
  # macOS sed needs an argument to -i; GNU sed does not.
  sed -i.bak "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=${KEY}|" .env && rm -f .env.bak
  echo "Wrote .env with a fresh encryption key."
fi

set -a; source .env; set +a

# After .env, not before: sourcing it would otherwise put ASR_BACKEND=mock back
# and --whisper would silently do nothing but download half a gigabyte.
if [ "$WHISPER" = "1" ]; then
  export ASR_BACKEND=whisper
  # `base` on CPU: it beats real time on a laptop. Override deliberately with
  #     WHISPER_MODEL=small ./run.sh --whisper
  export WHISPER_MODEL="${WHISPER_MODEL_REQUESTED:-base}"
  export WHISPER_DEVICE="${WHISPER_DEVICE:-auto}"
  export SUMMARIZER_BACKEND=extractive
  echo
  echo "Real Whisper (${WHISPER_MODEL}) — uploads transcribe your own audio."
  echo "Speaker separation stays approximate until you set HUGGINGFACE_TOKEN"
  echo "and DIARIZATION_BACKEND=pyannote in .env."
  echo
fi

exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --reload
