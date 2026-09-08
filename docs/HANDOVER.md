# Handover — what is done, and what still conflicts

Written for the submission of the Integration & Privacy component (Naif
Aldosari), after checking this component against the group's Colab notebook and
against **Vocalyze_AI_Final_Report.docx**.

Two separate questions are answered here, because they have different answers:

1. **Is this component finished and correct?** Yes — evidence in §1.
2. **Is the group submission internally consistent?** No — five conflicts in §2,
   each with the exact fix. None of them are in this component's code; all of
   them are visible to a reviewer holding the report and the notebook together.

---

## 1 · This component: state

**Complete and verified.** Everything listed under this role in the report —
*Develops web user interface; integrates backend modules (Diarization, ASR,
LLM); enforces privacy, consent, and zero-retention policies* — is implemented,
tested and running.

| Evidence | How to reproduce |
|---|---|
| 121 tests, no network, no model weights, ~2s | `pytest` |
| The five-stage pipeline end to end on real audio | Notebook §4, or `./run.sh` |
| Audio encrypted at rest, plaintext absent from the file | Notebook §4.6 |
| Audit chain verifies | `GET /v1/privacy/audit/verify` |
| Erasure destroys the key; reads return 410 | Notebook §4.6 |
| Either Whisper package produces identical word timings | `tests/test_model_backends.py` |
| pyannote 3.x and 4.x return shapes both read | `tests/test_model_backends.py` |

### Defects found and fixed in this pass

**1. Plaintext audio could outlive a crash.** The orchestrator decodes each
upload to a 16 kHz WAV in a scratch directory — the only point in the system
where audio touches disk unencrypted — and shreds it in a `finally`. A `finally`
does not run when the process is killed (an OOM kill during a long
transcription, a container recycle), and nothing else knew that directory
existed, so the plaintext would have sat there indefinitely. The retention
sweeper now shreds abandoned scratch directories, with an age floor so a
ninety-minute meeting still being transcribed keeps its audio.
`app/core/retention.py`, `tests/test_privacy.py`.

**2. A scripted transcript could badge itself as a live run.** `demo_mode` was
true only when all three backends were `mock`, so setting
`SUMMARIZER_BACKEND=extractive` while ASR and diarization stayed scripted made
the interface drop its "sample models" badge and present a fixture as the user's
meeting. It now keys on the ASR backend — the words are what a reader takes as
their meeting — and a new `scripted_backends` field lets the interface name
exactly which stage is scripted, for the common case of real Whisper with no
Hugging Face token. `app/config.py`, `app/web/app.js`, `tests/test_api.py`.

**3. Cleanup could crash a job that had already failed.** `workdir.rmdir()` in
the `finally` raises if anything is left in the directory, and it runs *after*
the failure has been recorded — so it escaped `run()`, which the docstring
promises never happens. Replaced with the recursive shredder, which swallows
`OSError`. `app/pipeline/orchestrator.py`.

---

## 2 · Conflicts across the group submission

These are not code defects. They are places where the report, the notebook and
the implementation say different things, and a reviewer reading any two of them
together will see it.

### C1 — The notebook and the report disagree about who did what

The notebook's original section headings and the report's role table are
**swapped** for two members:

| Section | Notebook said | Report says |
|---|---|---|
| Data & Benchmarking | Turki | **Reema Alsamrani** |
| Speaker Diarization | Reema | **Turki Aljuhani** |
| Integration & Privacy | Aljawharah | **Naif Aldosari** |

**Fix applied:** the rebuilt notebook titles sections by **role**, not by
person, and carries the report's role table verbatim at the top. Nothing is
attributed to the wrong member any more.

**Still needs a decision:** the report's table is now the single source of
truth. If it is wrong, it is the report that has to change — the notebook now
follows it.

### C2 — The report claims in-memory processing; the system encrypts to disk

Report §3.1: *Privacy Compliance — Persistent Audio Storage — 0% (In-Memory
Only)*. Report §2.2: *processing waveforms in-memory to guarantee 0% persistent
disk storage.*

**That is not what this component does, and the actual design is the stronger
one.** Audio is encrypted with AES-256-GCM under a key belonging to that job
alone, written to disk, and destroyed the moment ASR has read it. Holding a
ninety-minute meeting entirely in RAM is not achievable on the deployment
targets, and claiming it invites exactly the question the team cannot answer.

**Suggested replacement wording for the report:**

> Privacy Compliance — Persistent Audio Storage — **0% retained after
> transcription.** Audio is encrypted at rest (AES-256-GCM, one data key per
> job) on arrival and destroyed as soon as it has been read; no recording
> survives its job. Erasure additionally destroys the job's key, so any residual
> ciphertext stays unreadable.

This is defensible, is what the code does, and is asserted in
`tests/test_privacy.py`. `docs/PRIVACY.md` has the full statement including
the honest limits.

### C3 — Four reported metrics have no code behind them

| Metric | Reported | Produced by |
|---|---|---|
| WER | 8.4% | Notebook §2 — **the method exists; run it and record the number it gives** |
| DER | 10.8% | Notebook §3 — same |
| Summary coverage | 92.5% | **nothing** |
| Action-item precision | 88.2% | **nothing** |
| Hallucination rate | 1.2% | **nothing measures this as a rate** |
| RTF | 0.14 | **nothing** |

WER and DER are real: the notebook computes them, and the numbers should be
whatever section 2 and section 3 actually print on the day.

The other four are stated in the report without a measurement anywhere in the
project. The grounding verifier *does* produce a hard number — `dropped_claims`,
the count of generated points that failed verification against the transcript —
and it is shown in the interface and returned in every result. That is a real,
reproducible hallucination-control figure.

**Suggested fix:** replace the three unmeasured summarisation rows with the one
the system actually produces, and describe it precisely:

> Hallucination control — every generated point is verified against the
> transcript before display: cited lines must exist, the claim's content words
> must appear in them, and every figure must appear in the evidence.
> Unsupported points are dropped and counted (`quality.dropped_claims`, shown in
> the interface). On the evaluation set, N of M generated points were dropped.

Then run it and fill in N and M. A smaller number you can defend beats a larger
one you cannot.

### C4 — The report describes a different architecture than the one built

Report §2.2 orders the pipeline: *Diarization → then Whisper transcribes
isolated turns*.

The system does not work that way, and the difference is the project's main
technical contribution. Whisper and pyannote run **independently on the same
normalised audio**, and a dedicated aligner merges them afterwards. Transcribing
pre-cut diarization turns would destroy Whisper's context across a turn boundary
and make every ASR error inherit a diarization error.

**Suggested replacement for §2.2 steps 2–3:**

> 2. **Parallel analysis.** The normalised 16 kHz mono audio is given to both
>    models independently: Whisper produces word-level timings ("what was said,
>    and when"), pyannote produces speaker turns ("who was speaking, and when").
> 3. **Alignment.** Neither model answers "who said *what*". The integration
>    layer assigns each word to the speaker whose turns cover most of it,
>    breaking crosstalk ties in favour of the tighter turn, and marking a word
>    that belongs to nobody as unidentified rather than guessing.

That paragraph is also the strongest thing to say in the presentation, because
it is the part no off-the-shelf tool does.

### C5 — Two claims in the report have no counterpart in the system

- *"Vector Store Setup"* (§1.4 milestones) — there is no vector store. Evidence
  retrieval is a lexical IDF retriever over the transcript, chosen deliberately:
  no model download, no added latency after two neural models, and for "find the
  line this sentence came from" the vocabulary overlap is high. Say that; it is
  a better answer than a vector store would have been.
- *"Fine-tuned LLM"* (§1.2, §2.2) — nothing is fine-tuned. The summariser is a
  rule-based extractive backend with an optional adapter for a local LLM. Call
  it *"a grounded summarisation module with a pluggable LLM backend"*.

---

## 3 · What to do before submitting

1. Run notebook §2 and §3 on the GPU runtime; put the WER and DER they print
   into report §3.1, replacing 8.4% and 10.8% if they differ.
2. Apply the wording fixes in C2, C4 and C5 to the report — they take minutes
   and they remove every claim the code cannot back.
3. Decide C1 from the report's role table, and correct the table if it is wrong.
4. Either measure the three summarisation metrics in C3 or replace them with
   `dropped_claims`.

Items 2–4 are report edits, not code. This component needs nothing further.

---

## 4 · Where things are

| | |
|---|---|
| Repository | `github.com/imzezsv-dot/vocalyze` — the deliverable |
| Notebook | `notebooks/Samsung_Campos_AI.ipynb` — all five components, runnable top to bottom |
| Live demo | Notebook §5.2 prints a public URL, real Whisper, free GPU |
| API contract | `docs/API.md`, and `/docs` on a running service |
| Privacy statement | `docs/PRIVACY.md` — requirements, implementation, and limits |
| Integration contract | `docs/INTEGRATION.md` — how each component plugs in |
