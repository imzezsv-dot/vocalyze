"""Job routes — the upload flow and everything that follows it.

Upload is deliberately a two-step exchange rather than one long request: the
file is validated and accepted, and processing happens behind a job id. A
90-minute meeting takes minutes to transcribe, and a browser holding an open
POST for that long fails on the first proxy timeout or dropped connection.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from ..core.errors import (
    ConsentRequired,
    EmptyUpload,
    FileTooLarge,
    ResultNotReady,
    UnsupportedFormat,
)
from ..core.logging import get_logger
from ..core.store import _shred
from ..deps import JobDep, ServicesDep
from ..pipeline.audio import extension_of
from ..pipeline.orchestrator import ORIGINAL_BLOB, RESULT_BLOB
from ..schemas import (
    JobResult,
    JobState,
    JobSummary,
    MeetingBrief,
    QualityReport,
    Transcript,
    UploadAccepted,
)

log = get_logger("vocalyze.api")
router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

CHUNK = 1024 * 1024


@router.post("", response_model=UploadAccepted, status_code=202)
async def create_job(
    request: Request,
    services: ServicesDep,
    file: Annotated[UploadFile, File(description="Meeting recording")],
    consent: Annotated[bool, Form(description="Participants agreed to this recording being processed")] = False,
    redact_pii: Annotated[bool | None, Form()] = None,
    delete_audio_after_asr: Annotated[bool | None, Form()] = None,
    retention_hours: Annotated[int | None, Form()] = None,
):
    settings = services.settings

    if settings.require_consent and not consent:
        raise ConsentRequired()
    if not file or not file.filename:
        raise EmptyUpload()

    extension = extension_of(file.filename)
    if extension not in settings.allowed_extensions:
        raise UnsupportedFormat(settings.allowed_extensions)

    # Stream to a scratch file so a 2 GB upload never sits in memory, and so the
    # size limit is enforced while bytes arrive rather than after.
    settings.ensure_dirs()
    scratch = settings.uploads_dir / f"incoming-{id(request):x}.part"
    size = 0
    try:
        with scratch.open("wb") as sink:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise FileTooLarge(settings.max_upload_mb)
                sink.write(chunk)
        if size == 0:
            raise EmptyUpload()

        privacy = {
            "consent": bool(consent),
            "redact_pii": redact_pii,
            "delete_audio_after_asr": delete_audio_after_asr,
            "retention_hours": retention_hours,
            "consent_recorded_at": _now(),
        }
        record, token, data_key = services.store.create(
            filename=_safe_name(file.filename), size_bytes=size, privacy=privacy
        )
        services.store.write_blob(record.id, ORIGINAL_BLOB, scratch.read_bytes(), data_key)
    finally:
        if scratch.exists():
            _shred(scratch)

    services.audit.record(
        "job.created",
        job_id=record.id,
        bytes=size,
        extension=extension,
        consent=bool(consent),
        retention_hours=privacy["retention_hours"] or settings.retention_hours,
    )

    if settings.synchronous_jobs:
        # Serverless / no-worker mode: run the pipeline inline so a single
        # HTTP round-trip returns the finished result. The frontend notices
        # `result` in the response and skips polling.
        import anyio

        await anyio.to_thread.run_sync(services.orchestrator.run, record.id)
        completed = services.store.get(record.id)
        payload = {
            "job_id": completed.id,
            "access_token": token,
            "state": completed.state.value,
            "expires_at": completed.expires_at.isoformat() if completed.expires_at else None,
            "poll": f"/v1/jobs/{completed.id}",
        }
        if completed.state is JobState.completed:
            data_key = services.store.data_key(completed)
            body = services.store.read_json(completed.id, RESULT_BLOB, data_key)
            payload["result"] = {
                "job": completed.summary().model_dump(mode="json"),
                "transcript": body["transcript"],
                "brief": body["brief"],
                "quality": body["quality"],
                "privacy": {
                    "encrypted_at_rest": settings.encrypt_at_rest,
                    "audio_retained": services.store.blob_exists(completed.id, "audio.enc"),
                    "expires_at": payload["expires_at"],
                    "models": body.get("models", {}),
                },
            }
        return JSONResponse(payload, status_code=202)

    await services.queue.submit(record.id)

    summary = record.summary()
    return UploadAccepted(
        job_id=record.id,
        access_token=token,
        state=summary.state,
        expires_at=summary.expires_at,
        poll=f"/v1/jobs/{record.id}",
    )


@router.get("/{job_id}", response_model=JobSummary)
async def get_job(job: JobDep):
    return job.summary()


@router.get("/{job_id}/events")
async def stream_job(job: JobDep, services: ServicesDep, token: Annotated[str | None, Query()] = None):
    """Server-sent progress. Polling the store is enough here — stages change
    every few seconds, not every few milliseconds, and it keeps the worker
    free of any coupling to connected clients."""

    async def generator():
        last = None
        while True:
            try:
                current = services.store.get(job.id).summary()
            except KeyError:
                break
            payload = current.model_dump(mode="json")
            if payload != last:
                yield f"data: {json.dumps(payload)}\n\n"
                last = payload
            if current.state in {JobState.completed, JobState.failed, JobState.purged}:
                break
            await asyncio.sleep(0.7)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{job_id}/result", response_model=JobResult)
async def get_result(job: JobDep, services: ServicesDep):
    summary = job.summary()
    if summary.state is not JobState.completed:
        raise ResultNotReady(summary.state.value)

    payload = _load_result(services, job)
    services.audit.record("result.read", job_id=job.id)
    return JobResult(
        job=summary,
        transcript=Transcript(**payload["transcript"]),
        brief=MeetingBrief(**payload["brief"]),
        quality=QualityReport(**payload["quality"]),
        privacy={
            **job.privacy,
            "audio_retained": services.store.blob_exists(job.id, "audio.enc"),
            "encrypted_at_rest": services.settings.encrypt_at_rest,
            "expires_at": summary.expires_at.isoformat() if summary.expires_at else None,
            "models": payload.get("models", {}),
        },
    )


@router.post("/{job_id}/speakers", response_model=JobResult)
async def rename_speakers(job: JobDep, services: ServicesDep, mapping: dict[str, str]):
    """Attach real names to SPEAKER_00 and friends.

    Names are stored inside the encrypted result and inherit its retention, so
    naming a colleague does not create a second, longer-lived copy of who
    attended the meeting.
    """
    summary = job.summary()
    if summary.state is not JobState.completed:
        raise ResultNotReady(summary.state.value)

    payload = _load_result(services, job)
    transcript = payload["transcript"]
    for utterance in transcript["utterances"]:
        utterance["speaker"] = mapping.get(utterance["speaker"], utterance["speaker"])
    transcript["speakers"] = [mapping.get(name, name) for name in transcript["speakers"]]

    data_key = services.store.data_key(job)
    payload["transcript"] = transcript
    services.store.write_json(job.id, RESULT_BLOB, payload, data_key)
    services.audit.record("speakers.renamed", job_id=job.id, count=len(mapping))
    return await get_result(job, services)


@router.get("/{job_id}/transcript.{fmt}")
async def export_transcript(job: JobDep, services: ServicesDep, fmt: str):
    summary = job.summary()
    if summary.state is not JobState.completed:
        raise ResultNotReady(summary.state.value)
    payload = _load_result(services, job)
    transcript = Transcript(**payload["transcript"])
    brief = MeetingBrief(**payload["brief"])
    services.audit.record("transcript.exported", job_id=job.id, format=fmt)

    if fmt == "json":
        return JSONResponse(payload)
    if fmt == "srt":
        return PlainTextResponse(_srt(transcript), media_type="text/plain; charset=utf-8")
    if fmt == "vtt":
        return PlainTextResponse(_vtt(transcript), media_type="text/vtt; charset=utf-8")
    if fmt == "md":
        return PlainTextResponse(_markdown(transcript, brief), media_type="text/markdown; charset=utf-8")
    return PlainTextResponse(_plain(transcript), media_type="text/plain; charset=utf-8")


@router.delete("/{job_id}", status_code=200)
async def delete_job(job: JobDep, services: ServicesDep):
    """Erasure on request. Blobs are shredded and the job's key destroyed, so
    any ciphertext that outlives this call in a backup stays unreadable."""
    services.store.purge(job.id)
    services.audit.record("job.deleted", job_id=job.id, reason="user_request")
    return {
        "deleted": True,
        "job_id": job.id,
        "message": "Audio, transcript and brief are gone. The audit entry recording the deletion remains.",
    }


# ---------------------------------------------------------------------------
def _load_result(services: ServicesDep, job) -> dict:
    data_key = services.store.data_key(job)
    return services.store.read_json(job.id, RESULT_BLOB, data_key)


def _safe_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in " ._-()[]").strip()
    return (cleaned or "recording")[:120]


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _timestamp(seconds: float, comma: bool = True) -> str:
    hours, rest = divmod(max(0.0, seconds), 3600)
    minutes, secs = divmod(rest, 60)
    marker = "," if comma else "."
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d}{marker}{int((secs % 1) * 1000):03d}"


def _srt(transcript: Transcript) -> str:
    blocks = []
    for index, utterance in enumerate(transcript.utterances, start=1):
        blocks.append(
            f"{index}\n{_timestamp(utterance.start)} --> {_timestamp(utterance.end)}\n"
            f"{utterance.speaker}: {utterance.text}\n"
        )
    return "\n".join(blocks)


def _vtt(transcript: Transcript) -> str:
    blocks = ["WEBVTT\n"]
    for utterance in transcript.utterances:
        blocks.append(
            f"{_timestamp(utterance.start, comma=False)} --> {_timestamp(utterance.end, comma=False)}\n"
            f"<v {utterance.speaker}>{utterance.text}\n"
        )
    return "\n".join(blocks)


def _plain(transcript: Transcript) -> str:
    lines = []
    for utterance in transcript.utterances:
        stamp = f"{int(utterance.start // 60):02d}:{int(utterance.start % 60):02d}"
        lines.append(f"[{stamp}] {utterance.speaker}: {utterance.text}")
    return "\n".join(lines)


def _markdown(transcript: Transcript, brief: MeetingBrief) -> str:
    lines = ["# Meeting brief", "", brief.summary, ""]
    if brief.decisions:
        lines += ["## Decisions", ""] + [f"- {item.text}" for item in brief.decisions] + [""]
    if brief.action_items:
        lines += ["## Action items", ""]
        for item in brief.action_items:
            owner = f" — {item.owner}" if item.owner else ""
            due = f" ({item.due})" if item.due else ""
            lines.append(f"- {item.text}{owner}{due}")
        lines.append("")
    if brief.key_points:
        lines += ["## Key points", ""] + [f"- {item.text}" for item in brief.key_points] + [""]
    lines += ["## Transcript", ""]
    for utterance in transcript.utterances:
        stamp = f"{int(utterance.start // 60):02d}:{int(utterance.start % 60):02d}"
        lines.append(f"**{utterance.speaker}** `{stamp}` {utterance.text}")
        lines.append("")
    return "\n".join(lines)
