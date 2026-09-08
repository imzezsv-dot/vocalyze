"""Word-to-speaker alignment — the piece that makes two models into one system.

Whisper answers *what was said and when*. pyannote answers *who was speaking
and when*. Neither answers *who said what*, and that is the whole product, so
it gets written here, in the integration layer, rather than being smuggled
into either model's component.

The two clocks never line up exactly: Whisper's word timings drift at segment
edges, pyannote's turn boundaries land mid-word, and both are wrong during
crosstalk. The approach is therefore overlap-maximising rather than
boundary-matching — each word goes to the speaker whose turns cover most of
that word's span — with an explicit "unknown" outcome instead of a confident
guess when nothing overlaps at all.

Deliberately stdlib-only and free of framework types: this is the part worth
unit-testing hardest, and it should be testable without standing up an app.
"""

from __future__ import annotations

from dataclasses import dataclass, field

UNKNOWN_SPEAKER = "UNKNOWN"
SENTENCE_END = ".!?؟。…"


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Seconds shared by two intervals; 0.0 when they only touch or miss."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def total_overlap(start: float, end: float, turns: list[dict], speaker: str) -> float:
    return sum(overlap(start, end, t["start"], t["end"]) for t in turns if t["speaker"] == speaker)


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------
@dataclass
class AlignOptions:
    max_gap: float = 0.8
    """Silence inside one speaker's stretch that still counts as one utterance."""

    max_utterance_seconds: float = 30.0
    """Long monologues are split so the UI and the LLM both get workable chunks."""

    nearest_turn_tolerance: float = 0.5
    """A word just outside every turn is adopted by the nearest turn within this
    distance — covers boundary drift without inventing speakers for real silence."""

    dominance: float = 0.8
    """When ASR gives no word timings, a turn covering this much of a segment
    takes the whole segment instead of the text being split."""

    overlap_flag: float = 0.3
    """Share of an utterance covered by a *competing* speaker before it is
    flagged as crosstalk. Flagged, not hidden: the reader should know."""

    min_words_per_part: int = 2
    """Below this, a proportional split produces fragments; keep the text whole."""


@dataclass
class AlignStats:
    words_total: int = 0
    words_unassigned: int = 0
    unassigned_speech_seconds: float = 0.0
    overlapped_utterances: int = 0
    speakers: list[str] = field(default_factory=list)
    used_word_timings: bool = True


# ---------------------------------------------------------------------------
# word-level assignment
# ---------------------------------------------------------------------------
def assign_word(word: dict, turns: list[dict], opts: AlignOptions) -> tuple[str, float]:
    """Return (speaker, share_of_word_covered) for one word."""
    start, end = float(word["start"]), float(word["end"])
    if end < start:
        start, end = end, start
    duration = max(end - start, 1e-6)

    # speaker -> (seconds of this word they cover, tightest turn that covers it)
    per_speaker: dict[str, tuple[float, float]] = {}
    for turn in turns:
        shared = overlap(start, end, turn["start"], turn["end"])
        if shared <= 0:
            continue
        span = turn["end"] - turn["start"]
        covered, tightest = per_speaker.get(turn["speaker"], (0.0, float("inf")))
        per_speaker[turn["speaker"]] = (covered + shared, min(tightest, span))

    if per_speaker:
        # During crosstalk two turns cover the word equally. The tighter turn
        # wins: a short turn bracketing this word is stronger evidence than a
        # long turn that merely spans it.
        best_speaker, (best_cover, _span) = min(
            per_speaker.items(), key=lambda kv: (-kv[1][0], kv[1][1])
        )
        return best_speaker, min(1.0, best_cover / duration)

    # No overlap at all: a word sitting in the gap between two turns. Adopt the
    # nearest turn if it is close enough, otherwise say so.
    midpoint = (start + end) / 2
    nearest, distance = None, float("inf")
    for turn in turns:
        gap = 0.0 if turn["start"] <= midpoint <= turn["end"] else min(
            abs(turn["start"] - midpoint), abs(turn["end"] - midpoint)
        )
        if gap < distance:
            nearest, distance = turn, gap
    if nearest is not None and distance <= opts.nearest_turn_tolerance:
        return nearest["speaker"], 0.0
    return UNKNOWN_SPEAKER, 0.0


def smooth_islands(speakers: list[str], min_island: int = 2) -> list[str]:
    """Absorb one- or two-word speaker flips surrounded by one other speaker.

    Turn boundaries drift by a word or two, which produces isolated flips like
    A A A B A A A. Nobody speaks a single word inside someone else's sentence
    and then hands it straight back, so the island is boundary noise and gets
    absorbed. Runs longer than `min_island` are left alone — those are real
    short interjections and deserve their own utterance.
    """
    if len(speakers) < 3:
        return speakers

    result = list(speakers)
    index = 0
    while index < len(result):
        end = index
        while end + 1 < len(result) and result[end + 1] == result[index]:
            end += 1
        run_length = end - index + 1
        before = result[index - 1] if index > 0 else None
        after = result[end + 1] if end + 1 < len(result) else None
        if (
            run_length <= min_island
            and before is not None
            and before == after
            and before != result[index]
            and result[index] != UNKNOWN_SPEAKER
        ):
            for position in range(index, end + 1):
                result[position] = before
        index = end + 1
    return result


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------
def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    # Rounded: this is a confidence shown to a reader, and float noise like
    # 0.8982500000000001 reads as false precision in the API and the exports.
    return round(sum(clean) / len(clean), 3) if clean else None


def _flush(buffer: list[dict], speaker: str, turns: list[dict], opts: AlignOptions) -> dict | None:
    if not buffer:
        return None
    start = float(buffer[0]["start"])
    end = float(buffer[-1]["end"])
    duration = max(end - start, 1e-6)
    text = " ".join(w["text"].strip() for w in buffer if w["text"].strip())
    text = text.replace(" ,", ",").replace(" .", ".").strip()

    own = total_overlap(start, end, turns, speaker)
    competitors = {t["speaker"] for t in turns if t["speaker"] != speaker and overlap(start, end, t["start"], t["end"]) > 0}
    rival = max((total_overlap(start, end, turns, s) for s in competitors), default=0.0)

    return {
        "speaker": speaker,
        "start": round(start, 3),
        "end": round(end, 3),
        "text": text,
        "confidence": _mean([w.get("confidence") for w in buffer]),
        "speaker_confidence": round(min(1.0, own / duration), 3) if turns else None,
        "overlapped": rival / duration >= opts.overlap_flag,
    }


def group_words(words: list[dict], speakers: list[str], turns: list[dict], opts: AlignOptions) -> list[dict]:
    """Consecutive words by one speaker become one utterance."""
    utterances: list[dict] = []
    buffer: list[dict] = []
    current = None

    for index, word in enumerate(words):
        speaker = speakers[index]
        if not word.get("text", "").strip():
            continue

        if buffer:
            gap = float(word["start"]) - float(buffer[-1]["end"])
            span = float(word["end"]) - float(buffer[0]["start"])
            ends_sentence = buffer[-1]["text"].strip().endswith(tuple(SENTENCE_END))
            too_long = span > opts.max_utterance_seconds and (ends_sentence or gap > 0.3)
            if speaker != current or gap > opts.max_gap or too_long:
                flushed = _flush(buffer, current, turns, opts)
                if flushed:
                    utterances.append(flushed)
                buffer = []

        current = speaker
        buffer.append(word)

    flushed = _flush(buffer, current, turns, opts)
    if flushed:
        utterances.append(flushed)
    return [u for u in utterances if u["text"]]


# ---------------------------------------------------------------------------
# fallback: ASR without word timings
# ---------------------------------------------------------------------------
def split_segment_by_turns(segment: dict, turns: list[dict], opts: AlignOptions) -> list[dict]:
    """Divide a segment's text across the speakers active during it.

    Used when the ASR backend returns segment timings only. Words are handed
    out in proportion to how long each speaker held the floor — an
    approximation, and marked as one by a lower speaker_confidence.
    """
    start, end = float(segment["start"]), float(segment["end"])
    duration = max(end - start, 1e-6)
    text = segment.get("text", "").strip()
    if not text:
        return []

    covering = [
        (t, overlap(start, end, t["start"], t["end"]))
        for t in turns
        if overlap(start, end, t["start"], t["end"]) > 0
    ]
    if not covering:
        return [
            {
                "speaker": UNKNOWN_SPEAKER,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "confidence": segment.get("confidence"),
                "speaker_confidence": 0.0,
                "overlapped": False,
            }
        ]

    per_speaker: dict[str, float] = {}
    for turn, shared in covering:
        per_speaker[turn["speaker"]] = per_speaker.get(turn["speaker"], 0.0) + shared
    top_speaker, top_share = max(per_speaker.items(), key=lambda kv: kv[1])

    tokens = text.split()
    if top_share / duration >= opts.dominance or len(per_speaker) == 1 or len(tokens) < opts.min_words_per_part * 2:
        return [
            {
                "speaker": top_speaker,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "confidence": segment.get("confidence"),
                "speaker_confidence": round(min(1.0, top_share / duration), 3),
                "overlapped": len(per_speaker) > 1,
            }
        ]

    # Walk the turns in time order and hand out words proportionally.
    ordered = sorted((t for t, _ in covering), key=lambda t: t["start"])
    parts: list[dict] = []
    cursor = 0
    consumed = 0.0
    for position, turn in enumerate(ordered):
        piece_start = max(start, turn["start"])
        piece_end = min(end, turn["end"])
        share = max(0.0, piece_end - piece_start) / duration
        consumed += share
        last = position == len(ordered) - 1
        take = len(tokens) - cursor if last else max(opts.min_words_per_part, round(share * len(tokens)))
        chunk = tokens[cursor : cursor + take]
        cursor += take
        if not chunk:
            continue
        parts.append(
            {
                "speaker": turn["speaker"],
                "start": round(piece_start, 3),
                "end": round(piece_end, 3),
                "text": " ".join(chunk),
                "confidence": segment.get("confidence"),
                "speaker_confidence": round(min(1.0, share), 3),
                "overlapped": False,
            }
        )
        if cursor >= len(tokens):
            break
    return parts or [
        {
            "speaker": top_speaker,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "confidence": segment.get("confidence"),
            "speaker_confidence": round(min(1.0, top_share / duration), 3),
            "overlapped": True,
        }
    ]


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def align(
    segments: list[dict],
    turns: list[dict],
    opts: AlignOptions | None = None,
) -> tuple[list[dict], AlignStats]:
    """Merge ASR segments and diarization turns into speaker-attributed utterances.

    `segments`: [{start, end, text, confidence?, words?: [{start,end,text,confidence?}]}]
    `turns`:    [{start, end, speaker, confidence?}]
    """
    opts = opts or AlignOptions()
    turns = sorted(
        ({"start": float(t["start"]), "end": float(t["end"]), "speaker": str(t["speaker"])} for t in turns),
        key=lambda t: (t["start"], t["end"]),
    )
    segments = sorted(segments, key=lambda s: float(s["start"]))
    stats = AlignStats()

    words: list[dict] = []
    for segment in segments:
        for word in segment.get("words") or []:
            if word.get("text", "").strip():
                words.append(
                    {
                        "start": float(word["start"]),
                        "end": float(word["end"]),
                        "text": word["text"],
                        "confidence": word.get("confidence", segment.get("confidence")),
                    }
                )

    if words:
        words.sort(key=lambda w: (w["start"], w["end"]))
        assigned = [assign_word(word, turns, opts)[0] for word in words]
        assigned = smooth_islands(assigned)
        for word, speaker in zip(words, assigned):
            if speaker == UNKNOWN_SPEAKER:
                stats.words_unassigned += 1
                stats.unassigned_speech_seconds += max(0.0, word["end"] - word["start"])
        stats.words_total = len(words)
        utterances = group_words(words, assigned, turns, opts)
    else:
        stats.used_word_timings = False
        utterances = []
        for segment in segments:
            parts = split_segment_by_turns(segment, turns, opts)
            utterances.extend(parts)
            for part in parts:
                if part["speaker"] == UNKNOWN_SPEAKER:
                    stats.unassigned_speech_seconds += part["end"] - part["start"]

    utterances.sort(key=lambda u: (u["start"], u["end"]))
    for index, utterance in enumerate(utterances, start=1):
        utterance["id"] = f"u{index}"

    stats.overlapped_utterances = sum(1 for u in utterances if u["overlapped"])
    stats.speakers = sorted({u["speaker"] for u in utterances if u["speaker"] != UNKNOWN_SPEAKER})
    stats.unassigned_speech_seconds = round(stats.unassigned_speech_seconds, 2)
    return utterances, stats


def relabel_speakers(utterances: list[dict], mapping: dict[str, str]) -> list[dict]:
    """Apply human names once someone identifies the speakers in the UI."""
    for utterance in utterances:
        utterance["speaker"] = mapping.get(utterance["speaker"], utterance["speaker"])
    return utterances
