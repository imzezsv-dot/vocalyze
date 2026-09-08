"""Grounding: does the transcript actually support what the model wrote?

An LLM asked to summarise a meeting will occasionally produce a decision that
nobody made. Prompting reduces that; it does not remove it. So the integration
layer treats every generated claim as an assertion to be checked against the
transcript before it reaches the screen:

1. the claim must cite utterance ids, and those ids must exist;
2. the claim's content words must appear in the cited utterances;
3. every number in the claim must appear in the evidence — invented figures
   are the most damaging and the most detectable failure.

A claim that cites nothing is not thrown away immediately: the retriever looks
for the utterances that would support it. If it finds them, the claim is kept
with real citations. If it finds nothing above threshold, the claim is dropped
and counted, and the count is shown in the UI.

Stdlib only, so it can be tested on its own.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

_TOKEN = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

_STOPWORDS = {
    # English function words
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from", "had", "has", "have",
    "he", "her", "his", "i", "if", "in", "is", "it", "its", "of", "on", "or", "our", "she", "should",
    "so", "that", "the", "their", "them", "then", "there", "they", "this", "to", "was", "we", "were",
    "will", "with", "would", "you", "your", "not", "no", "do", "does", "did", "can", "could", "about",
    "into", "than", "them", "these", "those", "also", "just", "than", "team", "meeting",
    # Arabic function words
    "من", "في", "على", "الى", "إلى", "عن", "مع", "هذا", "هذه", "ذلك", "التي", "الذي", "أن", "ان",
    "كان", "كانت", "قد", "لا", "ما", "هو", "هي", "نحن", "هم", "أو", "او", "ثم", "كل", "بعد", "قبل",
}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u0640", "")  # tatweel
    text = re.sub(r"[\u064B-\u0652]", "", text)  # Arabic diacritics
    return text.lower()


def tokens(text: str, keep_stopwords: bool = False) -> list[str]:
    found = _TOKEN.findall(normalise(text))
    if keep_stopwords:
        return found
    return [t for t in found if t not in _STOPWORDS and len(t) > 1]


def _stem(token: str) -> str:
    """Crude suffix trimming — enough to match ship/shipping, delay/delayed."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _stems(text: str) -> set[str]:
    return {_stem(t) for t in tokens(text)}


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(normalise(text))}


@dataclass
class GroundingOptions:
    min_overlap: float = 0.55
    retrieve_k: int = 3
    number_penalty: float = 0.45
    """A claim carrying a figure absent from its evidence loses this much score."""


@dataclass
class GroundingResult:
    utterance_ids: list[str]
    score: float
    verified: bool
    reason: str = ""


class Retriever:
    """Tiny IDF-weighted lexical retriever over the transcript.

    Lexical, not embedding-based, on purpose: it needs no model download, adds
    no latency to a request that has already run two neural models, and for
    "find the line this sentence came from" the vocabulary overlap is high.
    """

    def __init__(self, utterances: list[dict]):
        self.utterances = utterances
        self._index: list[tuple[str, set[str]]] = [(u["id"], _stems(u.get("text", ""))) for u in utterances]
        document_frequency: dict[str, int] = {}
        for _, terms in self._index:
            for term in terms:
                document_frequency[term] = document_frequency.get(term, 0) + 1
        total = max(1, len(self._index))
        self._idf = {term: math.log(1 + total / freq) for term, freq in document_frequency.items()}

    def search(self, claim: str, k: int = 3) -> list[tuple[str, float]]:
        query = _stems(claim)
        if not query:
            return []
        scored: list[tuple[str, float]] = []
        for utterance_id, terms in self._index:
            shared = query & terms
            if not shared:
                continue
            weight = sum(self._idf.get(term, 1.0) for term in shared)
            norm = sum(self._idf.get(term, 1.0) for term in query) or 1.0
            scored.append((utterance_id, weight / norm))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


def coverage(claim: str, evidence: str, opts: GroundingOptions) -> tuple[float, str]:
    """Share of the claim's content supported by the evidence text."""
    claim_terms = _stems(claim)
    if not claim_terms:
        return 0.0, "claim has no content words"
    evidence_terms = _stems(evidence)
    if not evidence_terms:
        return 0.0, "no evidence text"

    hit = len(claim_terms & evidence_terms) / len(claim_terms)

    claim_numbers = numbers_in(claim)
    missing_numbers = claim_numbers - numbers_in(evidence)
    if missing_numbers:
        hit = max(0.0, hit - opts.number_penalty)
        return hit, f"figure not in evidence: {', '.join(sorted(missing_numbers))}"
    return hit, ""


def ground_claim(
    claim: str,
    cited_ids: list[str] | None,
    by_id: dict[str, dict],
    retriever: Retriever,
    opts: GroundingOptions | None = None,
) -> GroundingResult:
    opts = opts or GroundingOptions()
    valid = [cid for cid in (cited_ids or []) if cid in by_id]
    invented = [cid for cid in (cited_ids or []) if cid not in by_id]

    if valid:
        evidence = " ".join(by_id[cid].get("text", "") for cid in valid)
        score, reason = coverage(claim, evidence, opts)
        if score >= opts.min_overlap:
            note = f"cited id not in transcript: {', '.join(invented)}" if invented else reason
            return GroundingResult(valid, round(score, 3), True, note)

    # Either nothing was cited, or what was cited does not support the claim.
    candidates = retriever.search(claim, opts.retrieve_k)
    if not candidates:
        return GroundingResult([], 0.0, False, "no supporting utterance found")

    best_ids = [cid for cid, _ in candidates]
    evidence = " ".join(by_id[cid].get("text", "") for cid in best_ids if cid in by_id)
    score, reason = coverage(claim, evidence, opts)
    verified = score >= opts.min_overlap
    if verified:
        # Keep only the utterances that carry the claim, not all three.
        kept = [cid for cid in best_ids if coverage(claim, by_id[cid].get("text", ""), opts)[0] > 0.15] or best_ids[:1]
        return GroundingResult(kept, round(score, 3), True, "evidence recovered by retrieval")
    return GroundingResult(best_ids[:1], round(score, 3), False, reason or "evidence too weak")


def verify_brief(brief: dict, utterances: list[dict], opts: GroundingOptions | None = None) -> tuple[dict, dict]:
    """Check every claim in a generated brief. Returns (brief, stats).

    Unsupported items are marked `verified: false`; the caller decides whether
    to drop them (DROP_UNGROUNDED_CLAIMS) or show them flagged.
    """
    opts = opts or GroundingOptions()
    by_id = {u["id"]: u for u in utterances}
    retriever = Retriever(utterances)
    grounded = dropped = 0

    def check(item: dict) -> dict:
        nonlocal grounded, dropped
        result = ground_claim(item.get("text", ""), item.get("utterance_ids"), by_id, retriever, opts)
        item["evidence"] = {
            "utterance_ids": result.utterance_ids,
            "grounding": result.score,
            "verified": result.verified,
        }
        if result.reason:
            item["evidence"]["note"] = result.reason
        item.pop("utterance_ids", None)
        if result.verified:
            grounded += 1
        else:
            dropped += 1
        return item

    for key in ("key_points", "decisions", "action_items"):
        brief[key] = [check(dict(item)) for item in brief.get(key) or []]

    summary_result = ground_claim(
        brief.get("summary", ""), brief.get("summary_utterance_ids"), by_id, retriever, opts
    )
    brief["summary_evidence"] = {
        "utterance_ids": summary_result.utterance_ids,
        "grounding": summary_result.score,
        "verified": summary_result.verified,
    }
    brief.pop("summary_utterance_ids", None)

    return brief, {"grounded": grounded, "ungrounded": dropped}


def drop_ungrounded(brief: dict) -> tuple[dict, int]:
    removed = 0
    for key in ("key_points", "decisions", "action_items"):
        kept = []
        for item in brief.get(key) or []:
            if item.get("evidence", {}).get("verified"):
                kept.append(item)
            else:
                removed += 1
        brief[key] = kept
    brief["dropped_claims"] = removed
    return brief, removed
