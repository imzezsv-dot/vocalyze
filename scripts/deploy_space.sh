#!/usr/bin/env bash
#
# Publish Vocalyze as a Hugging Face Space that runs the real models.
#
#     ./scripts/deploy_space.sh
#
# You need a free Hugging Face account. Everything else — creating the Space,
# generating the encryption key, setting the variables and secrets, checking
# the pyannote licence, and pushing — happens here.
#
# The Hugging Face account is the one part that cannot be scripted: the Space
# is created under it, and the pyannote licence is accepted by it.

set -euo pipefail
cd "$(dirname "$0")/.."

SPACE_NAME="${1:-vocalyze}"

# `base` is the free CPU tier's model: int8 `base` runs faster than real time
# on 2 shared cores, `small` runs at roughly real time, and `large-v3` is far
# slower than real time. Override for a GPU tier:
#
#     WHISPER_MODEL=large-v3 ./scripts/deploy_space.sh
WHISPER_MODEL="${WHISPER_MODEL:-base}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }

# --------------------------------------------------------------- the CLI
# Everything goes in a virtualenv beside the repository: `pip` is not on the
# PATH of a stock macOS Python, and a Homebrew one refuses to install into
# itself anyway. Nothing here touches the system Python.
PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/" >&2
  exit 1
fi

if [ ! -x .venv/bin/hf ]; then
  step "Installing the Hugging Face CLI (in ./.venv, nothing system-wide)"
  [ -d .venv ] || "$PYTHON" -m venv .venv
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/python -m pip install -q --upgrade "huggingface_hub>=0.34" cryptography
fi

PY="$PWD/.venv/bin/python"
HF="$PWD/.venv/bin/hf"

# ------------------------------------------------------------- signing in
if ! "$HF" auth whoami >/dev/null 2>&1; then
  step "Sign in to Hugging Face"
  echo "    A browser will open, or paste a WRITE token from:"
  echo "    https://huggingface.co/settings/tokens"
  "$HF" auth login --add-to-git-credential
fi

USERNAME="$("$PY" -c 'from huggingface_hub import whoami; print(whoami()["name"])')"
TOKEN="$("$PY" -c 'from huggingface_hub import get_token; print(get_token() or "")')"

if [ -z "$USERNAME" ] || [ -z "$TOKEN" ]; then
  echo "Could not read the signed-in account. Run 'hf auth login' and try again." >&2
  exit 1
fi
echo "    Signed in as: $USERNAME"

# ------------------------------------------------- is pyannote usable yet?
step "Checking access to the pyannote models"
DIARIZATION="pyannote"
if ! "$PY" - "$TOKEN" <<'PY'
import sys
from huggingface_hub import model_info

token = sys.argv[1]
for repo in ("pyannote/speaker-diarization-3.1", "pyannote/segmentation-3.0"):
    model_info(repo, token=token)
PY
then
  DIARIZATION="mock"
  warn "Not available to this account yet, so speaker separation starts in approximate mode."
  warn "To switch it on, accept the terms with THIS account on both pages:"
  warn "  https://hf.co/pyannote/speaker-diarization-3.1"
  warn "  https://hf.co/pyannote/segmentation-3.0"
  warn "then set DIARIZATION_BACKEND=pyannote in the Space's settings."
else
  echo "    Both models are accessible."
fi

# ------------------------------------------------------ creating the Space
ENCRYPTION_KEY="$("$PY" -m app.core.crypto)"
REPO_ID="$USERNAME/$SPACE_NAME"

step "Creating the Space $REPO_ID"
"$HF" repos create "$REPO_ID" \
  --type space --sdk docker --public --exist-ok \
  --secrets "ENCRYPTION_KEY=$ENCRYPTION_KEY" \
  --secrets "HUGGINGFACE_TOKEN=$TOKEN" \
  --env "ASR_BACKEND=whisper" \
  --env "DIARIZATION_BACKEND=$DIARIZATION" \
  --env "SUMMARIZER_BACKEND=extractive" \
  --env "WHISPER_MODEL=$WHISPER_MODEL" \
  --env "WHISPER_DEVICE=cpu" \
  --env "MAX_UPLOAD_MB=50" \
  --env "MAX_DURATION_MINUTES=15"

# ----------------------------------------------------------- uploading it
# Uploaded rather than pushed: this touches no git remote, no branch and no
# credential helper, so a repository checked out from anywhere — under any
# GitHub account — is left exactly as it was found.
step "Uploading the code to the Space"
"$HF" upload "$REPO_ID" . . --type space \
  --exclude ".git/*" \
  --exclude ".venv/*" \
  --exclude "var/*" \
  --exclude "**/__pycache__/*" \
  --exclude "*.pyc" \
  --commit-message "Vocalyze — integration and privacy layer"

# A Space reads its configuration from the YAML front matter of README.md and
# never looks at README_HF.md. Without this step the Space does not know it is
# a Docker build on port 7860, and never starts.
"$HF" upload "$REPO_ID" README_HF.md README.md --type space \
  --commit-message "Space configuration"

# ------------------------------------------------------------------ done
cat <<EOF

$(printf '\033[1m==> Done.\033[0m')

    Your site:  https://huggingface.co/spaces/$REPO_ID

    It is building now — five to ten minutes the first time, because the image
    installs Whisper and bakes the '$WHISPER_MODEL' weights into itself. Watch
    the build log on that page; the site answers as soon as it turns green.

    After that it transcribes real audio with no download on the first request.
    Recordings up to 50 MB and 15 minutes, on two shared CPU cores.

    Diarization is '$DIARIZATION'. Approximate speaker separation shows in the
    interface as a badge naming the scripted stage, rather than being passed
    off as a real model.

    For a presentation: open it once before you present. A free Space sleeps
    after a period of inactivity and takes about thirty seconds to wake.
EOF
