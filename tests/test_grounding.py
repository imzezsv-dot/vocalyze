"""Hallucination control.

The verifier's job is to catch the three ways a generated brief goes wrong:
a claim nobody made, a citation to a line that does not exist, and a figure
the model invented. A verifier that passes everything is worse than none,
because it looks like a guarantee — so these tests check the rejections as
hard as the acceptances.
"""

from __future__ import annotations

from app.pipeline.grounding_core import (
    GroundingOptions,
    Retriever,
    coverage,
    drop_ungrounded,
    ground_claim,
    numbers_in,
    verify_brief,
)

UTTERANCES = [
    {"id": "u1", "text": "The word error rate is eleven point two percent on the clean split."},
    {"id": "u2", "text": "We agreed to flag overlapping speech instead of forcing a single speaker."},
    {"id": "u3", "text": "Rima will wire the evidence check into the pipeline before Thursday."},
    {"id": "u4", "text": "The dataset is resampled to sixteen kilohertz mono."},
]
BY_ID = {u["id"]: u for u in UTTERANCES}


def ground(claim, cited=None, **kwargs):
    return ground_claim(claim, cited, BY_ID, Retriever(UTTERANCES), GroundingOptions(**kwargs))


# ------------------------------------------------------------- acceptance
def test_a_claim_supported_by_its_citation_is_verified():
    result = ground("The dataset is resampled to sixteen kilohertz mono", ["u4"])
    assert result.verified
    assert result.utterance_ids == ["u4"]
    assert result.score >= 0.55


def test_an_uncited_but_true_claim_is_recovered_with_real_citations():
    """A model that forgets to cite is not lying. The retriever finds the line."""
    result = ground("Overlapping speech is flagged instead of forcing a single speaker")
    assert result.verified
    assert "u2" in result.utterance_ids


# -------------------------------------------------------------- rejection
def test_a_claim_nobody_made_is_rejected():
    result = ground("The team decided to cancel the project and refund the sponsor")
    assert not result.verified


def test_a_citation_to_a_nonexistent_line_does_not_grant_support():
    result = ground("The budget was doubled for next quarter", ["u99"])
    assert not result.verified


def test_an_invented_figure_is_caught_even_when_the_wording_matches():
    """The most damaging failure, and the most detectable: everything else in
    the sentence comes from u1, only the number is wrong."""
    honest = ground("The word error rate is eleven point two percent on the clean split", ["u1"])
    invented = ground("The word error rate is 47 percent on the clean split", ["u1"])
    assert honest.verified
    assert not invented.verified
    assert "47" in invented.reason


def test_a_correct_figure_passes_the_number_check():
    utterances = [{"id": "u1", "text": "We processed 90 hours of audio after cleaning."}]
    result = ground_claim("90 hours of audio were processed", ["u1"], {"u1": utterances[0]}, Retriever(utterances))
    assert result.verified


def test_numbers_are_extracted_ignoring_thousands_separators():
    assert numbers_in("we spent 1,200 on 3.5 hours") == {"1200", "3.5"}


def test_coverage_of_an_empty_claim_is_zero():
    score, reason = coverage("", "anything at all", GroundingOptions())
    assert score == 0.0
    assert reason


# ------------------------------------------------------------ whole brief
def test_verify_brief_annotates_every_claim_and_counts_both_outcomes():
    brief = {
        "summary": "The team discussed word error rate and agreed to flag overlapping speech.",
        "summary_utterance_ids": ["u1", "u2"],
        "key_points": [{"text": "The dataset is sixteen kilohertz mono.", "utterance_ids": ["u4"]}],
        "decisions": [{"text": "Overlapping speech is flagged, not forced.", "utterance_ids": ["u2"]}],
        "action_items": [
            {"text": "Rima wires the evidence check before Thursday.", "owner": "Rima", "utterance_ids": ["u3"]},
            {"text": "The team will migrate the database to Postgres.", "utterance_ids": []},
        ],
    }
    verified, stats = verify_brief(dict(brief), UTTERANCES)

    assert stats["grounded"] == 3
    assert stats["ungrounded"] == 1
    assert verified["summary_evidence"]["verified"] is True
    # The raw citation field is replaced by a checked evidence block.
    for key in ("key_points", "decisions", "action_items"):
        for item in verified[key]:
            assert "utterance_ids" not in item
            assert "verified" in item["evidence"]


def test_dropping_removes_only_the_unsupported_claims():
    brief = {
        "key_points": [
            {"text": "kept", "evidence": {"verified": True}},
            {"text": "invented", "evidence": {"verified": False}},
        ],
        "decisions": [],
        "action_items": [],
    }
    result, removed = drop_ungrounded(brief)
    assert removed == 1
    assert [item["text"] for item in result["key_points"]] == ["kept"]
    assert result["dropped_claims"] == 1


def test_the_retriever_returns_nothing_for_a_claim_with_no_content_words():
    assert Retriever(UTTERANCES).search("the and of") == []
