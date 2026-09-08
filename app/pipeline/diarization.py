"""Speaker diarization — the seam where the diarization component plugs in.

Same contract idea as the ASR seam: this layer knows only `DiarizationBackend`
and `DiarizationResult`. pyannote 3.1 needs a Hugging Face token and acceptance
of the model's licence terms, and that failure mode is common enough that the
error message says exactly what to do rather than surfacing a raw 401.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..core.logging import get_logger
from ..schemas import DiarizationResult, SpeakerTurn

log = get_logger("vocalyze.diarization")


class DiarizationBackend(Protocol):
    name: str

    def diarize(self, audio_path: Path, num_speakers: int | None = None) -> DiarizationResult: ...


# ---------------------------------------------------------------------------
class MockDiarizer:
    """Turns from the scripted meeting, with boundary jitter and one genuine
    overlap, so the aligner is exercised rather than handed a clean answer."""

    name = "mock"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings

    def diarize(self, audio_path: Path, num_speakers: int | None = None) -> DiarizationResult:
        from . import fixtures

        turns = [SpeakerTurn(**turn) for turn in fixtures.build_turns()]
        return DiarizationResult(
            turns=turns,
            num_speakers=len({t.speaker for t in turns}),
            model="scripted-sample",
            backend=self.name,
        )


# ---------------------------------------------------------------------------
class PyannoteDiarizer:
    name = "pyannote"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pipeline = None

    def _load(self):
        if self._pipeline is not None:
            return self._pipeline
        try:
            from pyannote.audio import Pipeline  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "DIARIZATION_BACKEND=pyannote needs pyannote.audio. Install it: pip install pyannote.audio"
            ) from exc

        if not self.settings.huggingface_token:
            raise RuntimeError(
                "pyannote needs a Hugging Face token. Accept the model terms at "
                f"https://hf.co/{self.settings.pyannote_model} then set HUGGINGFACE_TOKEN."
            )

        log.info("loading %s", self.settings.pyannote_model)
        pipeline = Pipeline.from_pretrained(
            self.settings.pyannote_model, use_auth_token=self.settings.huggingface_token
        )
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
        except ImportError:
            pass
        self._pipeline = pipeline
        return pipeline

    def diarize(self, audio_path: Path, num_speakers: int | None = None) -> DiarizationResult:
        pipeline = self._load()
        kwargs: dict = {}
        if num_speakers:
            kwargs["num_speakers"] = int(num_speakers)
        annotation = pipeline(str(audio_path), **kwargs)

        turns = [
            SpeakerTurn(start=float(segment.start), end=float(segment.end), speaker=str(speaker))
            for segment, _track, speaker in annotation.itertracks(yield_label=True)
            if float(segment.end) > float(segment.start)
        ]
        turns.sort(key=lambda t: (t.start, t.end))
        return DiarizationResult(
            turns=turns,
            num_speakers=len({t.speaker for t in turns}),
            model=self.settings.pyannote_model,
            backend=self.name,
        )
