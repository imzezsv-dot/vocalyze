"""Service-level routes."""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import ServicesDep

router = APIRouter(prefix="/v1", tags=["system"])


@router.get("/health")
async def health(services: ServicesDep):
    settings = services.settings
    writable = True
    try:
        probe = settings.data_dir / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError:
        writable = False

    return {
        "status": "ok" if writable else "degraded",
        "version": settings.version,
        "queue_depth": services.queue.depth,
        "storage_writable": writable,
    }


@router.get("/capabilities")
async def capabilities(services: ServicesDep):
    """What this deployment can do and how it is configured to behave.

    The interface reads this on load: a demo build and a full build should not
    look identical, because telling someone a scripted sample is their meeting
    would be a lie.
    """
    return services.settings.capabilities()
