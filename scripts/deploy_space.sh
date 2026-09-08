#!/usr/bin/env bash
#
# Create the Hugging Face Space and push this repository to it.
#
#   ./scripts/deploy_space.sh <your-hf-username>
#
# Needs a Hugging Face account and a write token from
# https://huggingface.co/settings/tokens — the account is the one part of this
# that cannot be scripted away, because the Space is created under it and the
# pyannote licence is accepted by it.
#
# After this finishes, set the runtime variables in the Space's
# Settings → Variables and secrets. README_HF.md lists them.

set -euo pipefail
cd "$(dirname "$0")/.."

USERNAME="${1:-}"
SPACE_NAME="${2:-vocalyze}"

if [ -z "$USERNAME" ]; then
  echo "usage: $0 <hf-username> [space-name]" >&2
  exit 64
fi

if ! command -v hf >/dev/null 2>&1 && ! command -v huggingface-cli >/dev/null 2>&1; then
  echo "Installing the Hugging Face CLI..."
  pip install -q --upgrade "huggingface_hub[cli]"
fi
HF="$(command -v hf || command -v huggingface-cli)"

echo "==> Signing in (paste a WRITE token from https://huggingface.co/settings/tokens)"
"$HF" auth login || "$HF" login

echo "==> Creating the Space $USERNAME/$SPACE_NAME (Docker SDK)"
"$HF" repo create "$SPACE_NAME" --repo-type space --space_sdk docker -y 2>/dev/null \
  || echo "    (it already exists — pushing to it)"

REMOTE="https://huggingface.co/spaces/$USERNAME/$SPACE_NAME"

echo "==> Pushing this repository to the Space"
git remote remove hf 2>/dev/null || true
git remote add hf "$REMOTE"
git push --force hf HEAD:main

cat <<EOF

==> Done. The Space is building at:
      $REMOTE

    It boots on scripted backends. To switch on the real models, open
      $REMOTE/settings
    and add the variables listed in README_HF.md — starting with
      ENCRYPTION_KEY   = $(python3 -m app.core.crypto 2>/dev/null || echo '<run: python -m app.core.crypto>')
      HUGGINGFACE_TOKEN= hf_...
      ASR_BACKEND      = whisper
      DIARIZATION_BACKEND = pyannote

    And accept the model terms with the same account, on both pages:
      https://hf.co/pyannote/speaker-diarization-3.1
      https://hf.co/pyannote/segmentation-3.0
EOF
