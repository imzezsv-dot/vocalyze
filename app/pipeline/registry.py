"""Backend selection.

One place that maps a configuration string to an implementation. Models are
built once per process and cached: loading Whisper takes tens of seconds and
several gigabytes, so a per-request construction would be a denial of service
against ourselves.
"""

from __future__ import annotations

from functools import lru_cache

from ..config import Settings, get_settings
from ..core.logging import get_logger
from .asr import ASRBackend, MockASR, WhisperASR
from .diarization import DiarizationBackend, MockDiarizer, PyannoteDiarizer
from .summarizer import ExtractiveSummarizer, LLMSummarizer, SummarizerBackend

log = get_logger("vocalyze.registry")

_ASR = {"mock": MockASR, "whisper": WhisperASR}
_DIARIZATION = {"mock": MockDiarizer, "pyannote": PyannoteDiarizer}
_SUMMARIZER = {"mock": ExtractiveSummarizer, "extractive": ExtractiveSummarizer, "llm": LLMSummarizer}


def _resolve(table: dict, key: str, kind: str, settings: Settings):
    try:
        factory = table[key.lower()]
    except KeyError:
        raise RuntimeError(
            f"Unknown {kind} backend '{key}'. Available: {', '.join(sorted(table))}."
        ) from None
    return factory(settings)


_cache: dict[str, object] = {}


def get_asr(settings: Settings | None = None) -> ASRBackend:
    settings = settings or get_settings()
    if "asr" not in _cache:
        _cache["asr"] = _resolve(_ASR, settings.asr_backend, "ASR", settings)
    return _cache["asr"]  # type: ignore[return-value]


def get_diarizer(settings: Settings | None = None) -> DiarizationBackend:
    settings = settings or get_settings()
    if "diarizer" not in _cache:
        _cache["diarizer"] = _resolve(_DIARIZATION, settings.diarization_backend, "diarization", settings)
    return _cache["diarizer"]  # type: ignore[return-value]


def get_summarizer(settings: Settings | None = None) -> SummarizerBackend:
    settings = settings or get_settings()
    if "summarizer" not in _cache:
        _cache["summarizer"] = _resolve(_SUMMARIZER, settings.summarizer_backend, "summarizer", settings)
    return _cache["summarizer"]  # type: ignore[return-value]


def preload() -> None:
    """Load models at startup so the first upload is not the slowest one."""
    settings = get_settings()
    for name, getter in (("ASR", get_asr), ("diarization", get_diarizer), ("summarizer", get_summarizer)):
        try:
            getter(settings)
        except Exception as exc:  # noqa: BLE001 - a missing model must not stop the API
            log.warning("%s backend unavailable at startup: %s", name, exc)
