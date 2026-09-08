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

# Force the runtime into synchronous, demo-lite mode before importing the app.
os.environ.setdefault("SYNCHRONOUS_JOBS", "true")
os.environ.setdefault("DEMO_MODE_LITE", "true")
os.environ.setdefault("ASR_BACKEND", "mock")
os.environ.setdefault("DIARIZATION_BACKEND", "mock")
os.environ.setdefault("SUMMARIZER_BACKEND", "mock")
os.environ.setdefault("DATA_DIR", "/tmp/vocalyze")
os.environ.setdefault("AUDIT_LOG_ENABLED", "false")
os.environ.setdefault("RETENTION_SWEEP_SECONDS", "999999")
if not os.environ.get("ENCRYPTION_KEY"):
    from app.core.crypto import generate_service_key
    os.environ["ENCRYPTION_KEY"] = generate_service_key()

from app.main import app  # noqa: E402  — must import after env setup

# Vercel expects a module-level `app`.
__all__ = ["app"]
