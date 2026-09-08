# Integration

How three separately built models become one product, and what each component
owner has to provide.

---

## The problem this layer solves

Whisper answers *what was said and when*. pyannote answers *who was speaking
and when*. The LLM answers *what it all meant*. None of them answers the
question a person actually has — **who said what, and what did we decide** —
and the gaps between them are where the work is:

- **Two clocks that never agree.** Whisper's word timings drift at segment edges; pyannote's boundaries land mid-word. Naively cutting one timeline by the other mislabels every handover in the meeting.
- **Crosstalk breaks both models at once.** Where two people talk over each other, ASR transcribes the louder one and diarization emits overlapping turns. Both are wrong in the same seconds.
- **The LLM will occasionally invent a decision.** Prompting reduces it, but a summary nobody can check is a summary nobody should act on.
- **Every stage handles personal data.** Privacy has to be enforced *between* the stages, where the data actually moves.

---

## Shape

```
   upload  ──►  FastAPI  ──►  job queue  ──►  orchestrator
                  │                              │
                  │                              ├─ 1 normalize    ffmpeg → 16 kHz mono
                  │                              ├─ 2 transcribe   ASRBackend          ← ASR owner
                  │                              ├─ 3 diarize      DiarizationBackend  ← diarization owner
                  │                              ├─ 4 align        align_core + redaction
                  │                              └─ 5 summarize    SummarizerBackend   ← LLM owner
                  │                                                 + grounding_core
                  ▼
              interface  ◄── encrypted result store
```

Stages 2, 3 and 5 are seams. This layer depends on the three protocols below
and never imports Whisper, pyannote or an LLM SDK directly, so each owner can
change model, framework or checkpoint without touching the API, the aligner or
the interface.

---

## Contracts

Implement one class, register it in `app/pipeline/registry.py`, select it with
an environment variable. Nothing else changes.

### ASR — `app/pipeline/asr.py`

```python
class ASRBackend(Protocol):
    name: str
    def transcribe(self, audio_path: Path) -> ASRResult: ...
```

`ASRResult` carries segments, and each segment carries `words` with `start`,
`end`, `text` and `confidence`.

**Word timings are the contract that matters.** With them, speaker attribution
is accurate to the word. Without them, the aligner falls back to splitting a
segment's text proportionally across the speakers active during it — a
noticeably worse approximation, marked as such by a lower `speaker_confidence`.
So `word_timestamps=True`, always.

Provided: `WhisperASR` (faster-whisper, falling back to openai-whisper) and
`MockASR`.

### Diarization — `app/pipeline/diarization.py`

```python
class DiarizationBackend(Protocol):
    name: str
    def diarize(self, audio_path: Path, num_speakers: int | None = None) -> DiarizationResult: ...
```

Turns may overlap — that is how crosstalk is expressed, and the aligner needs
it. Do not merge overlapping turns to make the output tidy; the interface
reports overlap as uncertainty and that is more useful than a clean guess.

Provided: `PyannoteDiarizer` (speaker-diarization-3.1) and `MockDiarizer`.

### Summarizer — `app/pipeline/summarizer.py`

```python
class SummarizerBackend(Protocol):
    name: str
    def summarize(self, utterances: list[dict], language: str | None) -> dict: ...
```

Return this shape. Every claim must cite the utterance ids it came from:

```json
{
  "summary": "...",
  "summary_utterance_ids": ["u1", "u4"],
  "key_points":  [{"text": "...", "utterance_ids": ["u2"]}],
  "decisions":   [{"text": "...", "utterance_ids": ["u9"]}],
  "action_items":[{"text": "...", "owner": "Rima", "due": "Thursday", "utterance_ids": ["u12"]}]
}
```

Provided: `LLMSummarizer` (any OpenAI-compatible endpoint, local or hosted)
and `ExtractiveSummarizer`, which selects real sentences from the transcript
and therefore cannot hallucinate — used in demo mode and as the fallback when
the LLM is unreachable, so a degraded run still produces something usable.

---

## The aligner

`app/pipeline/align_core.py`. Deliberately stdlib-only and framework-free: it
is the part worth testing hardest, and it should be testable without standing
up an application.

**Every word goes to the speaker whose turns cover most of it.** Overlap
maximisation rather than boundary matching, because the boundaries are the
part both models get wrong.

Four decisions worth knowing about:

1. **Ties go to the tighter turn.** During crosstalk two turns cover a word equally. A short turn bracketing that word is stronger evidence than a long turn that merely spans it.
2. **Isolated flips are smoothed.** A run of one or two words attributed to a different speaker, surrounded on both sides by one other speaker, is boundary noise — nobody speaks a single word inside someone else's sentence and hands it straight back. Longer runs are left alone; those are real interjections and get their own utterance.
3. **A word far from every turn becomes `UNKNOWN`.** Adopted by the nearest turn within 0.5 s, otherwise labelled unidentified. An honest gap beats a confident guess.
4. **Crosstalk is flagged, not resolved.** When a competing speaker covers 30% or more of an utterance, it is marked `overlapped` and the interface shows it as such.

If diarization fails entirely, alignment still runs: every utterance becomes
`UNKNOWN` and the transcript survives. Losing speaker labels should not lose
the meeting.

Measured against the scripted fixture, which has known ground truth: **100%
word-level speaker accuracy**, with the injected crosstalk correctly flagged
(`tests/test_alignment.py::TestFixtureRoundTrip`).

---

## Grounding

`app/pipeline/grounding_core.py`. Every generated claim is checked against the
transcript before it reaches the screen:

1. Cited utterance ids must exist.
2. The claim's content words must appear in the cited utterances.
3. **Every number in the claim must appear in the evidence.** Invented figures are the most damaging hallucination and the most detectable.

A claim citing nothing is not discarded immediately — an IDF-weighted lexical
retriever looks for the lines that would support it, and if it finds them the
claim survives with real citations. Below `MIN_EVIDENCE_OVERLAP` (0.55), it is
dropped and counted, and the count is shown in the interface.

Lexical rather than embedding-based on purpose: no model download, no added
latency after two neural models have already run, and for "find the line this
sentence came from" the vocabulary overlap is high.

This complements the LLM owner's own hallucination controls rather than
replacing them. Prompting makes the model retrieve instead of recall; this
layer verifies that it did.

---

## Adding a real model

```bash
pip install -r requirements-models.txt

# .env
ASR_BACKEND=whisper
DIARIZATION_BACKEND=pyannote
HUGGINGFACE_TOKEN=hf_...        # accept the pyannote model terms first
SUMMARIZER_BACKEND=llm
LLM_BASE_URL=http://127.0.0.1:11434/v1   # local model keeps transcripts on the machine
LLM_MODEL=llama3.1
```

Nothing else changes. `/v1/capabilities` reports the switch and the interface
stops calling itself a demo.

---

## Failure behaviour

| Failure | Result |
|---|---|
| ffmpeg missing or file undecodable | Job fails at `normalizing` with a message naming the fix |
| ASR finds no speech | Job fails with "No speech was found in this recording" |
| Diarization crashes or has no token | Pipeline continues; all speakers `UNKNOWN`, stage marked failed, transcript still delivered |
| LLM unreachable or returns bad JSON | Falls back to the extractive brief, flagged `degraded` |
| LLM invents a claim | Dropped by grounding, counted, count shown in the interface |
| Worker dies mid-job | Job stays in its last stage; the retention sweeper removes its data at expiry |
| Server restarts | Queue is in-process, so queued jobs are lost. Single-node trade-off; the `JobQueue` seam is where a Celery or RQ broker would go |

---

## Where the team's work meets

| Component | Owner | Contract | Consumed by |
|---|---|---|---|
| Dataset, 16 kHz preprocessing, speaker-disjoint split | data component | 16 kHz mono WAV; the same normalisation is applied here at ingest | ASR, diarization |
| Whisper ASR, WER evaluation | ASR component | `ASRBackend` → `ASRResult` with word timings | the aligner |
| pyannote diarization, DER evaluation | diarization component | `DiarizationBackend` → overlapping `SpeakerTurn`s | the aligner |
| Summary, key points, decisions, actions | LLM component | `SummarizerBackend` → JSON with `utterance_ids` | grounding, then the interface |
| Upload, API, alignment, grounding, interface, privacy | this layer | the whole product | the demo and the report |
