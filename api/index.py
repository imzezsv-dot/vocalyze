"""Vercel Python runtime entry point.

Vercel discovers this file, imports `app`, and serves it as an ASGI
serverless function. Every request is a fresh invocation with an
ephemeral `/tmp` — nothing persists between calls — so this deployment
runs in synchronous, demo-lite mode:

  - `SYNCHRONOUS_JOBS=true`  → upload runs the pipeline inline and
    returns the completed result in the same HTTP round-trip. No queue,
    no polling, no worker.
  - `DEMO_MODE_LITE=true`    → skip ffmpeg (not available on Vercel).
    The mock backends do not read the audio anyway, so the pipeline
    still produces a full transcript and brief.
  - `ENCRYPTION_KEY`         → set from `python -m app.core.crypto` in
    Vercel's Environment Variables; a random ephemeral one is generated
    if omitted, which is fine for a preview.

Static assets (HTML/CSS/JS/fonts) are served by Vercel's edge from
`public/`, cached — vercel.json handles the routing.
"""

from __future__ import annotations

import os
import sys
import traceback

# The repository root, so `import app` resolves however the bundle is laid out.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force the runtime into synchronous, demo-lite mode before importing the app.
os.environ.setdefault("SYNCHRONOUS_JOBS", "true")
os.environ.setdefault("DEMO_MODE_LITE", "true")
os.environ.setdefault("ASR_BACKEND", "mock")
os.environ.setdefault("DIARIZATION_BACKEND", "mock")
os.environ.setdefault("SUMMARIZER_BACKEND", "mock")
os.environ.setdefault("DATA_DIR", "/tmp/vocalyze")
os.environ.setdefault("AUDIT_LOG_ENABLED", "false")
os.environ.setdefault("RETENTION_SWEEP_SECONDS", "999999")
try:
    if not os.environ.get("ENCRYPTION_KEY"):
        from app.core.crypto import generate_service_key

        os.environ["ENCRYPTION_KEY"] = generate_service_key()

    from app.main import app  # noqa: E402  — must import after env setup

except Exception:  # noqa: BLE001
    # If the service cannot be imported here, every /v1/ route answers 500 with
    # no body, and the interface can only report "no API is reachable" — which
    # is true and useless. A serverless bundle fails in ways a laptop does not:
    # a missing package, a file the builder left out, a Python version older
    # than the syntax in these modules. So serve the reason instead of nothing.
    _REASON = traceback.format_exc()
    print(_REASON, file=sys.stderr)

    _BODY = {
        "error": "service_unavailable",
        "detail": "The API failed to start in this deployment.",
        "fix": "Read `cause` below — it is the import error from the serverless bundle.",
        "python": sys.version,
        "cwd": os.getcwd(),
        "sys_path": sys.path[:6],
        "cause": _REASON.strip().splitlines()[-8:],
    }

    async def app(scope, receive, send):  # type: ignore[misc]
        """Minimal ASGI app: answers every request with the reason."""
        if scope["type"] != "http":
            return
        import json

        payload = json.dumps(_BODY, indent=2).encode()
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"cache-control", b"no-store"),
                (b"access-control-allow-origin", b"*"),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


# Vercel expects a module-level `app`.
__all__ = ["app"]
