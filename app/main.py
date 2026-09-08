"""Vocalyze — integration service.

Brings the three model components together behind one API and one interface:

    POST   /v1/jobs                     upload a recording
    GET    /v1/jobs/{id}                progress
    GET    /v1/jobs/{id}/events         progress as a stream
    GET    /v1/jobs/{id}/result         transcript + verified brief
    GET    /v1/jobs/{id}/transcript.md  export (md | txt | srt | vtt | json)
    POST   /v1/jobs/{id}/speakers       attach real names to speaker labels
    DELETE /v1/jobs/{id}                erase everything for this recording
    GET    /v1/privacy/policy           the privacy settings actually in force
    GET    /v1/privacy/audit/verify     check the audit chain

Run it:  uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import jobs, privacy, system
from .config import get_settings
from .core.audit import AuditLog
from .core.crypto import Sealer
from .core.logging import get_logger, setup_logging
from .core.retention import RetentionSweeper
from .core.store import JobStore
from .deps import Services
from .pipeline import registry
from .pipeline.orchestrator import Orchestrator
from .worker import JobQueue

log = get_logger("vocalyze")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging()
    settings.ensure_dirs()

    sealer = Sealer.from_settings(settings.encryption_key, settings.encrypt_at_rest)
    store = JobStore(settings, sealer)
    audit = AuditLog(settings.audit_path, settings.audit_log_enabled)
    orchestrator = Orchestrator(settings, store, audit)
    queue = JobQueue(orchestrator, settings.worker_concurrency)
    sweeper = RetentionSweeper(store, audit, settings.retention_sweep_seconds)

    app.state.services = Services(
        settings=settings, store=store, audit=audit, queue=queue, sweeper=sweeper,
        orchestrator=orchestrator,
    )

    if not settings.synchronous_jobs:
        queue.start()
        sweeper.start()
    registry.preload()
    audit.record("service.started", version=settings.version, asr=settings.asr_backend)
    log.info(
        "%s %s ready — asr=%s diarization=%s summariser=%s, retention %dh",
        settings.app_name,
        settings.version,
        settings.asr_backend,
        settings.diarization_backend,
        settings.summarizer_backend,
        settings.retention_hours,
    )
    try:
        yield
    finally:
        if not settings.synchronous_jobs:
            await queue.stop()
            await sweeper.stop()
        audit.record("service.stopped")


settings = get_settings()

app = FastAPI(
    title="Vocalyze",
    version=settings.version,
    description="Meeting recordings become an attributed transcript and a brief where every claim cites its source.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Job-Token"],
)

app.include_router(jobs.router)
app.include_router(privacy.router)
app.include_router(system.router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """One error shape everywhere, so the interface never has to guess."""
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": _slug(exc.status_code), "detail": str(detail), "fix": None},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", [])[1:]) or "request"
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "detail": f"{field}: {first.get('msg', 'is not valid')}",
            "fix": "Check the field against /docs and send it again.",
        },
    )


def _slug(status_code: int) -> str:
    return {400: "bad_request", 401: "not_authorized", 404: "not_found", 413: "file_too_large"}.get(
        status_code, f"http_{status_code}"
    )


# The interface is served by the same process as the API: one command to run,
# same origin, so the browser needs no CORS exception for the normal case.
web_dir = settings.web_dir
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(web_dir / "index.html")
