"""Service container and job authorisation.

FastAPI dependency injection points. `ServicesDep` hands routes the shared
services attached at startup; `JobDep` looks up the job and enforces that the
caller holds its bearer token — the job id alone grants nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from .config import Settings
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


def get_services(request: Request) -> Services:
    return request.app.state.services


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
