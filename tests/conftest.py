"""Test fixtures.

The suite runs against the scripted backends: no weights are downloaded and
no network is touched, so it finishes in seconds and gives the same answer on
a laptop and in CI. Each test that needs the service gets its own DATA_DIR and
its own encryption key, so nothing leaks between tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from app.config import Settings
    from app.core.crypto import generate_service_key

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENCRYPTION_KEY", generate_service_key())
    monkeypatch.setenv("RETENTION_SWEEP_SECONDS", "3600")
    return Settings()


@pytest.fixture
def store(settings):
    from app.core.crypto import Sealer
    from app.core.store import JobStore

    return JobStore(settings, Sealer.from_settings(settings.encryption_key, settings.encrypt_at_rest))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient with the lifespan run, so the worker and sweeper exist."""
    from fastapi.testclient import TestClient

    from app.core.crypto import generate_service_key

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("ENCRYPTION_KEY", generate_service_key())
    monkeypatch.setenv("RETENTION_SWEEP_SECONDS", "3600")
    monkeypatch.setenv("DEMO_MODE_LITE", "true")  # no ffmpeg needed for the mock backends

    import app.config as config

    config.get_settings.cache_clear()
    import app.main as main

    importlib_reload(main)
    with TestClient(main.app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


def importlib_reload(module):
    import importlib

    return importlib.reload(module)


@pytest.fixture
def wav_bytes():
    """A tiny, valid RIFF header. DEMO_MODE_LITE skips ffmpeg, so the bytes
    only have to survive upload, encryption and storage."""
    return b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 32
