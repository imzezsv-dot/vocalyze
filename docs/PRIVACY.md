# Privacy requirements

Vocalyze processes recordings of people talking, unprompted, about their work.
A meeting recording is not a document someone chose to write — it captures
hesitations, disagreements and things said in the belief that they were
ephemeral. That makes it some of the most sensitive material a team will ever
put through a machine learning pipeline.

This document states what the system must guarantee, how each guarantee is
implemented, and how anyone can check it holds. Every requirement below has a
line of code and a test behind it; where a guarantee is weaker than it sounds,
that is written down too.

---

## 1. Data map

What the system holds, where it lives, and how long.

| Data | Source | Where it is stored | Protection | Lifetime |
|---|---|---|---|---|
| Uploaded recording | the person uploading | `var/vocalyze/jobs/<id>/original.enc` | AES-256-GCM, per-job key | Destroyed as soon as a normalised copy exists (seconds) |
| Normalised audio (16 kHz mono) | derived | `.../audio.enc` | AES-256-GCM, per-job key | Destroyed when both models have read it |
| Transcript, speaker labels, brief | derived | `.../result.enc` | AES-256-GCM, per-job key | Retention window, default 24 hours |
| Job metadata (filename, size, duration, timings) | derived | `.../meta.json` | Filesystem, mode 0600 | Retention window; a tombstone survives deletion |
| Per-job data key | generated | `.../meta.json`, wrapped by the service key | AES-256-GCM key wrapping | Destroyed on erasure — this is what makes deletion final |
| Audit trail | derived | `var/vocalyze/audit.log` | Hash-chained, append-only | Kept: it is the evidence that deletion happened |
| Application logs | derived | stdout | Redaction filter on every record | Operator's log policy |

Nothing else is collected. There are no accounts, no analytics, no cookies, no
device fingerprints, and no cross-job linkage: two recordings from the same
person share no identifier.

---

## 2. Requirements

### PR-1 — Processing requires recorded consent

A recording captures people who are not the uploader and who cannot agree at
upload time. The system cannot obtain their consent, so it requires the
uploader to attest that it was obtained, and records that attestation with a
timestamp.

- **Implemented:** `require_consent` in `app/config.py`; enforced in `app/api/jobs.py`; the attestation and its time are stored in the job's `privacy` block; the interface disables the upload button until the box is ticked.
- **Verify:** `tests/test_api.py::TestUploadValidation::test_consent_is_required`
- **Honest limit:** this is an attestation, not proof. It puts the obligation where it belongs — on the person who has the relationship with the room — and creates a record of who accepted it.

### PR-2 — Everything on disk is encrypted, with a key per job

- **Implemented:** `app/core/crypto.py`. A fresh 256-bit data key per job encrypts that job's blobs; the data key is wrapped with the service key from `ENCRYPTION_KEY` and stored beside the job. The job id is the AEAD associated data, so a blob cannot be moved between jobs undetected.
- **Verify:** `tests/test_api.py::TestPrivacy::test_stored_result_is_unreadable_without_the_key`
- **Why per-job:** destroying one key makes exactly one meeting unreadable, including in any backup that already copied the ciphertext. A single service-wide key would make erasure a promise about file deletion rather than a cryptographic fact.

### PR-3 — Audio is deleted as soon as it has been used

Audio is the richest and most identifying artefact: it carries voice prints,
tone, background conversation and everyone in the room, including people not
mentioned in the transcript. It is kept only while it is being read.

- **Implemented:** `app/pipeline/orchestrator.py`. The original upload is shredded once a normalised copy exists; the normalised copy is shredded once ASR and diarization have both run. Plaintext audio exists only inside a temporary directory removed in a `finally` block.
- **Verify:** `tests/test_api.py::TestPrivacy::test_audio_is_deleted_once_it_has_been_transcribed`
- **Configurable:** `DELETE_AUDIO_AFTER_ASR=false` keeps audio for the retention window. The interface says which of the two is in force rather than showing a fixed reassurance.

### PR-4 — Identifiers are removed before storage and before the summariser

- **Implemented:** `app/pipeline/redaction.py`, applied in the aligning stage, before the transcript is written and before any text reaches the summariser. Covers email, phone, national id, payment card (Luhn-checked), IBAN, IP address and URL.
- **Verify:** `tests/test_privacy_and_grounding.py::TestRedactionCatches` and `TestRedactionLeavesMeetingContent`
- **Deliberately narrow:** over-redaction destroys the meeting. Budgets, dates, percentages and version numbers are the substance of a discussion and are left alone; a card number is only redacted if it passes a Luhn check. Redactions are counted and shown, and each altered line is marked, because quietly changing a quote is worse than not redacting it.

### PR-5 — A job id alone grants nothing

- **Implemented:** `app/deps.py`. Upload returns a bearer token; every read, export, rename and delete requires it. Only the SHA-256 of the token is stored, so the store cannot issue access to itself. Ids are 96 bits of randomness and are not sequential.
- **Verify:** `tests/test_api.py::TestPrivacy::test_a_job_id_alone_grants_nothing`
- **Known exception:** the progress stream accepts the token as a query parameter, because `EventSource` cannot set headers. Query strings appear in proxy logs, which is why job tokens are single-purpose and expire with the job.

### PR-6 — Data expires without anyone remembering to delete it

- **Implemented:** `app/core/retention.py`. A sweeper runs every `RETENTION_SWEEP_SECONDS` and on startup, so a service that was offline past a job's expiry still deletes on boot. Default window 24 hours; per-upload overrides are allowed within 1–720 hours.
- **Verify:** `tests/test_api.py::TestPrivacy::test_expired_jobs_are_swept`

### PR-7 — Erasure on request is immediate and final

- **Implemented:** `DELETE /v1/jobs/{id}`. Blobs are overwritten and unlinked, and the job's wrapped key is destroyed. A tombstone remains so the interface can say the recording was deleted rather than that it never existed, and subsequent reads return 410.
- **Verify:** `tests/test_api.py::TestPrivacy::test_deletion_removes_the_payload_and_the_key`
- **Honest limit:** overwriting a file is not a guarantee on journalling filesystems, SSDs with wear levelling, or snapshotted volumes. That is precisely why erasure destroys the key. Treat the overwrite as defence in depth, and the key destruction as the guarantee.

### PR-8 — Transcripts do not leave the machine unless that was configured

Sending a meeting to a hosted model is a disclosure to a third party. It is a
decision for the operator, taken deliberately, not a default.

- **Implemented:** `app/pipeline/summarizer.py`. With `ALLOW_EXTERNAL_LLM=false` (the default), a summariser endpoint that resolves outside loopback or a private network is refused at construction, so the service fails at startup rather than silently exfiltrating the first meeting. Local endpoints — Ollama, vLLM, on-prem — are always allowed.
- **Verify:** `tests/test_api.py::TestEgressGuard`
- The interface states which of the two is in force, from `/v1/capabilities`.

### PR-9 — Logs never contain what was said

Log files are the least protected place personal data can land: they are
tailed, shipped to aggregators and kept long past any retention window.

- **Implemented:** `app/core/logging.py` filters emails, phone numbers, IBANs and bearer tokens from every record; the pipeline logs counts and durations rather than content; `app/core/audit.py` refuses to write any field whose name suggests content.
- **Verify:** `tests/test_api.py::TestPrivacy::test_audit_records_actions_but_never_content`

### PR-10 — What the system did is checkable

- **Implemented:** `app/core/audit.py`. Every entry carries the SHA-256 of the entry before it, so removing or editing one breaks the chain. `GET /v1/privacy/audit/verify` checks it; `GET /v1/privacy/jobs/{id}/audit` returns the trail for one recording to whoever holds its token.
- **Verify:** `tests/test_api.py::TestPrivacy::test_audit_chain_verifies`
- **Honest limit:** the chain detects tampering; it does not prevent it. An operator with write access could rebuild the whole file. Detection is what a single-node service can offer without an external notary.

### PR-11 — The interface tells the truth about the run

Privacy claims that are hard-coded in an interface become false the moment
configuration changes.

- **Implemented:** the interface reads `/v1/capabilities` and `/v1/privacy/policy` and renders what is actually in force — including "encryption at rest is switched off in this deployment" when it is. Demo mode is labelled, because presenting a scripted sample as somebody's meeting would be a lie.

---

## 3. Rights, and how to exercise them

| Right | How |
|---|---|
| Access | `GET /v1/jobs/{id}/result` with the job token |
| Portability | `GET /v1/jobs/{id}/transcript.json` (also `md`, `txt`, `srt`, `vtt`) |
| Erasure | `DELETE /v1/jobs/{id}` — immediate, key destroyed |
| Rectification | `POST /v1/jobs/{id}/speakers` to correct speaker attribution |
| Transparency | `GET /v1/privacy/policy`, `GET /v1/privacy/jobs/{id}/audit` |
| Erasure by default | no action needed; the retention sweeper deletes everything within the window |

---

## 4. Regulatory mapping

Not legal advice — a map from the controls above to the obligations they exist
to satisfy, so a reviewer can find the relevant control quickly.

| Obligation | Saudi PDPL | GDPR | Control |
|---|---|---|---|
| Lawful basis, consent | Art. 5–6 | Art. 6, 7 | PR-1 |
| Purpose limitation | Art. 10 | Art. 5(1)(b) | Transcription and briefing only; no secondary use, no training on user data |
| Data minimisation | Art. 11 | Art. 5(1)(c) | PR-3, PR-4 |
| Storage limitation | Art. 18 | Art. 5(1)(e) | PR-6 |
| Security of processing | Art. 19 | Art. 32 | PR-2, PR-5, PR-9 |
| Right of access, portability | Art. 4, 22 | Art. 15, 20 | Section 3 |
| Right to erasure | Art. 4 | Art. 17 | PR-7 |
| Transfers and third parties | Art. 29 | Art. 28, 44 | PR-8 |
| Accountability, records | Art. 30 | Art. 5(2), 30 | PR-10 |

**Special categories.** Speech reveals more than its words: health, religion,
union membership and political opinion can all surface in an unguarded
meeting, and voice itself is biometric. The system does not perform speaker
identification against any enrolled voice database — diarization only
distinguishes voices *within one recording* and labels them `SPEAKER_00`,
`SPEAKER_01`. No voice print is stored, and names are attached only if a person
types them.

---

## 5. What this does not protect against

Stated plainly, because a privacy document that lists only strengths is
marketing.

- **A compromised host.** An attacker with root while the service is running can read the service key from the environment and decrypt anything on disk. Encryption at rest protects a stolen disk, not a live intrusion.
- **A malicious operator.** Whoever runs the service can change the configuration, disable redaction and read every transcript. The audit chain makes that visible after the fact; it does not prevent it.
- **The uploader.** Anyone can upload a recording of people who never agreed. PR-1 records who claimed consent; it cannot verify the claim.
- **Model providers, when enabled.** With `ALLOW_EXTERNAL_LLM=true`, the transcript is subject to that provider's retention and training policy, which is outside this system entirely.
- **Traffic analysis.** File sizes and timings are visible to anyone watching the network, even over TLS.

---

## 6. Before a real deployment

- [ ] Set `ENCRYPTION_KEY` from `python -m app.core.crypto`, hold it outside the repository, and back it up — losing it means losing every stored job.
- [ ] Terminate TLS in front of the service; it speaks plain HTTP.
- [ ] Keep `ALLOW_EXTERNAL_LLM=false` unless participants were told a third party would process the recording.
- [ ] Set `RETENTION_HOURS` to the shortest window the team can work with.
- [ ] Put `DATA_DIR` on an encrypted volume, and confirm backups of it respect the retention window — a nightly snapshot silently extends every retention period.
- [ ] Restrict `CORS_ORIGINS` to the interface's real origin.
- [ ] Decide who may read the audit log, and ship it somewhere append-only.
- [ ] Tell participants, in the meeting invitation, that recordings are transcribed and briefed automatically, and how long they are kept.
