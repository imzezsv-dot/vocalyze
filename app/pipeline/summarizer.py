"""Meeting brief generation — the seam where the LLM component plugs in.

Two rules hold whatever backend is used.

*Every claim cites its evidence.* The model is required to return utterance
ids alongside each point, and `grounding_core` checks them afterwards. Asking
for citations makes the model retrieve rather than recall, and it gives the
verifier something to check. Prompting alone is not treated as sufficient.

*The transcript leaves the process only if the operator allowed it.* Sending a
meeting to a third-party API is a disclosure of personal data, so external
endpoints are refused unless ALLOW_EXTERNAL_LLM is on. A loopback or
private-network endpoint (Ollama, vLLM, an on-prem server) is always allowed.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from ..config import Settings
from ..core.logging import get_logger

log = get_logger("vocalyze.summarizer")

SYSTEM_PROMPT = """You summarise meeting transcripts. Every line of the transcript is labelled with an id like [u7].

Rules:
- Use only what the transcript says. Never add background knowledge, never infer what people probably meant.
- Every point you write must cite the ids of the transcript lines it came from.
- Numbers, names and dates must appear verbatim in the lines you cite.
- If the meeting contains no decisions or no action items, return an empty list. An empty list is a correct answer.
- Write plainly, in the language of the transcript.

Return one JSON object, no prose around it:
{
  "summary": "3-5 sentences covering what the meeting was about and what came out of it",
  "summary_utterance_ids": ["u1", "u4"],
  "key_points": [{"text": "...", "utterance_ids": ["u2"]}],
  "decisions": [{"text": "...", "utterance_ids": ["u9"]}],
  "action_items": [{"text": "...", "owner": "name or null", "due": "when or null", "utterance_ids": ["u12"]}]
}"""


class SummarizerBackend(Protocol):
    name: str

    def summarize(self, utterances: list[dict], language: str | None = None) -> dict: ...


# ---------------------------------------------------------------------------
# Extractive fallback — no model required
# ---------------------------------------------------------------------------
_DECISION = re.compile(
    r"\b(agreed|we agree|let's do|lets do|we will go with|we'll go with|decided|the decision|"
    r"consider it|approved|that is settled|that's settled|we flag|we'll flag|we ship|we submit)\b",
    re.IGNORECASE,
)
_COMMITMENT = re.compile(
    r"\b(i'll|i will|i can have|will wire|will own|will write|will send|will run|will prepare|"
    r"you write|needs to|need to)\b",
    re.IGNORECASE,
)
_OWNER = re.compile(r"\b([A-Z][a-z]{2,12})\s*,?\s+(?:you\s+\w+|will|'ll|is going to|owns|takes|writes)\b")
_DUE = re.compile(
    r"\b(?:by|before|on|ready)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|"
    r"next week|today)\b",
    re.IGNORECASE,
)
_INFORMATIVE = re.compile(r"\d|\b(percent|hours|error rate|dataset|split|model|overlap|report|format)\b", re.I)


class ExtractiveSummarizer:
    """Selects real sentences from the transcript instead of generating new ones.

    Used in demo mode, and as the fallback when the LLM is unreachable: a brief
    built from the speakers' own words cannot hallucinate, so a degraded run
    still produces something trustworthy rather than nothing.
    """

    name = "extractive"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings

    def summarize(self, utterances: list[dict], language: str | None = None) -> dict:
        usable = [u for u in utterances if len(u.get("text", "").split()) >= 4]
        if not usable:
            return {"summary": "", "summary_utterance_ids": [], "key_points": [], "decisions": [], "action_items": []}

        # Commitments are checked first. "Let's do that. Rima will wire the
        # evidence check in before Thursday" is a decision *and* an action, and
        # the action is the more useful of the two to surface with an owner.
        actions, decisions, taken = [], [], set()
        for utterance in usable:
            text = utterance["text"]
            if (_COMMITMENT.search(text) or _OWNER.search(text)) and len(actions) < 5:
                actions.append(utterance)
                taken.add(utterance["id"])
            elif _DECISION.search(text) and len(decisions) < 5:
                decisions.append(utterance)
                taken.add(utterance["id"])

        points = sorted(
            (u for u in usable if u["id"] not in taken),
            key=lambda u: (len(_INFORMATIVE.findall(u["text"])), len(u["text"])),
            reverse=True,
        )[:5]
        points.sort(key=lambda u: u["start"])

        # Summary: what the meeting was about, then what came out of it. Sources
        # are deduplicated, so a line that is both context and decision is used
        # once rather than printed twice.
        sources, seen = [], set()
        for utterance in (points[:1] or usable[:1]) + decisions[:2] + actions[:1]:
            if utterance["id"] not in seen:
                seen.add(utterance["id"])
                sources.append(utterance)
        sources.sort(key=lambda u: u["start"])
        summary = " ".join(_first_sentence(u["text"]) for u in sources)

        return {
            "summary": summary,
            "summary_utterance_ids": [u["id"] for u in sources],
            "key_points": [{"text": _first_sentence(u["text"]), "utterance_ids": [u["id"]]} for u in points],
            "decisions": [{"text": _first_sentence(u["text"]), "utterance_ids": [u["id"]]} for u in decisions],
            "action_items": [
                {
                    "text": _first_sentence(u["text"]),
                    "owner": _owner_of(u["text"]),
                    "due": _due_of(u["text"]),
                    "utterance_ids": [u["id"]],
                }
                for u in actions
            ],
        }


def _owner_of(text: str) -> str | None:
    match = _OWNER.search(text)
    if match:
        return match.group(1)
    return "the speaker" if re.search(r"\b(i'll|i will|i can)\b", text, re.IGNORECASE) else None


def _due_of(text: str) -> str | None:
    match = _DUE.search(text)
    return match.group(1).title() if match else None


def _first_sentence(text: str, limit: int = 220) -> str:
    parts = re.split(r"(?<=[.!?؟])\s+", text.strip())
    sentence = parts[0] if parts else text
    if len(sentence) < 40 and len(parts) > 1:
        sentence = " ".join(parts[:2])
    return sentence[:limit].strip()


# ---------------------------------------------------------------------------
# LLM backend (OpenAI-compatible chat completions)
# ---------------------------------------------------------------------------
class ExternalCallBlocked(RuntimeError):
    pass


class LLMSummarizer:
    name = "llm"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        self.fallback = ExtractiveSummarizer(settings)
        _assert_egress_allowed(self.base_url, settings.allow_external_llm)

    def summarize(self, utterances: list[dict], language: str | None = None) -> dict:
        transcript = "\n".join(f"[{u['id']}] {u['speaker']}: {u['text']}" for u in utterances)
        body = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Transcript:\n{transcript}"},
            ],
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.llm_api_key or 'not-needed'}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            log.warning("LLM unreachable (%s); falling back to the extractive brief", exc.__class__.__name__)
            brief = self.fallback.summarize(utterances, language)
            brief["degraded"] = "llm_unreachable"
            return brief

        content = payload["choices"][0]["message"]["content"]
        try:
            return _parse_json(content)
        except ValueError:
            log.warning("LLM returned unparseable JSON; falling back to the extractive brief")
            brief = self.fallback.summarize(utterances, language)
            brief["degraded"] = "llm_bad_json"
            return brief


def _parse_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(content[start : end + 1])


def _assert_egress_allowed(base_url: str, allow_external: bool) -> None:
    """Refuse to send a transcript off the machine unless that was configured."""
    if allow_external:
        return
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host in {"localhost", "::1"}:
        return
    try:
        address = ipaddress.ip_address(socket.gethostbyname(host))
    except (ValueError, socket.gaierror):
        raise ExternalCallBlocked(
            f"ALLOW_EXTERNAL_LLM is off, so the transcript cannot be sent to {host}. "
            "Point LLM_BASE_URL at a local model, or set ALLOW_EXTERNAL_LLM=true if participants "
            "consented to third-party processing."
        ) from None
    if not (address.is_loopback or address.is_private):
        raise ExternalCallBlocked(
            f"ALLOW_EXTERNAL_LLM is off, so the transcript cannot be sent to {host} ({address}). "
            "Point LLM_BASE_URL at a local model, or set ALLOW_EXTERNAL_LLM=true if participants "
            "consented to third-party processing."
        )
