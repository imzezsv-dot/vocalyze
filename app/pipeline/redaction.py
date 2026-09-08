"""PII redaction, applied between the transcript and everything downstream.

Two jobs. First, data minimisation: the summariser only ever needs the shape
of the discussion, not someone's IBAN, so the identifier is replaced before
the text leaves the process. Second, honesty: redaction is counted and shown
in the UI, because silently altering a transcript is worse than not redacting.

Patterns are deliberately narrow. Over-redaction destroys meeting content —
budgets, dates and version numbers are the substance of a meeting — so card
numbers are Luhn-checked and bare numbers are left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PLACEHOLDER = "[{kind} redacted]"


@dataclass
class RedactionReport:
    count: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str) -> None:
        self.count += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def merge(self, other: "RedactionReport") -> None:
        self.count += other.count
        for kind, value in other.by_kind.items():
            self.by_kind[kind] = self.by_kind.get(kind, 0) + value


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# Order matters: the specific, self-validating patterns run before the broad
# phone pattern, so a URL or an IP is never mistaken for a phone number.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("url", re.compile(r"https?://[^\s<>\"]{4,}")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("card", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
    ("ip address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("national id", re.compile(r"\b[12]\d{9}\b")),
    ("phone", re.compile(r"(?<![\w.])\(?\+?\d[\d\s().-]{6,17}\d(?![\w.])")),
]

_DATE_LIKE = re.compile(r"^\d{1,4}([/.-])\d{1,2}\1\d{1,4}$")


def redact_text(text: str, kinds: set[str] | None = None) -> tuple[str, RedactionReport]:
    """Replace identifiers with a labelled placeholder. Returns (text, report)."""
    report = RedactionReport()
    if not text:
        return text, report

    result = text
    for kind, pattern in _RULES:
        if kinds and kind not in kinds:
            continue

        def _replace(match: re.Match[str], _kind: str = kind) -> str:
            value = match.group(0)
            if _kind == "card":
                digits = re.sub(r"\D", "", value)
                if not (13 <= len(digits) <= 19 and _luhn(digits)):
                    return value
            if _kind == "ip address":
                if any(int(part) > 255 for part in value.split(".")):
                    return value
            if _kind == "phone":
                digits = re.sub(r"\D", "", value)
                # Real numbers run 9-15 digits (E.164). Anything shorter is a
                # budget, a version or a date, and belongs in the transcript.
                if not 9 <= len(digits) <= 15 or _DATE_LIKE.match(value.strip()):
                    return value
            report.add(_kind)
            return PLACEHOLDER.format(kind=_kind)

        result = pattern.sub(_replace, result)
    return result, report


def redact_utterances(utterances: list[dict], kinds: set[str] | None = None) -> RedactionReport:
    """Redact in place. Each changed utterance is marked `redacted` so the UI
    can show a marker rather than quietly presenting an altered quote."""
    total = RedactionReport()
    for utterance in utterances:
        cleaned, report = redact_text(utterance.get("text", ""), kinds)
        if report.count:
            utterance["text"] = cleaned
            utterance["redacted"] = True
            total.merge(report)
    return total
