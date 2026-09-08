"""Word-to-speaker alignment.

This is the piece the whole product rests on — if it is wrong, the transcript
attributes words to the wrong person and every claim in the brief cites the
wrong line. The cases below are the ones real audio produces: turn boundaries
that drift, two people talking at once, silence nobody owns, a diarizer that
returned nothing, and an ASR backend that gives segments but no word timings.
"""

from __future__ import annotations

from app.pipeline.align_core import (
    UNKNOWN_SPEAKER,
    AlignOptions,
    align,
    assign_word,
    overlap,
    smooth_islands,
    split_segment_by_turns,
)


def word(start, end, text="word", confidence=0.9):
    return {"start": start, "end": end, "text": text, "confidence": confidence}


def turn(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker}


# ---------------------------------------------------------------- geometry
def test_overlap_is_zero_when_intervals_only_touch():
    assert overlap(0.0, 1.0, 1.0, 2.0) == 0.0
    assert overlap(0.0, 1.0, 2.0, 3.0) == 0.0
    assert overlap(0.0, 2.0, 1.0, 3.0) == 1.0


# ------------------------------------------------------- word assignment
def test_word_goes_to_the_speaker_covering_most_of_it():
    turns = [turn(0.0, 1.2, "A"), turn(1.2, 3.0, "B")]
    speaker, share = assign_word(word(1.0, 1.8), turns, AlignOptions())
    assert speaker == "B"
    assert 0.7 < share <= 1.0


def test_crosstalk_tie_goes_to_the_tighter_turn():
    """Two speakers cover the word equally. A short turn bracketing the word is
    stronger evidence than a long turn that merely spans it."""
    turns = [turn(0.0, 30.0, "A"), turn(4.9, 5.6, "B")]
    speaker, _ = assign_word(word(5.0, 5.5), turns, AlignOptions())
    assert speaker == "B"


def test_word_just_outside_a_turn_is_adopted_by_it():
    """Boundary drift of a few hundred milliseconds is normal, not a new speaker."""
    turns = [turn(0.0, 2.0, "A")]
    speaker, share = assign_word(word(2.1, 2.4), turns, AlignOptions())
    assert speaker == "A"
    assert share == 0.0  # adopted, and honest about the lack of real overlap


def test_word_in_real_silence_is_unknown_not_guessed():
    turns = [turn(0.0, 2.0, "A"), turn(30.0, 32.0, "B")]
    speaker, _ = assign_word(word(12.0, 12.5), turns, AlignOptions())
    assert speaker == UNKNOWN_SPEAKER


def test_no_turns_at_all_yields_unknown():
    speaker, _ = assign_word(word(1.0, 1.5), [], AlignOptions())
    assert speaker == UNKNOWN_SPEAKER


# ------------------------------------------------------------- smoothing
def test_single_word_flip_is_absorbed():
    assert smooth_islands(["A", "A", "B", "A", "A"]) == ["A", "A", "A", "A", "A"]


def test_a_real_interjection_survives_smoothing():
    """Three words in a row is someone actually speaking, not boundary noise."""
    assert smooth_islands(["A", "A", "B", "B", "B", "A", "A"]) == ["A", "A", "B", "B", "B", "A", "A"]


def test_smoothing_never_invents_a_speaker_for_unknown():
    assert smooth_islands(["A", "A", UNKNOWN_SPEAKER, "A", "A"])[2] == UNKNOWN_SPEAKER


# ------------------------------------------------------------- end to end
def test_align_groups_consecutive_words_into_one_utterance_per_speaker():
    segments = [
        {
            "start": 0.0,
            "end": 4.0,
            "text": "one two three four",
            "words": [word(0.0, 0.9, "one"), word(1.0, 1.9, "two"), word(2.1, 2.9, "three"), word(3.1, 3.9, "four")],
        }
    ]
    turns = [turn(0.0, 2.0, "A"), turn(2.0, 4.0, "B")]
    utterances, stats = align(segments, turns)

    assert [u["speaker"] for u in utterances] == ["A", "B"]
    assert utterances[0]["text"] == "one two"
    assert utterances[1]["text"] == "three four"
    assert [u["id"] for u in utterances] == ["u1", "u2"]
    assert stats.words_total == 4
    assert stats.words_unassigned == 0


def test_utterance_ids_are_sequential_and_time_ordered():
    segments = [
        {
            "start": 0.0,
            "end": 6.0,
            "text": "a b c",
            "words": [word(0.0, 0.5, "a"), word(2.5, 3.0, "b"), word(5.0, 5.5, "c")],
        }
    ]
    turns = [turn(0.0, 1.0, "A"), turn(2.0, 3.5, "B"), turn(4.5, 6.0, "A")]
    utterances, _ = align(segments, turns)
    assert [u["id"] for u in utterances] == [f"u{i}" for i in range(1, len(utterances) + 1)]
    assert utterances == sorted(utterances, key=lambda u: u["start"])


def test_crosstalk_is_flagged_rather_than_hidden():
    segments = [
        {
            "start": 0.0,
            "end": 3.0,
            "text": "hello there friend",
            "words": [word(0.1, 0.9, "hello"), word(1.0, 1.9, "there"), word(2.0, 2.9, "friend")],
        }
    ]
    turns = [turn(0.0, 3.0, "A"), turn(0.0, 3.0, "B")]
    utterances, stats = align(segments, turns)
    assert stats.overlapped_utterances == len(utterances)
    assert all(u["overlapped"] for u in utterances)


def test_a_diarizer_that_returned_nothing_still_produces_a_transcript():
    """Losing speaker labels must not lose the meeting."""
    segments = [
        {
            "start": 0.0,
            "end": 2.0,
            "text": "still readable",
            "words": [word(0.0, 0.9, "still"), word(1.0, 1.9, "readable")],
        }
    ]
    utterances, stats = align(segments, [])
    assert len(utterances) == 1
    assert utterances[0]["speaker"] == UNKNOWN_SPEAKER
    assert utterances[0]["text"] == "still readable"
    assert stats.speakers == []
    assert stats.unassigned_speech_seconds > 0


def test_asr_without_word_timings_falls_back_to_proportional_split():
    segments = [{"start": 0.0, "end": 10.0, "text": "one two three four five six seven eight", "confidence": 0.8}]
    turns = [turn(0.0, 5.0, "A"), turn(5.0, 10.0, "B")]
    utterances, stats = align(segments, turns)

    assert stats.used_word_timings is False
    assert [u["speaker"] for u in utterances] == ["A", "B"]
    # No text is lost or duplicated by the split.
    assert " ".join(u["text"] for u in utterances) == "one two three four five six seven eight"


def test_a_dominant_speaker_keeps_the_whole_segment_undivided():
    segments = [{"start": 0.0, "end": 10.0, "text": "mostly one person speaking here"}]
    turns = [turn(0.0, 9.5, "A"), turn(9.5, 10.0, "B")]
    parts = split_segment_by_turns(segments[0], turns, AlignOptions())
    assert len(parts) == 1
    assert parts[0]["speaker"] == "A"
    assert parts[0]["text"] == "mostly one person speaking here"


def test_alignment_reproduces_the_fixture_ground_truth():
    """The scripted meeting carries the speaker of every word. Running it
    through the mock diarizer's jittered, merged turns and back out again must
    return every word to the person who said it."""
    from app.pipeline import fixtures

    truth = fixtures.build_words()
    utterances, _ = align(fixtures.build_segments(), fixtures.build_turns())

    correct = 0
    for expected in truth:
        midpoint = (expected["start"] + expected["end"]) / 2
        owner = next(
            (u["speaker"] for u in utterances if u["start"] <= midpoint <= u["end"]),
            None,
        )
        if owner == expected["speaker"]:
            correct += 1

    accuracy = correct / len(truth)
    assert accuracy == 1.0, f"word-level speaker accuracy dropped to {accuracy:.1%}"
