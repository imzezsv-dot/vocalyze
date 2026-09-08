"""Service container and job authorisation.

FastAPI dependency injection points. `ServicesDep` hands routes the shared
services attached at startup; `JobDep` looks up the job and enforces that the
caller holds its bearer token — the job id alone grants nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from .config import Settings, get_settings
from .core.audit import AuditLog
from .core.errors import JobNotFound, JobPurged, NotAuthorized
from .core.store import JobRecord, JobStore
from .schemas import JobState


@dataclass
class Services:
    settings: Settings
    store: JobStore
    audit: AuditLog
    queue: "object"  # JobQueue; kept loose to avoid circular imports
    sweeper: "object"  # RetentionSweeper
    orchestrator: "object" = None  # only used in synchronous_jobs mode


def build_services(app, *, start_background: bool = True) -> Services:
    """Assemble the service container and attach it to the app.

    Called from the lifespan handler, and again lazily by `get_services` if
    that never ran.
    """
    # Imported here rather than at module scope: worker and orchestrator both
    # reach back into this package, and the lazy path is not on the hot path.
    from .core.crypto import Sealer
    from .core.logging import setup_logging
    from .core.retention import RetentionSweeper
    from .pipeline.orchestrator import Orchestrator
    from .worker import JobQueue

    settings = get_settings()
    setup_logging()
    settings.ensure_dirs()

    sealer = Sealer.from_settings(settings.encryption_key, settings.encrypt_at_rest)
    store = JobStore(settings, sealer)
    audit = AuditLog(settings.audit_path, settings.audit_log_enabled)
    orchestrator = Orchestrator(settings, store, audit)

    services = Services(
        settings=settings,
        store=store,
        audit=audit,
        queue=JobQueue(orchestrator, settings.worker_concurrency),
        sweeper=RetentionSweeper(store, audit, settings.retention_sweep_seconds),
        orchestrator=orchestrator,
    )

    if start_background and not settings.synchronous_jobs:
        services.queue.start()
        services.sweeper.start()

    app.state.services = services
    return services


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:
        # Not every ASGI host emits lifespan events — Vercel's Python runtime
        # does not — so startup may never have run. Building on first use
        # beats answering 500 to every request on those hosts.
        services = build_services(request.app)
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def _extract_token(
    authorization: str | None,
    x_job_token: str | None,
    query_token: str | None,
) -> str:
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    if x_job_token:
        return x_job_token.strip()
    if query_token:
        return query_token.strip()
    return ""


def get_job(
    job_id: str,
    services: ServicesDep,
    authorization: Annotated[str | None, Header()] = None,
    x_job_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
    token: Annotated[str | None, Query()] = None,
) -> JobRecord:
    presented = _extract_token(authorization, x_job_token, token)
    try:
        services.store.get(job_id)
    except KeyError:
        raise JobNotFound() from None

    record = services.store.authorise(job_id, presented)
    if record is None:
        raise NotAuthorized()
    if record.state is JobState.purged:
        raise JobPurged()
    return record


JobDep = Annotated[JobRecord, Depends(get_job)]
