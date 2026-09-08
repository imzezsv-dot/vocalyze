"""The seams where the team's real models plug in.

These run without Whisper, pyannote or torch installed — the adapters are
driven with stand-ins that return the shapes the real libraries return. That
is the point: the failure modes worth catching here are the ones that appear
on a version bump or a missing token, and waiting to discover them on a GPU
host during a demo is too late.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from app.config import Settings
from app.pipeline.diarization import PyannoteDiarizer, _annotation_of


# ---------------------------------------------------------------- pyannote
class FakeSegment:
    def __init__(self, start, end):
        self.start, self.end = start, end


class FakeAnnotation:
    """What pyannote 3.x returns from a pipeline call."""

    def __init__(self, tracks):
        self._tracks = tracks

    def itertracks(self, yield_label=False):
        for start, end, speaker in self._tracks:
            yield FakeSegment(start, end), None, speaker


class FakeDiarizeOutput:
    """What pyannote 4.x returns: the annotation is one field of a result."""

    def __init__(self, annotation):
        self.speaker_diarization = annotation


TRACKS = [(0.0, 2.0, "SPEAKER_00"), (2.0, 4.5, "SPEAKER_01"), (4.5, 4.5, "SPEAKER_02")]


def test_the_three_x_return_shape_is_read():
    annotation = _annotation_of(FakeAnnotation(TRACKS))
    assert list(annotation.itertracks(yield_label=True))


def test_the_four_x_return_shape_is_read():
    """Calling .itertracks on the 4.x object raises AttributeError, which the
    orchestrator records as a failed diarization — every speaker label lost on
    a version bump alone."""
    inner = FakeAnnotation(TRACKS)
    assert _annotation_of(FakeDiarizeOutput(inner)) is inner


@pytest.mark.parametrize(
    "output",
    [FakeAnnotation(TRACKS), FakeDiarizeOutput(FakeAnnotation(TRACKS))],
    ids=["pyannote-3.x", "pyannote-4.x"],
)
def test_both_shapes_produce_the_same_turns(output, monkeypatch):
    diarizer = PyannoteDiarizer(Settings())
    monkeypatch.setattr(diarizer, "_load", lambda: (lambda _path, **_kw: output))

    result = diarizer.diarize(Path("/dev/null"))

    assert [(t.start, t.end, t.speaker) for t in result.turns] == [
        (0.0, 2.0, "SPEAKER_00"),
        (2.0, 4.5, "SPEAKER_01"),
    ]  # the zero-length third turn is dropped
    assert result.num_speakers == 2
    assert result.backend == "pyannote"


def test_a_missing_token_says_what_to_do_about_it(monkeypatch):
    """A raw 401 from the hub is the single most common way this deployment
    fails, and it does not explain itself."""
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    diarizer = PyannoteDiarizer(Settings())

    pytest.importorskip("pyannote.audio", reason="the token check runs after the import check")
    with pytest.raises(RuntimeError, match="Hugging Face token"):
        diarizer._load()


def test_the_scripted_diarizer_returns_the_fixture_whatever_the_audio():
    """Why a real transcription must never be paired with it: the turns come
    from the scripted meeting, so over somebody's own recording they become
    confident speaker names invented from a fixture."""
    from app.pipeline.diarization import MockDiarizer

    first = MockDiarizer().diarize(Path("/some/recording.wav"))
    second = MockDiarizer().diarize(Path("/a/completely/different.wav"))

    assert [(t.start, t.speaker) for t in first.turns] == [(t.start, t.speaker) for t in second.turns]
    assert first.turns, "the fixture does carry turns — that is the hazard"


def test_no_diarization_says_nothing_rather_than_inventing_speakers():
    """The setting for real audio with no speaker model. Every line ends up
    UNKNOWN, which is true, instead of carrying a fixture's label."""
    from app.pipeline.diarization import NoDiarizer

    result = NoDiarizer().diarize(Path("/some/recording.wav"))
    assert result.turns == []
    assert result.num_speakers == 0
    assert result.backend == "none"


def test_real_speech_with_no_diarizer_comes_back_unattributed():
    """End to end through the aligner: words are kept, speakers are not guessed."""
    from app.pipeline.alignment import build_transcript
    from app.pipeline.diarization import NoDiarizer
    from app.schemas import ASRResult, ASRSegment, Word

    asr = ASRResult(
        language="en", duration=2.4,
        segments=[ASRSegment(
            start=0.0, end=2.4, text="Right, let's start.", confidence=0.8,
            words=[Word(start=0.0, end=0.8, text="Right,", confidence=0.9),
                   Word(start=0.8, end=2.4, text="let's start.", confidence=0.9)],
        )],
        model="small", backend="faster-whisper",
    )

    transcript, _stats = build_transcript(asr, NoDiarizer().diarize(Path("/dev/null")))

    # UNKNOWN is listed so the interface can render an "Unidentified" group.
    # What matters is that no real speaker was invented alongside it.
    assert transcript.speakers == ["UNKNOWN"]
    assert {u.speaker for u in transcript.utterances} == {"UNKNOWN"}
    assert " ".join(u.text for u in transcript.utterances) == "Right, let's start."


def test_the_notebook_never_pairs_real_speech_with_scripted_speakers():
    """The demo cells transcribe real audio. Falling back to the scripted
    diarizer there would put invented speaker names on a real recording."""
    import json

    notebook = json.loads((ROOT / "notebooks" / "Vocalyze.ipynb").read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        source = "".join(cell["source"])
        if 'ASR_BACKEND="whisper"' not in source and 'ASR_BACKEND"] = "whisper"' not in source:
            continue
        assert 'DIARIZATION_BACKEND"] = "mock"' not in source, (
            "a cell running real Whisper falls back to the scripted diarizer"
        )
        assert 'DIARIZATION_BACKEND="mock"' not in source, (
            "a cell running real Whisper falls back to the scripted diarizer"
        )


def test_a_diarizer_that_fails_costs_labels_but_not_the_meeting(client):
    """The orchestrator catches a diarization failure and carries on. Verified
    end to end here because it is the guarantee a reviewer will ask about."""
    from app.pipeline import registry

    class BrokenDiarizer:
        name = "pyannote"

        def diarize(self, audio_path, num_speakers=None):
            raise RuntimeError("hub returned 401")

    registry._cache["diarizer"] = BrokenDiarizer()
    try:
        accepted = client.post(
            "/v1/jobs",
            files={"file": ("meeting.wav", b"RIFF$\x00\x00\x00WAVEfmt ", "audio/wav")},
            data={"consent": "true"},
        ).json()

        import time

        headers = {"Authorization": f"Bearer {accepted['access_token']}"}
        for _ in range(200):
            summary = client.get(f"/v1/jobs/{accepted['job_id']}", headers=headers).json()
            if summary["state"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        assert summary["state"] == "completed", "a failed diarizer must not fail the job"
        result = client.get(f"/v1/jobs/{accepted['job_id']}/result", headers=headers).json()
        assert result["transcript"]["utterances"], "the transcript must survive"
        assert result["transcript"]["speakers"] == ["UNKNOWN"]
        stages = {s["name"]: s["state"] for s in result["job"]["stages"]}
        assert stages["diarizing"] == "failed"   # and it says so, rather than hiding it
    finally:
        registry._cache.pop("diarizer", None)


# ----------------------------------------------------------------- Whisper
class FakeFasterWord:
    """faster-whisper: `.word` keeps the leading space, `.probability` is
    already a probability."""

    def __init__(self, start, end, word, probability):
        self.start, self.end, self.word, self.probability = start, end, word, probability


class FakeFasterSegment:
    def __init__(self, start, end, text, words):
        self.start, self.end, self.text, self.words = start, end, text, words
        self.avg_logprob = -0.22          # a log probability, not a probability
        self.no_speech_prob = 0.01


class FakeFasterInfo:
    language, duration = "en", 4.5


class FakeFasterModel:
    def transcribe(self, _path, **_kwargs):
        words = [
            FakeFasterWord(0.0, 0.8, " Right,", 0.94),
            FakeFasterWord(0.8, 1.6, " let's", 0.91),
            FakeFasterWord(1.6, 2.4, " start.", 0.88),
            FakeFasterWord(2.4, 2.4, " ", 0.10),      # an empty word: dropped
        ]
        return iter([FakeFasterSegment(0.0, 2.4, " Right, let's start. ", words)]), FakeFasterInfo()


class FakeOpenAIModel:
    """openai-whisper returns plain dicts, and `word` keeps its leading space."""

    def transcribe(self, _path, **_kwargs):
        return {
            "language": "en",
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.4,
                    "text": " Right, let's start. ",
                    "avg_logprob": -0.22,
                    "no_speech_prob": 0.01,
                    "words": [
                        {"start": 0.0, "end": 0.8, "word": " Right,", "probability": 0.94},
                        {"start": 0.8, "end": 1.6, "word": " let's", "probability": 0.91},
                        {"start": 1.6, "end": 2.4, "word": " start.", "probability": 0.88},
                        {"start": 2.4, "end": 2.4, "word": " ", "probability": 0.10},
                    ],
                }
            ],
        }


@pytest.mark.parametrize(
    "flavour, model",
    [("faster-whisper", FakeFasterModel()), ("openai-whisper", FakeOpenAIModel())],
)
def test_either_whisper_package_produces_the_same_words(flavour, model, monkeypatch):
    """The ASR component may hand this layer either package — the notebook uses
    openai-whisper, the container prefers faster-whisper. Word timings are what
    make speaker attribution accurate, so both paths must yield the same ones."""
    from app.pipeline.asr import WhisperASR

    backend = WhisperASR(Settings())
    monkeypatch.setattr(backend, "_load", lambda: model)
    monkeypatch.setattr(backend, "_flavour", flavour, raising=False)
    backend._flavour = flavour

    result = backend.transcribe(Path("/dev/null"))

    assert len(result.segments) == 1
    words = result.segments[0].words
    assert [(w.start, w.end, w.text) for w in words] == [
        (0.0, 0.8, "Right,"),      # the leading space is stripped, not carried
        (0.8, 1.6, "let's"),
        (1.6, 2.4, "start."),
    ]  # the whitespace-only word is dropped rather than becoming an empty token
    assert result.segments[0].text == "Right, let's start."


def test_a_segment_log_probability_is_read_as_a_confidence(monkeypatch):
    """avg_logprob is a *log* probability. Passing it through unconverted would
    put a negative number in a field the UI renders as a percentage."""
    from app.pipeline.asr import WhisperASR

    backend = WhisperASR(Settings())
    monkeypatch.setattr(backend, "_load", lambda: FakeFasterModel())
    backend._flavour = "faster-whisper"

    segment = backend.transcribe(Path("/dev/null")).segments[0]
    assert 0.0 <= segment.confidence <= 1.0
    assert round(segment.confidence, 2) == 0.80        # exp(-0.22)
    assert segment.words[0].confidence == 0.94         # already a probability: unchanged


def test_whisper_words_reach_the_aligner_as_attributed_speech():
    """The contract end to end: what the ASR component returns, merged with what
    the diarization component returns, with no model installed on either side."""
    from app.pipeline.alignment import build_transcript
    from app.schemas import ASRResult, ASRSegment, DiarizationResult, SpeakerTurn, Word

    asr = ASRResult(
        language="en",
        duration=2.4,
        segments=[
            ASRSegment(
                start=0.0, end=2.4, text="Right, let's start.", confidence=0.8,
                words=[
                    Word(start=0.0, end=0.8, text="Right,", confidence=0.94),
                    Word(start=0.8, end=1.6, text="let's", confidence=0.91),
                    Word(start=1.6, end=2.4, text="start.", confidence=0.88),
                ],
            )
        ],
        model="small", backend="faster-whisper",
    )
    # The turn boundary lands mid-word, which is what pyannote actually does:
    # "let's" (0.8-1.6) is 0.2s inside the first turn and 0.6s inside the
    # second. Majority overlap gives it to the second speaker rather than
    # splitting the word or matching on the boundary.
    turns = DiarizationResult(
        turns=[
            SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00"),
            SpeakerTurn(start=1.0, end=2.4, speaker="SPEAKER_01"),
        ],
        num_speakers=2, backend="pyannote", model="speaker-diarization-3.1",
    )

    transcript, _stats = build_transcript(asr, turns)

    assert transcript.speakers == ["SPEAKER_00", "SPEAKER_01"]
    assert [u.text for u in transcript.utterances] == ["Right,", "let's start."]
    assert [u.speaker for u in transcript.utterances] == ["SPEAKER_00", "SPEAKER_01"]


# ----------------------------------------------------------- the container
DOCKERFILE = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")


def test_the_image_installs_the_model_dependencies():
    """Without this the image boots, accepts ASR_BACKEND=whisper, and then
    fails the first upload with 'needs a Whisper package'."""
    assert "requirements-models.txt" in DOCKERFILE


def test_the_image_runs_as_the_uid_hugging_face_uses():
    assert "useradd -m -u 1000" in DOCKERFILE
    assert "USER vocalyze" in DOCKERFILE


def test_the_image_transcribes_real_audio_by_default():
    """This image is the always-on deployment people are shown. A visitor who
    uploads their own recording and is handed the scripted sample meeting has
    been shown nothing, so the real backend has to be the default here — the
    scripted one stays available through ASR_BACKEND=mock."""
    assert "ASR_BACKEND=whisper" in DOCKERFILE
    assert "SUMMARIZER_BACKEND=extractive" in DOCKERFILE


def test_the_image_bakes_the_whisper_weights_in():
    """Downloading weights on first use means the first visitor waits minutes on
    what looks like a hang. Baking them costs image size once."""
    assert "faster_whisper import WhisperModel" in DOCKERFILE
    assert "ARG WHISPER_MODEL=base" in DOCKERFILE, (
        "a free Space is 2 shared cores: int8 `base` beats real time there, "
        "`small` roughly matches it, `large-v3` is far slower"
    )


def test_the_image_caps_uploads_to_what_two_cores_can_finish():
    """A refusal that says why beats a progress bar that never moves."""
    assert "MAX_DURATION_MINUTES=15" in DOCKERFILE
    assert "MAX_UPLOAD_MB=50" in DOCKERFILE


def test_the_image_does_not_default_to_a_gated_model():
    """pyannote needs a token and per-account licence acceptance. Defaulting to
    it would make every fresh deployment fail its first diarization."""
    assert "DIARIZATION_BACKEND=mock" in DOCKERFILE


def test_the_image_writes_only_where_that_user_can():
    """A free Space has no /data — that is paid persistent storage — and a
    process that cannot write its own cache cannot download a model."""
    assert "DATA_DIR=/data" not in DOCKERFILE
    for cache in ("HF_HOME=", "TORCH_HOME=", "XDG_CACHE_HOME="):
        assert cache in DOCKERFILE
    assert DOCKERFILE.count("/home/vocalyze") >= 4
