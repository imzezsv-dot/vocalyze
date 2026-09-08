"""Runtime configuration for the Vocalyze integration service.

Every knob is an environment variable so the same image can run in
demo mode on a laptop and in strict local-only mode on a lab machine.
Read once at import; call `get_settings()` everywhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # ---- service ----
    app_name: str = "Vocalyze"
    version: str = "1.0.0"
    env: str = field(default_factory=lambda: os.getenv("VOCALYZE_ENV", "development"))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    cors_origins: list[str] = field(
        default_factory=lambda: _list("CORS_ORIGINS", ["http://127.0.0.1:8000", "http://localhost:8000"])
    )

    # ---- storage ----
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./var/vocalyze")).resolve())

    # ---- upload limits ----
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 200))
    max_duration_minutes: int = field(default_factory=lambda: _int("MAX_DURATION_MINUTES", 120))
    allowed_extensions: list[str] = field(
        default_factory=lambda: _list("ALLOWED_EXTENSIONS", ["wav", "mp3", "m4a", "flac", "ogg", "opus", "webm", "mp4"])
    )

    # ---- pipeline backends: "mock" | "whisper" | "pyannote" | "openai" | "local" ----
    asr_backend: str = field(default_factory=lambda: os.getenv("ASR_BACKEND", "mock"))
    diarization_backend: str = field(default_factory=lambda: os.getenv("DIARIZATION_BACKEND", "mock"))
    summarizer_backend: str = field(default_factory=lambda: os.getenv("SUMMARIZER_BACKEND", "mock"))

    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "large-v3"))
    whisper_device: str = field(default_factory=lambda: os.getenv("WHISPER_DEVICE", "auto"))
    whisper_language: str | None = field(default_factory=lambda: os.getenv("WHISPER_LANGUAGE") or None)
    pyannote_model: str = field(
        default_factory=lambda: os.getenv("PYANNOTE_MODEL", "pyannote/speaker-diarization-3.1")
    )
    huggingface_token: str | None = field(default_factory=lambda: os.getenv("HUGGINGFACE_TOKEN") or None)
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    llm_api_key: str | None = field(default_factory=lambda: os.getenv("LLM_API_KEY") or None)
    llm_base_url: str | None = field(default_factory=lambda: os.getenv("LLM_BASE_URL") or None)

    # ---- audio normalisation (matches the dataset team's 16 kHz mono standard) ----
    target_sample_rate: int = field(default_factory=lambda: _int("TARGET_SAMPLE_RATE", 16000))
    target_channels: int = field(default_factory=lambda: _int("TARGET_CHANNELS", 1))
    ffmpeg_bin: str = field(default_factory=lambda: os.getenv("FFMPEG_BIN", "ffmpeg"))

    # ---- privacy controls ----
    require_consent: bool = field(default_factory=lambda: _bool("REQUIRE_CONSENT", True))
    encrypt_at_rest: bool = field(default_factory=lambda: _bool("ENCRYPT_AT_REST", True))
    encryption_key: str | None = field(default_factory=lambda: os.getenv("ENCRYPTION_KEY") or None)
    delete_audio_after_asr: bool = field(default_factory=lambda: _bool("DELETE_AUDIO_AFTER_ASR", True))
    redact_pii: bool = field(default_factory=lambda: _bool("REDACT_PII", True))
    allow_external_llm: bool = field(default_factory=lambda: _bool("ALLOW_EXTERNAL_LLM", False))
    retention_hours: int = field(default_factory=lambda: _int("RETENTION_HOURS", 24))
    retention_sweep_seconds: int = field(default_factory=lambda: _int("RETENTION_SWEEP_SECONDS", 300))
    audit_log_enabled: bool = field(default_factory=lambda: _bool("AUDIT_LOG_ENABLED", True))

    # ---- grounding / hallucination control ----
    min_evidence_overlap: float = field(default_factory=lambda: _float("MIN_EVIDENCE_OVERLAP", 0.55))
    drop_ungrounded_claims: bool = field(default_factory=lambda: _bool("DROP_UNGROUNDED_CLAIMS", True))

    # ---- worker ----
    worker_concurrency: int = field(default_factory=lambda: _int("WORKER_CONCURRENCY", 1))
    job_timeout_seconds: int = field(default_factory=lambda: _int("JOB_TIMEOUT_SECONDS", 3600))

    # ---- serverless / demo-lite modes ----
    # Vercel and other serverless runtimes: run the pipeline inline on upload
    # so a single request returns the completed result (no background worker,
    # no filesystem persistence across invocations).
    synchronous_jobs: bool = field(default_factory=lambda: _bool("SYNCHRONOUS_JOBS", False))
    # Skip ffmpeg normalisation — the mock backends do not need real audio,
    # and ffmpeg is not available on serverless runtimes.
    demo_mode_lite: bool = field(default_factory=lambda: _bool("DEMO_MODE_LITE", False))

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.log"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def web_dir(self) -> Path:
        return BASE_DIR / "app" / "web"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.uploads_dir, self.jobs_dir):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def capabilities(self) -> dict:
        """What this deployment can actually do — surfaced in the UI."""
        return {
            "asr_backend": self.asr_backend,
            "diarization_backend": self.diarization_backend,
            "summarizer_backend": self.summarizer_backend,
            "demo_mode": {self.asr_backend, self.diarization_backend, self.summarizer_backend} == {"mock"},
            "privacy": {
                "require_consent": self.require_consent,
                "encrypt_at_rest": self.encrypt_at_rest,
                "delete_audio_after_asr": self.delete_audio_after_asr,
                "redact_pii": self.redact_pii,
                "allow_external_llm": self.allow_external_llm,
                "retention_hours": self.retention_hours,
            },
            "limits": {
                "max_upload_mb": self.max_upload_mb,
                "max_duration_minutes": self.max_duration_minutes,
                "allowed_extensions": self.allowed_extensions,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
