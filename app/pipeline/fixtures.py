"""A scripted meeting for demo mode.

Demo mode exists so the API, the aligner, the verifier and the UI can be run
end to end on a laptop with no GPU and no model weights — useful for the demo,
and useful in CI where downloading Whisper is not an option.

The script is stored once and read by both mock backends, but they read it
differently on purpose: the mock ASR returns word timings, while the mock
diarizer returns speaker turns with jitter at the boundaries and a couple of
merged turns, the way a real diarizer does. So the aligner still has real work
to do, and a bug in it still shows up in the demo.
"""

from __future__ import annotations

import random

# (speaker, text, seconds_of_speech)
SCRIPT: list[tuple[str, str, float]] = [
    ("SPEAKER_00", "Alright, let's start with where the dataset stands.", 3.4),
    ("SPEAKER_01", "AMI and ICSI are both downloaded and cleaned. We are at ninety hours after removing the corrupted sessions.", 8.2),
    ("SPEAKER_01", "Everything is resampled to sixteen kilohertz mono, and the split is speaker disjoint, so no speaker appears in both train and test.", 9.1),
    ("SPEAKER_00", "Good. Any sessions we had to throw away?", 2.9),
    ("SPEAKER_01", "Four ICSI sessions had broken timestamps. They are documented in the dataset card.", 5.4),
    ("SPEAKER_02", "On the recognition side, Whisper large is running on the clean split. Word error rate is eleven point two percent, down from eighteen point four on the raw audio.", 11.6),
    ("SPEAKER_02", "Most of the remaining errors are crosstalk, where two people speak at once.", 5.0),
    ("SPEAKER_03", "Diarization has the same problem. Speaker error rate is around nine percent, and almost all of it is in the overlap regions.", 8.4),
    ("SPEAKER_00", "So both models fail in the same place. Can we handle overlap once, in the merge step, instead of twice?", 7.2),
    ("SPEAKER_03", "That is what I would suggest. Flag the overlapped spans and let the interface show them as uncertain rather than guessing a speaker.", 8.8),
    ("SPEAKER_02", "Right, uncertain is better than confidently wrong.", 2.6),
    ("SPEAKER_00", "Agreed. We flag overlap in the interface instead of forcing a single speaker.", 5.1),
    ("SPEAKER_02", "For the summary, we should require the model to quote the line it took each point from.", 5.7),
    ("SPEAKER_01", "That also gives us a check. If the quoted line does not exist, we drop the point.", 5.3),
    ("SPEAKER_00", "Let's do that. Rima will wire the evidence check into the pipeline before Thursday.", 5.6),
    ("SPEAKER_03", "I can have it ready Wednesday if the transcript format is frozen today.", 4.8),
    ("SPEAKER_00", "Consider it frozen. Saad, you write the report against this format, and we submit Sunday.", 6.4),
]

GAP = 0.45  # pause between turns

# The interjection at this script index is spoken over the previous speaker,
# so the diarizer emits two turns covering the same seconds — real crosstalk,
# which the aligner has to resolve and the interface has to admit to.
CROSSTALK_AT = 10


def build_words(seed: int = 7) -> list[dict]:
    """Expand the script into word-level timings, as an ASR model would."""
    rng = random.Random(seed)
    words: list[dict] = []
    clock = 1.2
    for speaker, text, duration in SCRIPT:
        pieces = text.split()
        per_word = duration / max(1, len(pieces))
        for piece in pieces:
            length = per_word * rng.uniform(0.75, 1.25)
            words.append(
                {
                    "start": round(clock, 3),
                    "end": round(clock + length * 0.92, 3),
                    "text": piece,
                    "confidence": round(rng.uniform(0.82, 0.99), 3),
                    "speaker": speaker,
                }
            )
            clock += length
        clock += GAP
    return words


def build_segments(seed: int = 7) -> list[dict]:
    """Group the words into ASR segments, which are not speaker turns —
    a segment can run across a speaker change, exactly as Whisper's do."""
    words = build_words(seed)
    segments: list[dict] = []
    bucket: list[dict] = []
    for word in words:
        bucket.append(word)
        span = bucket[-1]["end"] - bucket[0]["start"]
        if span > 12.0 or word["text"].endswith((".", "?")) and span > 6.0:
            segments.append(_segment(bucket))
            bucket = []
    if bucket:
        segments.append(_segment(bucket))
    return segments


def _segment(bucket: list[dict]) -> dict:
    return {
        "start": bucket[0]["start"],
        "end": bucket[-1]["end"],
        "text": " ".join(w["text"] for w in bucket),
        "confidence": round(sum(w["confidence"] for w in bucket) / len(bucket), 3),
        "words": [{k: w[k] for k in ("start", "end", "text", "confidence")} for w in bucket],
    }


def build_turns(seed: int = 7, jitter: float = 0.18) -> list[dict]:
    """Speaker turns as a diarizer would emit them: boundaries slightly off,
    consecutive same-speaker turns merged, one short overlap at a handover."""
    rng = random.Random(seed + 1)
    words = build_words(seed)
    turns: list[dict] = []
    current = None
    for word in words:
        if current and word["speaker"] == current["speaker"] and word["start"] - current["end"] < 1.2:
            current["end"] = word["end"]
            continue
        if current:
            turns.append(current)
        current = {"start": word["start"], "end": word["end"], "speaker": word["speaker"]}
    if current:
        turns.append(current)

    for turn in turns:
        turn["start"] = round(max(0.0, turn["start"] + rng.uniform(-jitter, jitter)), 3)
        turn["end"] = round(turn["end"] + rng.uniform(-jitter, jitter), 3)
        turn["confidence"] = round(rng.uniform(0.78, 0.97), 3)

    # The interjection is spoken over the previous speaker: stretch that
    # speaker's turn across it so two turns genuinely share the same seconds.
    interjection_start = _script_start(CROSSTALK_AT, seed)
    for index, turn in enumerate(turns[:-1]):
        if turn["end"] <= interjection_start <= turns[index + 1]["end"]:
            turn["end"] = round(turns[index + 1]["end"] + 0.25, 3)
            break
    return turns


def _script_start(script_index: int, seed: int) -> float:
    """When the given script line begins, in the same clock the words use."""
    words = build_words(seed)
    spoken = 0
    for position, (_speaker, text, _duration) in enumerate(SCRIPT):
        if position == script_index:
            return words[spoken]["start"] if spoken < len(words) else 0.0
        spoken += len(text.split())
    return 0.0


def total_duration(seed: int = 7) -> float:
    words = build_words(seed)
    return round(words[-1]["end"] + 1.0, 2) if words else 0.0
