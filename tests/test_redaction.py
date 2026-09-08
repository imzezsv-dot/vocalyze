"""PII redaction, in both directions.

Precision matters as much as recall here. Over-redaction destroys meeting
content — budgets, dates, percentages and version numbers are the substance of
a meeting — so the second half of this file is as important as the first.
"""

from __future__ import annotations

import pytest

from app.pipeline.redaction import redact_text, redact_utterances

# ------------------------------------------------------- things to remove
REMOVED = [
    ("email", "Send it to layla.ahmed@example.com before Friday"),
    ("phone", "Call me on +966 50 123 4567 when you land"),
    ("phone", "My number is 0501234567"),
    ("url", "The draft is at https://internal.example.com/secret/doc"),
    ("iban", "Transfer to SA0380000000608010167519 today"),
    ("card", "The card is 4111 1111 1111 1111"),
    ("ip address", "The box answers on 192.168.10.42"),
    ("national id", "His id is 1098765432"),
]


@pytest.mark.parametrize("kind,text", REMOVED, ids=[f"{k}:{t[:18]}" for k, t in REMOVED])
def test_identifiers_are_replaced_and_counted(kind, text):
    cleaned, report = redact_text(text)
    assert report.count == 1
    assert report.by_kind == {kind: 1}
    assert f"[{kind} redacted]" in cleaned


# --------------------------------------------------------- things to keep
KEPT = [
    "Word error rate is 11.2 percent, down from 18.4",
    "The budget is 250000 riyals for the year",
    "We ship on 2026-03-15",
    "Version 3.11.9 is the one we tested",
    "Ninety hours of audio after cleaning",
    "Meeting room 402, third floor",
    "The split is 70 15 15",
]


@pytest.mark.parametrize("text", KEPT, ids=[t[:24] for t in KEPT])
def test_meeting_substance_is_never_redacted(text):
    cleaned, report = redact_text(text)
    assert report.count == 0
    assert cleaned == text


def test_a_number_that_fails_the_luhn_check_is_not_a_card():
    cleaned, report = redact_text("Order reference 4111 1111 1111 1112 shipped")
    assert report.count == 0
    assert cleaned.endswith("shipped")


def test_an_impossible_ip_is_left_alone():
    cleaned, report = redact_text("Coordinates 999.888.777.666 are not an address")
    assert report.count == 0


def test_a_url_is_not_mistaken_for_a_phone_number():
    cleaned, report = redact_text("See https://example.com/2025/01/15/report")
    assert report.by_kind == {"url": 1}


def test_several_identifiers_in_one_line_are_all_removed():
    cleaned, report = redact_text("Mail sara@example.com or call +966501234567")
    assert report.count == 2
    assert report.by_kind == {"email": 1, "phone": 1}
    assert "@" not in cleaned


def test_redaction_marks_the_utterance_so_the_reader_is_told():
    utterances = [
        {"id": "u1", "text": "Nothing sensitive here at all"},
        {"id": "u2", "text": "Reach me at nora@example.com"},
    ]
    report = redact_utterances(utterances)

    assert report.count == 1
    assert "redacted" not in utterances[0]
    assert utterances[1]["redacted"] is True
    assert "example.com" not in utterances[1]["text"]


def test_empty_text_is_handled():
    cleaned, report = redact_text("")
    assert cleaned == ""
    assert report.count == 0
