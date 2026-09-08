---
title: Vocalyze
emoji: 🎙️
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Meeting recordings → attributed transcript + grounded brief
---

# Vocalyze — integration & privacy layer

Upload a meeting recording. Get a speaker-attributed transcript and a brief
in which every point cites the line it came from.

By default the Space boots in seconds on scripted backends. The image ships
with the model dependencies installed, so switching to the real ones is
configuration, not a rebuild.

## Switching on the real models

In **Settings → Variables and secrets**:

| Name | Value | Kind |
|---|---|---|
| `ENCRYPTION_KEY` | output of `python -m app.core.crypto` | secret |
| `HUGGINGFACE_TOKEN` | `hf_…` | secret |
| `ASR_BACKEND` | `whisper` | variable |
| `DIARIZATION_BACKEND` | `pyannote` | variable |
| `WHISPER_MODEL` | `small` on CPU, `large-v3` on GPU | variable |
| `SUMMARIZER_BACKEND` | `llm`, with `LLM_BASE_URL` — optional | variable |

**Before pyannote will load**, accept the terms with the same account the
token belongs to, on *both* pages:

- <https://hf.co/pyannote/speaker-diarization-3.1>
- <https://hf.co/pyannote/segmentation-3.0>

Without that the hub answers 401 and diarization is skipped. The transcript
still comes out — every line is just labelled `UNKNOWN`, and the interface
says the diarizing stage failed rather than hiding it.

## What to expect on the free CPU tier

The first transcription downloads the weights, so it is slow in a way later
ones are not. After that `small` runs at roughly real time: a ten-minute
meeting takes about ten minutes. `large-v3` is slower than real time and
belongs on a GPU tier.

Without paid persistent storage nothing survives a container restart, and the
retention sweeper deletes jobs after `RETENTION_HOURS` (24 by default) either
way.

Full docs are in the repository: `README.md`, `docs/API.md`,
`docs/PRIVACY.md`, `docs/INTEGRATION.md`.
