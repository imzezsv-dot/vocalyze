"""The pipeline orchestrator — Whisper, pyannote and the LLM as one system.

Stage order and what each stage is responsible for:

    normalizing   decode to 16 kHz mono so both models see identical audio
    transcribing  ASR  -> words with timings
    diarizing     diarization -> speaker turns
    aligning      merge the two, redact identifiers
    summarizing   generate the brief, then verify every claim against the
                  transcript before anyone sees it

Privacy is enforced *between* stages rather than at the edges, because that is
where the data actually moves: plaintext audio exists only inside a stage and
is shredded in a `finally`, the original upload is destroyed as soon as a
normalised copy exists, and the normalised copy is destroyed as soon as both
models have read it. If the process dies mid-run, the retention sweeper still
removes whatever survived.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..core.audit import AuditLog
from ..core.errors import PipelineError
from ..core.logging import get_logger
from ..core.store import JobRecord, JobStore, _shred
from ..schemas import JobState, MeetingBrief, QualityReport, Transcript
from . import audio as audio_tools
from . import registry
from .align_core import UNKNOWN_SPEAKER
from .alignment import build_transcript, to_dicts
from .grounding_core import GroundingOptions, drop_ungrounded, verify_brief
from .redaction import redact_utterances

log = get_logger("vocalyze.pipeline")

ORIGINAL_BLOB = "original.enc"
AUDIO_BLOB = "audio.enc"
RESULT_BLOB = "result.enc"

WORKDIR_PREFIX = "vocalyze-work-"
"""Scratch directories the pipeline decodes audio into.

Named with a prefix rather than an opaque temp name so the retention sweeper
can recognise one abandoned by a killed process. What lives in here is
*plaintext* audio — the only point in the system where that is true on disk —
so a crash between `mkdtemp` and the `finally` must not leave it lying around
until someone notices.
"""


class Orchestrator:
    def __init__(self, settings: Settings, store: JobStore, audit: AuditLog):
        self.settings = settings
        self.store = store
        self.audit = audit

    # ------------------------------------------------------------------
    def run(self, job_id: str) -> None:
        """Process one job. Never raises: failure is recorded on the job."""
        started = time.monotonic()
        record = self.store.get(job_id)
        data_key = self.store.data_key(record)
        privacy = effective_privacy(self.settings, record)
        workdir = Path(tempfile.mkdtemp(prefix=f"{WORKDIR_PREFIX}{job_id}-", dir=self.settings.data_dir))

        try:
            wav_path = self._normalise(record, data_key, workdir)
            asr_result = self._transcribe(record, wav_path)
            diarization = self._diarize(record, wav_path)

            if privacy["delete_audio_after_asr"]:
                self.store.delete_blob(job_id, AUDIO_BLOB)
                self.audit.record("audio.deleted", job_id=job_id, reason="delete_audio_after_asr")

            transcript, quality = self._align(record, asr_result, diarization, privacy)
            brief, quality = self._summarize(record, transcript, quality)

            self.store.write_json(
                job_id,
                RESULT_BLOB,
                {
                    "transcript": transcript.model_dump(mode="json"),
                    "brief": brief.model_dump(mode="json"),
                    "quality": quality.model_dump(mode="json"),
                    "models": {
                        "asr": {"backend": asr_result.backend, "model": asr_result.model},
                        "diarization": {"backend": diarization.backend, "model": diarization.model},
                        "summarizer": {"backend": brief.backend, "model": brief.model},
                    },
                },
                data_key,
            )
            self.store.update(job_id, duration_seconds=round(transcript.duration, 2))
            self.store.set_state(job_id, JobState.completed)
            self.audit.record(
                "job.completed",
                job_id=job_id,
                seconds=round(time.monotonic() - started, 1),
                speakers=len(transcript.speakers),
                utterances=len(transcript.utterances),
            )
            log.info("job %s completed in %.1fs", job_id, time.monotonic() - started)

        except PipelineError as exc:
            self._fail(job_id, exc.stage, str(exc))
        except Exception as exc:  # noqa: BLE001 - anything unexpected still fails cleanly
            log.exception("job %s failed", job_id)
            self._fail(job_id, "pipeline", f"{exc.__class__.__name__}: {exc}")
        finally:
            # `_shred` recurses into directories and swallows OSError, so the
            # cleanup cannot itself raise out of `run()` — a failure here would
            # otherwise escape past `_fail` and reach the worker as a crash on a
            # job that had already been recorded as failed.
            _shred(workdir)

    # ------------------------------------------------------------------
    def _fail(self, job_id: str, stage: str, message: str) -> None:
        try:
            self.store.mark_stage(job_id, JobState(stage), "failed", detail=message[:300])
        except (ValueError, KeyError):
            pass
        self.store.set_state(job_id, JobState.failed, error=message[:500])
        self.audit.record("job.failed", job_id=job_id, stage=stage)

    def _stage(self, record: JobRecord, stage: JobState):
        self.store.set_state(record.id, stage)
        self.store.mark_stage(record.id, stage, "running")

    # ------------------------------------------------------------------
    def _normalise(self, record: JobRecord, data_key: bytes, workdir: Path) -> Path:
        self._stage(record, JobState.normalizing)
        try:
            raw = self.store.read_blob(record.id, ORIGINAL_BLOB, data_key)
        except FileNotFoundError as exc:
            raise PipelineError("normalizing", "The uploaded audio is no longer on disk.") from exc

        source = workdir / "source"
        source.write_bytes(raw)
        del raw

        # Serverless / demo-lite: skip ffmpeg entirely and pretend a normalised
        # 16 kHz mono copy exists. The mock backends do not read the file, so
        # nothing downstream cares — this lets the pipeline run on hosts where
        # ffmpeg is not installed (e.g. Vercel Python runtime).
        if self.settings.demo_mode_lite:
            wav = workdir / "audio.wav"
            wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
            self.store.write_blob(record.id, AUDIO_BLOB, wav.read_bytes(), data_key)
            self.store.delete_blob(record.id, ORIGINAL_BLOB)
            _shred(source)
            self.store.update(record.id, duration_seconds=112.5)
            self.store.mark_stage(
                record.id, JobState.normalizing, "done",
                detail="demo-lite: audio normalisation skipped",
            )
            return wav

        try:
            info = audio_tools.probe(source, self.settings.ffmpeg_bin)
            if info.duration > self.settings.max_duration_minutes * 60:
                raise PipelineError(
                    "normalizing",
                    f"The recording is {info.duration / 60:.0f} minutes, over the "
                    f"{self.settings.max_duration_minutes} minute limit.",
                )
            wav = audio_tools.normalise(
                source,
                workdir / "audio.wav",
                sample_rate=self.settings.target_sample_rate,
                channels=self.settings.target_channels,
                ffmpeg_bin=self.settings.ffmpeg_bin,
            )
        except audio_tools.AudioError as exc:
            raise PipelineError("normalizing", str(exc)) from exc

        self.store.write_blob(record.id, AUDIO_BLOB, wav.read_bytes(), data_key)
        # The uploaded file may be a video or a lossless master; once a
        # normalised copy exists there is no reason to keep the original.
        self.store.delete_blob(record.id, ORIGINAL_BLOB)
        _shred(source)

        self.store.update(record.id, duration_seconds=round(info.duration, 2))
        self.store.mark_stage(
            record.id,
            JobState.normalizing,
            "done",
            detail=f"{info.duration:.0f}s, {info.sample_rate} Hz {info.codec} to "
            f"{self.settings.target_sample_rate} Hz mono",
        )
        self.audit.record("audio.normalised", job_id=record.id, seconds=round(info.duration, 1))
        return wav

    def _transcribe(self, record: JobRecord, wav: Path):
        self._stage(record, JobState.transcribing)
        backend = registry.get_asr(self.settings)
        try:
            result = backend.transcribe(wav)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError("transcribing", f"Speech recognition failed: {exc}") from exc
        if not result.segments:
            raise PipelineError("transcribing", "No speech was found in this recording.")
        self.store.mark_stage(
            record.id,
            JobState.transcribing,
            "done",
            detail=f"{len(result.segments)} segments, {result.backend}:{result.model}",
        )
        return result

    def _diarize(self, record: JobRecord, wav: Path):
        self._stage(record, JobState.diarizing)
        backend = registry.get_diarizer(self.settings)
        try:
            result = backend.diarize(wav)
        except Exception as exc:  # noqa: BLE001
            # A failed diarizer costs speaker labels, not the transcript. Carry
            # on with an empty result: every utterance becomes UNKNOWN and the
            # interface says so, which beats losing the meeting entirely.
            from ..schemas import DiarizationResult

            log.warning("diarization failed for %s: %s", record.id, exc)
            self.store.mark_stage(record.id, JobState.diarizing, "failed", detail=str(exc)[:200])
            return DiarizationResult(turns=[], num_speakers=0, backend="unavailable", model="none")
        self.store.mark_stage(
            record.id,
            JobState.diarizing,
            "done",
            detail=f"{result.num_speakers} speakers, {len(result.turns)} turns",
        )
        return result

    def _align(self, record: JobRecord, asr_result, diarization, privacy: dict) -> tuple[Transcript, QualityReport]:
        self._stage(record, JobState.aligning)
        transcript, stats = build_transcript(asr_result, diarization)

        redactions = 0
        if privacy["redact_pii"]:
            utterance_dicts = to_dicts(transcript)
            report = redact_utterances(utterance_dicts)
            redactions = report.count
            if redactions:
                from ..schemas import Utterance

                transcript.utterances = [Utterance(**item) for item in utterance_dicts]
                self.audit.record("transcript.redacted", job_id=record.id, count=redactions, kinds=report.by_kind)

        confidences = [u.confidence for u in transcript.utterances if u.confidence is not None]
        quality = QualityReport(
            asr_mean_confidence=round(sum(confidences) / len(confidences), 3) if confidences else None,
            low_confidence_utterances=sum(1 for c in confidences if c < 0.6),
            speaker_count=len([s for s in transcript.speakers if s != UNKNOWN_SPEAKER]),
            overlapped_utterances=stats.overlapped_utterances,
            unassigned_speech_seconds=stats.unassigned_speech_seconds,
            redactions=redactions,
        )
        self.store.mark_stage(
            record.id,
            JobState.aligning,
            "done",
            detail=f"{len(transcript.utterances)} utterances, {quality.speaker_count} speakers"
            + (f", {redactions} redacted" if redactions else ""),
        )
        return transcript, quality

    def _summarize(self, record: JobRecord, transcript: Transcript, quality: QualityReport):
        self._stage(record, JobState.summarizing)
        backend = registry.get_summarizer(self.settings)
        utterances = to_dicts(transcript)
        try:
            raw = backend.summarize(utterances, transcript.language)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError("summarizing", f"The brief could not be generated: {exc}") from exc

        verified, stats = verify_brief(
            dict(raw), utterances, GroundingOptions(min_overlap=self.settings.min_evidence_overlap)
        )
        dropped = 0
        if self.settings.drop_ungrounded_claims:
            verified, dropped = drop_ungrounded(verified)

        brief = MeetingBrief(
            summary=verified.get("summary", ""),
            summary_evidence=verified.get("summary_evidence", {}),
            key_points=verified.get("key_points", []),
            decisions=verified.get("decisions", []),
            action_items=verified.get("action_items", []),
            dropped_claims=dropped,
            backend=getattr(backend, "name", "unknown"),
            model=self.settings.llm_model if getattr(backend, "name", "") == "llm" else "rule-based",
        )
        quality.grounded_claims = stats["grounded"]
        quality.dropped_claims = dropped

        detail = f"{stats['grounded']} claims verified"
        if dropped:
            detail += f", {dropped} dropped as unsupported"
        self.store.mark_stage(record.id, JobState.summarizing, "done", detail=detail)
        if dropped:
            self.audit.record("brief.claims_dropped", job_id=record.id, count=dropped)
        return brief, quality


def effective_privacy(settings: Settings, record: JobRecord) -> dict:
    """Per-job choices override the deployment default, never the other way.

    Resolved rather than raw: a stored `null` means "no per-job choice was
    made", which is an implementation detail. Callers — the pipeline and the
    result endpoint alike — need the value actually in force.
    """
    chosen = record.privacy or {}
    return {
        "consent": bool(chosen.get("consent", False)),
        "consent_recorded_at": chosen.get("consent_recorded_at"),
        "redact_pii": _pick(chosen.get("redact_pii"), settings.redact_pii),
        "delete_audio_after_asr": _pick(chosen.get("delete_audio_after_asr"), settings.delete_audio_after_asr),
        "retention_hours": int(chosen.get("retention_hours") or settings.retention_hours),
    }


def _pick(value, default):
    return default if value is None else bool(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
