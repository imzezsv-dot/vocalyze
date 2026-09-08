# Deploy — two one-click buttons

## 1 · Vercel preview (live UI + API, mock pipeline)

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fimzezsv-dot%2Ftest&project-name=vocalyze&repository-name=vocalyze)

- Click the button, sign in to Vercel, press **Deploy**. No configuration to
  fill in — `vercel.json` and `api/index.py` do the work.
- Live in about 60 seconds. URL looks like `vocalyze-xxx.vercel.app`.
- What you get: the real FastAPI backend, the interface, upload flow,
  privacy endpoints, `/docs` — everything except the model weights.

## 2 · Hugging Face Space (real Whisper + pyannote + LLM)

[![Open in Spaces](https://huggingface.co/datasets/huggingface/badges/resolve/main/deploy-to-spaces-lg.svg)](https://huggingface.co/new-space?template=docker&sdk=docker)

- Click the button, sign in to Hugging Face, name it `vocalyze`, template
  = **Docker**.
- After it opens: **Files → Add file → Upload files**, drop in this repo
  (or `git push` to the Space's remote). It builds the `Dockerfile` and
  boots on port 7860.
- To switch on the real models, in **Settings → Variables & secrets**:
  ```
  ENCRYPTION_KEY       = <paste from `python -m app.core.crypto`>
  ASR_BACKEND          = whisper
  DIARIZATION_BACKEND  = pyannote
  HUGGINGFACE_TOKEN    = hf_… (after accepting the pyannote licence)
  SUMMARIZER_BACKEND   = llm        # optional
  LLM_BASE_URL         = https://…  # optional
  ```
- First real run downloads Whisper (a few minutes on CPU). Upgrade the
  Space to a GPU tier if you want minute-scale transcription instead of
  hour-scale.

## Why two deployments?

- **Vercel is the always-on public URL.** It shows the interface, the API
  contract, the upload → attributed transcript → grounded brief flow.
  Runs the mock backends because Vercel serverless functions physically
  cannot host multi-GB model weights or ffmpeg-heavy pipelines
  (250 MB unzipped function limit, 10–60 s execution limit, no persistent
  disk, no GPU). This is the case for every Vercel Python deployment —
  not a limitation of this project.
- **HF Space is where the real Whisper / pyannote / LLM run.** Docker
  container, persistent disk, free CPU tier, optional GPU. The same
  codebase, same interface — flipped by three env vars.

Both point at the same repo. Update code once, both deployments follow.

---

## What still needs a human click

I can prepare every file and script, but the final **Deploy** button on
Vercel and Hugging Face requires *your* account login. That's the only
step I cannot take for you — the platforms don't accept third-party
deploys without a session token that only you can generate.

If you paste a Vercel personal token here (`vercel.com/account/tokens`),
I can run `vercel deploy --token=<token> --prod` from this session and
hand you back the live URL directly.
