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

- **Backends by default:** scripted (mock) so the Space boots in seconds.
- **Real models:** set `ASR_BACKEND=whisper`, `DIARIZATION_BACKEND=pyannote`,
  `SUMMARIZER_BACKEND=llm` in the Space's secrets (and add `HUGGINGFACE_TOKEN`
  after accepting the pyannote licence).
- Full docs are inside the repo: `docs/README.md`, `docs/API.md`,
  `docs/PRIVACY.md`, `docs/INTEGRATION.md`.
