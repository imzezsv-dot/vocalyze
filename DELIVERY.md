# Delivery — Integration & Privacy

Vocalyze's integration and privacy layer. Everything listed under this role in
the responsibilities document is in this repository: **UI · Upload Flow ·
FastAPI Backend · Integration · Privacy Requirements**.

---

## What to hand in

| Deliverable | Where | For |
|---|---|---|
| Source code | This repository | Review, and the report bibliography |
| Live demo | The Vercel deployment (below) | The demo slot in the presentation |
| Docs | [`README.md`](README.md), [`docs/API.md`](docs/API.md), [`docs/PRIVACY.md`](docs/PRIVACY.md), [`docs/INTEGRATION.md`](docs/INTEGRATION.md) | The report appendix |
| Test evidence | `pytest` — 97 tests, no network, no weights | The "how do you know it works" question |

The other components — the dataset work, Whisper, pyannote and the LLM — plug
into this layer through the three contracts in `app/pipeline/`:
`ASRBackend`, `DiarizationBackend`, `SummarizerBackend`. Adding a real model
takes no code change here, only an environment variable. That is the
integration story to open the demo with.

---

## Running it

**Locally, for a reviewer:**

```bash
./run.sh          # virtualenv + encryption key + uvicorn, one command
# then open http://127.0.0.1:8000
```

No model downloads: the scripted backends return the same shapes Whisper and
pyannote do, so the interface is exercised end to end offline.

**The tests:**

```bash
pytest            # 97 tests, ~2 seconds
```

**With the team's real models:**

```bash
pip install -r requirements-models.txt
```

```ini
ASR_BACKEND=whisper
DIARIZATION_BACKEND=pyannote
HUGGINGFACE_TOKEN=hf_…          # after accepting the pyannote licence
SUMMARIZER_BACKEND=llm
LLM_BASE_URL=http://127.0.0.1:11434/v1
```

---

## The two deployments

### Vercel — the always-on public URL

```
1. vercel.com/new → import github.com/imzezsv-dot/vocalyze → name it "vocalyze".
2. Framework preset: Other. Root directory: repo root. Build command: (none).
3. Deploy. Vercel reads vercel.json and:
     - runs api/index.py as the serverless FastAPI backend
     - serves public/* from the edge CDN
```

Set `ENCRYPTION_KEY` (from `python -m app.core.crypto`) in the project's
environment variables; without one, each invocation mints an ephemeral key,
which is fine for a preview and wrong for anything else.

Because serverless functions have no background worker and no persistent
disk, the upload runs the pipeline **inline** and returns the finished result
in the same HTTP round-trip — the interface notices `result` in the response
and skips polling. The scripted backends finish in about 100 ms, well inside
the 10-second Hobby ceiling. `tests/test_deployment.py` covers this path.

**What the Vercel build can and cannot do:**

- ✅ the real interface, the real upload flow, the real HTTP API
  (`/v1/jobs`, `/v1/privacy/policy`, `/v1/capabilities`, `/v1/health`, `/docs`),
  and genuine speaker attribution and grounding over the scripted meeting.
- ❌ Whisper, pyannote, an LLM. Vercel has no ffmpeg, no persistent disk and
  no long-running worker, and the weights are gigabytes. The interface says so
  itself: it reads `/v1/capabilities` and badges the build as scripted rather
  than pretending a sample is your meeting.

### Docker — where the real models run

Any host that takes a Dockerfile: Hugging Face Spaces, Render, Fly, Railway.

```
1. huggingface.co/new-space → Docker template, name it "vocalyze".
2. Settings → Variables and secrets:
     ENCRYPTION_KEY      = (output of `python -m app.core.crypto`)
     ASR_BACKEND         = whisper
     DIARIZATION_BACKEND = pyannote
     HUGGINGFACE_TOKEN   = hf_…
     SUMMARIZER_BACKEND  = llm          # optional
3. git remote add hf https://huggingface.co/spaces/<you>/vocalyze
4. git push hf HEAD:main
```

The Space reads `README_HF.md` as its front matter — that `---` block sets the
SDK to Docker and the port to 7860.

The two are complementary: Vercel is the always-on preview of the system's
shape; the Docker image is the same system with the real models behind it.

---

## The presentation slot

Five minutes, in this order:

1. Open the deployment. The hero states the promise in one line: *who said
   what, and what you decided.*
2. Press **See a finished example**. The brief and the transcript render side
   by side. Press any `u12`-style id in the brief and the transcript scrolls to
   the line that claim came from. That chip is the integration story: three
   models, one verifiable output.
3. Scroll to **The ledger** — the audit chain, the retention window and the
   receipts. None of it is a claim; all of it is inspectable live at
   `/v1/privacy/policy` and `/v1/privacy/audit/verify`.
4. `/docs` for the OpenAPI contract if the audience is technical.

---

## What is deliberately not here

- No dataset code, no ASR training or WER script, no diarization notebook, no
  LLM prompt engineering. Those belong to the other components and reach this
  layer through the contracts above.
- No authentication or user accounts. A bearer token gates each job; there are
  no users. That is the right shape for a lab deployment — a shared production
  service would put SSO on top of it.
