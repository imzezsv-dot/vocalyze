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

Upload a meeting recording. Get a speaker-attributed transcript and a brief in
which every point cites the line it came from.

**This Space transcribes real audio.** Whisper runs on your upload — not a
sample. The weights are baked into the image, so the first request is as fast
as the rest.

## What runs here

| Stage | Backend | Notes |
|---|---|---|
| Speech recognition | **Whisper `base`**, int8 on CPU | your words, really transcribed |
| Speaker separation | approximate | needs a token — see below |
| Brief | extractive, grounded | every point verified against the transcript |

Limits are sized for two shared CPU cores: **50 MB, 15 minutes**. A longer
recording is refused with a message that says so, rather than appearing to
work and never finishing.

The interface badges which stage is scripted. A build where the words are real
but the speaker labels are approximate says exactly that — it is not presented
as a full run.

## Switching on real speaker separation

pyannote's weights are gated, so they cannot ship in the image. In
**Settings → Variables and secrets**:

| Name | Value | Kind |
|---|---|---|
| `HUGGINGFACE_TOKEN` | `hf_…` | secret |
| `DIARIZATION_BACKEND` | `pyannote` | variable |

First accept the terms with the same account the token belongs to, on *both*
pages:

- <https://hf.co/pyannote/speaker-diarization-3.1>
- <https://hf.co/pyannote/segmentation-3.0>

Without that the hub answers 401, diarization is skipped, and every line is
labelled `UNKNOWN` — the transcript still comes out, and the interface says the
diarizing stage failed rather than hiding it.

## Other settings worth knowing

| Name | Default | Why you would change it |
|---|---|---|
| `ENCRYPTION_KEY` | generated per boot | set it (secret) to keep jobs readable across restarts |
| `WHISPER_MODEL` | `base` | `small` on a paid CPU tier, `large-v3` on a GPU tier |
| `ASR_BACKEND` | `whisper` | `mock` for a fast-booting preview on the sample meeting |
| `RETENTION_HOURS` | `24` | how long a job survives before the sweeper deletes it |

## Notes

A free Space sleeps after inactivity and takes about thirty seconds to wake —
open it once before a presentation.

Without paid persistent storage nothing survives a container restart, and the
retention sweeper deletes jobs after `RETENTION_HOURS` either way.

Full docs in the repository: `README.md`, `docs/API.md`, `docs/PRIVACY.md`,
`docs/INTEGRATION.md`.
