# Delivery — Naif's part

Vocalyze / Integration & Privacy layer. Everything listed against Naif in
the responsibilities document is in this repo: **UI · Upload Flow · FastAPI
Backend · Integration · Privacy Requirements**.

---

## What to hand in

Send Saad (Documentation & Demo) these three links:

| Deliverable | Where | For |
|---|---|---|
| Source code | GitHub PR — this branch | Review, and the final report bibliography |
| Live demo | Hugging Face Space (setup below) | The demo slot in the presentation |
| Docs | `docs/README.md`, `docs/API.md`, `docs/PRIVACY.md`, `docs/INTEGRATION.md` | The report appendix |

The team's models (Turki's data, Taghreed's Whisper, Rima's pyannote,
Al-Jawharah's LLM) plug into this layer through the three contracts in
`app/pipeline/` — `ASRBackend`, `DiarizationBackend`, `SummarizerBackend`.
No code change here to add a real model, just an env var switch. That is
the integration story to open the demo with.

---

## Hugging Face Space (the live demo)

Free, has GPU tier, and takes a Dockerfile as-is. This is the closest fit
for the pipeline because the job queue is long-lived — **Vercel serverless
functions are the wrong shape** (10–60 s ceiling, no shared filesystem,
so no job progress and no encrypted store).

```
1. huggingface.co/new-space → Docker template, name it "vocalyze".
2. In Settings → Variables and secrets, add:
     ENCRYPTION_KEY   = (paste the output of `python -m app.core.crypto`)
     ASR_BACKEND      = mock         # switch to `whisper` when models load
     DIARIZATION_BACKEND = mock       # or `pyannote` + HUGGINGFACE_TOKEN
     SUMMARIZER_BACKEND  = mock       # or `llm` + LLM_BASE_URL
3. `git remote add hf https://huggingface.co/spaces/<you>/vocalyze`
4. `git push hf HEAD:main`
5. Space builds the Docker image, boots on port 7860, and hands you the URL.
```

The Space picks up `README_HF.md` as its front matter (the `---` block
sets the SDK to docker and the app port to 7860).

## Vercel (one-click, no Docker)

Vercel deploys this repo as-is — the FastAPI app runs on Vercel's Python
serverless runtime, static assets are served from the edge CDN. No
Dockerfile needed on Vercel's side.

```
1. vercel.com/new → import github.com/imzezsv-dot/test → project name "vocalyze".
2. Framework preset: Other. Root directory: repo root. Build command: (none).
3. Deploy. Vercel reads vercel.json and:
     - runs api/index.py as the serverless FastAPI backend
     - serves public/* from its edge CDN
```

That's it. The upload runs the pipeline inline (single HTTP round-trip
returns the finished result — no queue, no polling), because Vercel
serverless has no background workers and no persistent filesystem. The
mock backends produce a full transcript and brief in ~100 ms, well
inside Vercel's 10 s Hobby ceiling.

**What Vercel can and cannot do:**

- ✅ live upload flow, real HTTP API (`/v1/jobs`, `/v1/privacy/policy`,
  `/v1/capabilities`, `/v1/health`, `/docs`).
- ✅ full interface, real speaker attribution & grounding on the demo
  fixture.
- ❌ Whisper, pyannote, an LLM — Vercel has no ffmpeg, no persistent
  disk, no long-running worker, and the model weights are gigabytes.
  For the real pipeline use the Hugging Face Space (above), Render, or
  any Docker host.

The two are complementary: Vercel is the always-on public preview;
the Space is where the real models run.

## Render / Fly / Railway (alternatives)

`render.yaml` is a one-click Blueprint on Render. Fly and Railway both
accept the Dockerfile directly (`fly launch --dockerfile Dockerfile`,
`railway up`). Any of them work; pick whichever the team already uses.

## Locally (for the reviewer)

```
./run.sh                    # venv + key + uvicorn, single command
# then http://127.0.0.1:8000
```

`./run.sh` mints an ENCRYPTION_KEY on first run and writes it to `.env`.
No model downloads — the mock backends produce the same shape as Whisper
and pyannote, so the interface is exercised end-to-end offline.

To run with the team's real models:

```
pip install -r requirements-models.txt
# then set ASR_BACKEND=whisper etc. in .env
```

---

## The presentation slot (Saad)

If you get five minutes at the demo, this is the order I'd use:

1. Open the Space URL. Point at the italic "decided" in the hero — this
   is the product's whole promise in one word.
2. Press *See a finished example*. The brief and transcript render side
   by side; press any `u12`-style id in the brief to jump to the line it
   came from. That chip is the integration story: three models, one
   verifiable output.
3. Scroll to *The ledger*. The audit chain, the retention window, and
   the receipts checklist are the privacy story — none of them are a
   claim, all of them are inspectable at `/v1/privacy/*`.
4. `/docs` for the OpenAPI contract if the audience is technical.

---

## What is *not* here on purpose

- No dataset code (Turki's), no ASR training or WER script (Taghreed's),
  no diarization notebook (Rima's), no LLM prompt engineering
  (Al-Jawharah's). Those live in the team notebook and plug into this
  layer through the contracts above.
- No authentication or user accounts. A job token gates each job; there
  are no users. That is the right shape for a demo and a lab deployment;
  a shared production service would add SSO on top.
