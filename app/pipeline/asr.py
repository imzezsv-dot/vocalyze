"""Speech recognition — the seam where the ASR component plugs in.

The integration layer never imports Whisper directly. It depends on the
`ASRBackend` protocol below, so the ASR owner can move from openai-whisper to
faster-whisper to a fine-tuned checkpoint without touching the API, the
aligner or the UI. What has to stay stable is the returned `ASRResult`,
especially `words` — word timings are what makes speaker attribution accurate,
so a backend that cannot produce them degrades the whole product.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..core.logging import get_logger
from ..schemas import ASRResult, ASRSegment, Word

log = get_logger("vocalyze.asr")


class ASRBackend(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> ASRResult: ...


# ---------------------------------------------------------------------------
class MockASR:
    """Replays the scripted meeting. Used for demo mode and CI."""

    name = "mock"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings

    def transcribe(self, audio_path: Path) -> ASRResult:
        from . import fixtures

        segments = [
            ASRSegment(
                start=segment["start"],
                end=segment["end"],
                text=segment["text"],
                confidence=segment["confidence"],
                words=[Word(**word) for word in segment["words"]],
            )
            for segment in fixtures.build_segments()
        ]
        return ASRResult(
            language="en",
            duration=fixtures.total_duration(),
            segments=segments,
            model="scripted-sample",
            backend=self.name,
        )


# ---------------------------------------------------------------------------
class WhisperASR:
    """Whisper via faster-whisper, falling back to openai-whisper.

    faster-whisper is preferred: same weights, roughly four times faster on the
    same hardware, and it streams segments so a long meeting does not sit in
    memory. Both paths request word timestamps.
    """

    name = "whisper"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._flavour = None

    def _load(self):
        if self._model is not None:
            return self._model

        device = self.settings.whisper_device
        try:
            from faster_whisper import WhisperModel  # type: ignore

            if device == "auto":
                device = "cuda" if _cuda_available() else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"
            log.info("loading faster-whisper %s on %s", self.settings.whisper_model, device)
            self._model = WhisperModel(self.settings.whisper_model, device=device, compute_type=compute_type)
            self._flavour = "faster-whisper"
            return self._model
        except ImportError:
            pass

        try:
            import whisper  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "ASR_BACKEND=whisper needs a Whisper package. "
                "Install one: pip install faster-whisper  (or)  pip install openai-whisper"
            ) from exc

        log.info("loading openai-whisper %s", self.settings.whisper_model)
        self._model = whisper.load_model(self.settings.whisper_model)
        self._flavour = "openai-whisper"
        return self._model

    def transcribe(self, audio_path: Path) -> ASRResult:
        model = self._load()
        if self._flavour == "faster-whisper":
            return self._transcribe_faster(model, audio_path)
        return self._transcribe_openai(model, audio_path)

    def _transcribe_faster(self, model, audio_path: Path) -> ASRResult:
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=self.settings.whisper_language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
        )
        segments: list[ASRSegment] = []
        for segment in segments_iter:
            words = [
                Word(
                    start=float(word.start),
                    end=float(word.end),
                    text=word.word.strip(),
                    confidence=_exp(getattr(word, "probability", None)),
                )
                for word in (segment.words or [])
                if word.word and word.word.strip()
            ]
            segments.append(
                ASRSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=segment.text.strip(),
                    words=words,
                    confidence=_exp(getattr(segment, "avg_logprob", None), logprob=True),
                    no_speech_prob=getattr(segment, "no_speech_prob", None),
                )
            )
        return ASRResult(
            language=getattr(info, "language", None),
            duration=float(getattr(info, "duration", 0.0)),
            segments=segments,
            model=self.settings.whisper_model,
            backend="faster-whisper",
        )

    def _transcribe_openai(self, model, audio_path: Path) -> ASRResult:
        payload = model.transcribe(
            str(audio_path),
            language=self.settings.whisper_language,
            word_timestamps=True,
            verbose=False,
        )
        segments: list[ASRSegment] = []
        for segment in payload.get("segments", []):
            words = [
                Word(
                    start=float(word["start"]),
                    end=float(word["end"]),
                    text=str(word.get("word", "")).strip(),
                    confidence=word.get("probability"),
                )
                for word in segment.get("words", [])
                if str(word.get("word", "")).strip()
            ]
            segments.append(
                ASRSegment(
                    start=float(segment["start"]),
                    end=float(segment["end"]),
                    text=str(segment.get("text", "")).strip(),
                    words=words,
                    confidence=_exp(segment.get("avg_logprob"), logprob=True),
                    no_speech_prob=segment.get("no_speech_prob"),
                )
            )
        duration = segments[-1].end if segments else 0.0
        return ASRResult(
            language=payload.get("language"),
            duration=duration,
            segments=segments,
            model=self.settings.whisper_model,
            backend="openai-whisper",
        )


def _exp(value, logprob: bool = False) -> float | None:
    """Turn a log probability into something a person can read as confidence."""
    if value is None:
        return None
    try:
        import math

        return round(min(1.0, max(0.0, math.exp(float(value)) if logprob else float(value))), 3)
    except (ValueError, OverflowError):
        return None


def _cuda_available() -> bool:
    try:
        import torch  # type: ignore

        return bool(torch.cuda.is_available())
    except ImportError:
        return False
