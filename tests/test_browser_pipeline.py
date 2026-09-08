"""The browser port must agree with the implementation it mirrors.

`app/web/pipeline.js` re-implements grounding, redaction and the extractive
summariser so a static host can run the whole pipeline in the page. Two copies
of one algorithm drift; the only thing that stops it is a test that runs both
on the same input and compares.

Skipped when node is not installed, so the suite still runs everywhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_JS = ROOT / "app" / "web" / "pipeline.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_js(body: str):
    """Evaluate a snippet against pipeline.js and return its JSON result."""
    script = f"""
    const pipeline = require({str(PIPELINE_JS)!r});
    const result = (() => {{ {body} }})();
    process.stdout.write(JSON.stringify(result));
    """
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    if completed.returncode != 0:
        raise AssertionError(f"node failed:\n{completed.stderr}")
    return json.loads(completed.stdout)


# ------------------------------------------------------------------ tokens
def test_tokenisation_matches():
    from app.pipeline.grounding_core import tokens

    samples = [
        "Word error rate is 11.2 percent on the clean split.",
        "We agreed to flag overlapping speech instead of forcing a single speaker.",
        "الميزانية 250000 ريال والإصدار 3.11.9",
    ]
    for text in samples:
        assert run_js(f"return pipeline.tokens({text!r});") == tokens(text), text


def test_number_extraction_matches():
    from app.pipeline.grounding_core import numbers_in

    for text in ["11.2 percent and 250,000 riyals", "version 3.11.9", "no digits here"]:
        assert sorted(run_js(f"return [...pipeline.numbersIn({text!r})];")) == sorted(numbers_in(text))


# --------------------------------------------------------------- grounding
GROUNDING_LINES = [
    {"id": "u1", "text": "Word error rate is eleven point two percent on the clean split."},
    {"id": "u2", "text": "We agreed to flag overlapping speech instead of forcing a single speaker."},
    {"id": "u3", "text": "Rima will wire the evidence check into the pipeline before Thursday."},
]

GROUNDING_CASES = [
    ("Word error rate is eleven point two percent on the clean split", ["u1"]),
    ("Word error rate is 47 percent on the clean split", ["u1"]),
    ("The team decided to cancel the project", ["u2"]),
    ("Overlapping speech is flagged instead of forcing one speaker", None),
    ("Rima will wire the evidence check in", ["u9"]),          # a cited id that does not exist
    ("", ["u1"]),                                               # nothing to ground
]


@pytest.mark.parametrize("claim, cited", GROUNDING_CASES)
def test_grounding_verdicts_match(claim, cited):
    """The verdict, the score and the evidence ids all have to agree — a port
    that accepts the same claims but cites different lines is still broken."""
    from app.pipeline.grounding_core import Retriever, ground_claim

    by_id = {u["id"]: u for u in GROUNDING_LINES}
    expected = ground_claim(claim, cited, by_id, Retriever(GROUNDING_LINES))

    actual = run_js(
        f"const lines = {json.dumps(GROUNDING_LINES)};"
        "const byId = Object.fromEntries(lines.map((u) => [u.id, u]));"
        f"return pipeline.groundClaim({json.dumps(claim)}, {json.dumps(cited)}, byId,"
        " new pipeline.Retriever(lines));"
    )

    assert actual["verified"] is expected.verified
    assert actual["utterance_ids"] == expected.utterance_ids
    assert actual["score"] == pytest.approx(expected.score, abs=0.001)


# --------------------------------------------------------------- redaction
REDACTION_CASES = [
    "Email me at layla.ahmed@example.com or call +966 50 123 4567",
    "Error rate is 11.2 percent, the budget is 250000 riyals, and we shipped 3.11.9",
    "the budget is 12 000 000 riyals this year",
    "server 192.168.1.20 and card 4111 1111 1111 1111",
    "meeting room 4 at 10 30 on 12 03 2026",
    "see https://example.com/report for the numbers",
]


@pytest.mark.parametrize("text", REDACTION_CASES)
def test_redaction_matches(text):
    """Precision matters as much as recall here: over-redaction destroys the
    meeting, so both ports must leave the same budgets and versions alone."""
    from app.pipeline.redaction import redact_text

    expected_text, expected_report = redact_text(text)
    actual = run_js(f"const [t, r] = pipeline.redactText({text!r}); return {{ text: t, report: r }};")

    assert actual["text"] == expected_text
    assert actual["report"]["count"] == expected_report.count
    assert actual["report"]["by_kind"] == expected_report.by_kind


# -------------------------------------------------------------- summariser
def _fixture_utterances():
    from app.pipeline.alignment import to_dicts
    from app.pipeline.asr import MockASR
    from app.pipeline.alignment import build_transcript
    from app.pipeline.diarization import MockDiarizer

    transcript, _stats = build_transcript(MockASR().transcribe(Path("/dev/null")), MockDiarizer().diarize(Path("/dev/null")))
    return to_dicts(transcript)


def test_the_extractive_brief_matches_on_the_real_fixture():
    """The scripted meeting is the same input both sides get. If the two
    summarisers pick different sentences, the browser build is showing a
    different brief from the one the service and the tests are held to."""
    from app.pipeline.summarizer import ExtractiveSummarizer

    utterances = _fixture_utterances()
    expected = ExtractiveSummarizer().summarize(utterances)
    actual = run_js(f"return pipeline.summarizeExtractive({json.dumps(utterances)});")

    assert actual["summary"] == expected["summary"]
    assert actual["summary_utterance_ids"] == expected["summary_utterance_ids"]
    for key in ("key_points", "decisions", "action_items"):
        assert [i["text"] for i in actual[key]] == [i["text"] for i in expected[key]], key
        assert [i["utterance_ids"] for i in actual[key]] == [i["utterance_ids"] for i in expected[key]], key

    assert [i.get("owner") for i in actual["action_items"]] == [i.get("owner") for i in expected["action_items"]]
    assert [i.get("due") for i in actual["action_items"]] == [i.get("due") for i in expected["action_items"]]


def test_the_verified_brief_matches_on_the_real_fixture():
    """End to end: summarise, verify every claim, drop what is unsupported."""
    from app.pipeline.grounding_core import drop_ungrounded, verify_brief
    from app.pipeline.summarizer import ExtractiveSummarizer

    utterances = _fixture_utterances()
    verified, stats = verify_brief(dict(ExtractiveSummarizer().summarize(utterances)), utterances)
    verified, dropped = drop_ungrounded(verified)

    actual = run_js(
        f"const u = {json.dumps(utterances)};"
        "const raw = pipeline.summarizeExtractive(u);"
        "let [brief, stats] = pipeline.verifyBrief(raw, u);"
        "let removed; [brief, removed] = pipeline.dropUngrounded(brief);"
        "return { brief, stats, removed };"
    )

    assert actual["removed"] == dropped
    assert actual["stats"]["grounded"] == stats["grounded"]
    for key in ("key_points", "decisions", "action_items"):
        assert [i["text"] for i in actual["brief"][key]] == [i["text"] for i in verified[key]], key
        assert [i["evidence"]["utterance_ids"] for i in actual["brief"][key]] == [
            i["evidence"]["utterance_ids"] for i in verified[key]
        ], key


def test_a_transcript_with_no_diarization_says_unknown_rather_than_guessing():
    """Calling a four-person meeting one speaker would be a fabrication, and
    this project is an argument against those."""
    result = run_js(
        "return pipeline.runPipeline(["
        "{start: 0.0, end: 4.0, text: 'Alright, let us start with where the dataset stands.'},"
        "{start: 4.0, end: 9.0, text: 'AMI and ICSI are both downloaded and cleaned this week.'}"
        "], { speakerTurns: null });"
    )
    assert result["transcript"]["speakers"] == []
    assert {u["speaker"] for u in result["transcript"]["utterances"]} == {"UNKNOWN"}
    assert result["quality"]["speaker_count"] == 0
