# API reference

Base URL `http://127.0.0.1:8000`. Interactive docs at `/docs`.

Every response that is not a success has the same shape, and always says what
to do next:

```json
{ "error": "consent_required",
  "detail": "This deployment processes recordings only with recorded consent from the meeting participants.",
  "fix": "Send consent=true with the upload after confirming everyone in the recording agreed." }
```

---

## Upload a recording

```
POST /v1/jobs        multipart/form-data
```

| Field | Type | Notes |
|---|---|---|
| `file` | file | wav, mp3, m4a, flac, ogg, opus, webm, mp4 · up to `MAX_UPLOAD_MB` |
| `consent` | bool | required when `REQUIRE_CONSENT=true` |
| `redact_pii` | bool | overrides the deployment default for this job |
| `delete_audio_after_asr` | bool | overrides the deployment default |
| `retention_hours` | int | 1–720, overrides the deployment default |

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs \
  -F file=@meeting.m4a \
  -F consent=true
```

```json
{ "job_id": "kQ8vN2mZpT7x",
  "access_token": "9f3c...", 
  "state": "queued",
  "expires_at": "2026-09-09T07:12:44Z",
  "poll": "/v1/jobs/kQ8vN2mZpT7x" }
```

**Keep `access_token`.** It is shown once and every other call needs it. The
job id on its own grants nothing.

Errors: `400 empty_upload` · `403 consent_required` · `413 file_too_large` ·
`415 unsupported_format`

---

## Follow the job

```bash
curl http://127.0.0.1:8000/v1/jobs/kQ8vN2mZpT7x \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "id": "kQ8vN2mZpT7x", "state": "diarizing", "progress": 0.5,
  "stages": [
    {"name": "normalizing",  "state": "done",    "detail": "111s, 44100 Hz aac to 16000 Hz mono"},
    {"name": "transcribing", "state": "done",    "detail": "13 segments, faster-whisper:large-v3"},
    {"name": "diarizing",    "state": "running", "detail": null},
    {"name": "aligning",     "state": "pending", "detail": null},
    {"name": "summarizing",  "state": "pending", "detail": null}
  ] }
```

States: `queued` → `normalizing` → `transcribing` → `diarizing` → `aligning` →
`summarizing` → `completed`, or `failed`, or `purged`.

Live progress as server-sent events:

```
GET /v1/jobs/{id}/events?token=<token>
```

The token goes in the query string here because `EventSource` cannot set
headers. Poll the endpoint above if your proxy buffers SSE.

---

## Read the result

```bash
curl http://127.0.0.1:8000/v1/jobs/$JOB/result -H "Authorization: Bearer $TOKEN"
```

```json
{ "job": { "...": "" },
  "transcript": {
    "language": "en", "duration": 111.5,
    "speakers": ["SPEAKER_00", "SPEAKER_01"],
    "utterances": [
      { "id": "u1", "speaker": "SPEAKER_00", "start": 1.2, "end": 4.2,
        "text": "Alright, let's start with where the dataset stands.",
        "confidence": 0.91, "speaker_confidence": 1.0,
        "overlapped": false, "redacted": false }
    ] },
  "brief": {
    "summary": "...",
    "summary_evidence": { "utterance_ids": ["u2"], "grounding": 0.86, "verified": true },
    "decisions": [ { "text": "...", "evidence": { "utterance_ids": ["u10"], "grounding": 1.0, "verified": true } } ],
    "action_items": [ { "text": "...", "owner": "Rima", "due": "Thursday", "evidence": { "...": "" } } ],
    "dropped_claims": 0 },
  "quality": {
    "asr_mean_confidence": 0.9, "speaker_count": 4,
    "overlapped_utterances": 1, "unassigned_speech_seconds": 0.0,
    "grounded_claims": 9, "dropped_claims": 0, "redactions": 0 },
  "privacy": { "audio_retained": false, "encrypted_at_rest": true, "expires_at": "...", "models": { "...": "" } } }
```

Field notes:

- `speaker_confidence` — share of the utterance covered by the assigned speaker's turns. Below ~0.6, treat attribution as uncertain.
- `overlapped` — two or more speakers active in this span. Attribution is a best effort and is marked rather than hidden.
- `evidence.grounding` — share of the claim's wording found in the lines it cites.
- `evidence.verified` — false means the transcript does not support the claim. With `DROP_UNGROUNDED_CLAIMS=true` those never appear; `dropped_claims` counts them.

`409 result_not_ready` while the job is still running.

---

## Exports

```
GET /v1/jobs/{id}/transcript.{md|txt|srt|vtt|json}
```

`md` is the brief plus the transcript, `srt` and `vtt` carry speaker labels as
subtitles, `json` is the full payload.

---

## Correct speaker names

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs/$JOB/speakers \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"SPEAKER_00": "Turki", "SPEAKER_01": "Taghreed"}'
```

Names are stored inside the encrypted result and inherit its retention.

---

## Delete

```bash
curl -X DELETE http://127.0.0.1:8000/v1/jobs/$JOB -H "Authorization: Bearer $TOKEN"
```

Blobs are shredded and the job's encryption key destroyed. Later reads return
`410 job_purged`.

---

## Privacy and service

| Endpoint | Returns |
|---|---|
| `GET /v1/privacy/policy` | the privacy settings actually in force |
| `GET /v1/privacy/jobs/{id}/audit` | what the service did with one recording (token required) |
| `GET /v1/privacy/audit/verify` | whether the audit hash chain is intact |
| `POST /v1/privacy/sweep` | run the retention sweep now |
| `GET /v1/capabilities` | active backends, limits, demo mode |
| `GET /v1/health` | status and queue depth |
