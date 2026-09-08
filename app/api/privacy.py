"""Privacy routes.

These endpoints exist so the privacy claims are inspectable rather than
promised. The policy the service is actually running is readable, the audit
trail for a job is readable by whoever holds that job's token, and the hash
chain over the trail can be checked by anyone.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import JobDep, ServicesDep

router = APIRouter(prefix="/v1/privacy", tags=["privacy"])


@router.get("/policy")
async def policy(services: ServicesDep):
    """The live configuration, not a copy of the documentation."""
    settings = services.settings
    return {
        "consent_required": settings.require_consent,
        "encryption_at_rest": {
            "enabled": settings.encrypt_at_rest,
            "cipher": "AES-256-GCM",
            "key_scope": "one data key per job, wrapped by the service key",
        },
        "audio": {
            "deleted_after_transcription": settings.delete_audio_after_asr,
            "original_upload_deleted_after_normalisation": True,
        },
        "pii_redaction": {
            "enabled": settings.redact_pii,
            "applied_to": "transcript before storage, the brief, and anything sent to the summariser",
            "kinds": ["email", "phone", "national id", "card", "iban", "ip address", "url"],
        },
        "third_party_processing": {
            "external_llm_allowed": settings.allow_external_llm,
            "summariser_backend": settings.summarizer_backend,
            "note": (
                "With external calls off, the transcript never leaves this machine; a summariser "
                "endpoint outside the local network is refused."
            ),
        },
        "retention": {
            "default_hours": settings.retention_hours,
            "enforced_by": "background sweeper plus deletion on request",
            "sweep_interval_seconds": settings.retention_sweep_seconds,
        },
        "access": "Each job is readable only with the bearer token issued at upload.",
        "logging": "Application logs carry counts and durations. Transcript text is filtered out.",
    }


@router.get("/jobs/{job_id}/audit")
async def job_audit(job: JobDep, services: ServicesDep):
    """Everything the service did with this recording."""
    entries = services.audit.entries(job_id=job.id)
    return {
        "job_id": job.id,
        "entries": [
            {"time": e["ts"], "action": e["action"], "details": e.get("details", {})} for e in entries
        ],
        "explanation": "Actions only. What was said in the meeting is never written to the audit log.",
    }


@router.get("/audit/verify")
async def verify_chain(services: ServicesDep):
    """Each audit line carries the hash of the line before it. If an entry were
    removed or edited, this check fails."""
    intact, count = services.audit.verify()
    return {
        "chain_intact": intact,
        "entries_checked": count,
        "meaning": "ok" if intact else "The audit log has been altered since it was written.",
    }


@router.post("/sweep")
async def sweep_now(services: ServicesDep):
    """Run the retention sweep immediately instead of waiting for the timer."""
    purged = services.sweeper.sweep_once()
    return {"purged": purged, "message": f"{purged} job(s) past their retention window were deleted."}
