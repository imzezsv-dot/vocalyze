"""Adapter between the model schemas and the pure aligner.

`align_core` deliberately knows nothing about pydantic or FastAPI so it stays
easy to test. This module is the thin translation layer either side of it.
"""

from __future__ import annotations

from ..schemas import ASRResult, DiarizationResult, Transcript, Utterance
from .align_core import UNKNOWN_SPEAKER, AlignOptions, AlignStats, align


def build_transcript(
    asr: ASRResult,
    diarization: DiarizationResult,
    opts: AlignOptions | None = None,
) -> tuple[Transcript, AlignStats]:
    segments = [
        {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "confidence": segment.confidence,
            "words": [
                {"start": word.start, "end": word.end, "text": word.text, "confidence": word.confidence}
                for word in segment.words
            ],
        }
        for segment in asr.segments
    ]
    turns = [{"start": turn.start, "end": turn.end, "speaker": turn.speaker} for turn in diarization.turns]

    raw, stats = align(segments, turns, opts)
    utterances = [Utterance(**item) for item in raw]
    speakers = sorted({u.speaker for u in utterances if u.speaker != UNKNOWN_SPEAKER})
    if any(u.speaker == UNKNOWN_SPEAKER for u in utterances):
        speakers.append(UNKNOWN_SPEAKER)

    transcript = Transcript(
        language=asr.language,
        duration=round(max([asr.duration] + [u.end for u in utterances] + [0.0]), 2),
        speakers=speakers,
        utterances=utterances,
    )
    return transcript, stats


def to_dicts(transcript: Transcript) -> list[dict]:
    return [u.model_dump() for u in transcript.utterances]
