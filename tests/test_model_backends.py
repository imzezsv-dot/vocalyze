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


# ----------------------------------------------------------- the container
DOCKERFILE = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")


def test_the_image_installs_the_model_dependencies():
    """Without this the image boots, accepts ASR_BACKEND=whisper, and then
    fails the first upload with 'needs a Whisper package'."""
    assert "requirements-models.txt" in DOCKERFILE


def test_the_image_runs_as_the_uid_hugging_face_uses():
    assert "useradd -m -u 1000" in DOCKERFILE
    assert "USER vocalyze" in DOCKERFILE


def test_the_image_writes_only_where_that_user_can():
    """A free Space has no /data — that is paid persistent storage — and a
    process that cannot write its own cache cannot download a model."""
    assert "DATA_DIR=/data" not in DOCKERFILE
    for cache in ("HF_HOME=", "TORCH_HOME=", "XDG_CACHE_HOME="):
        assert cache in DOCKERFILE
    assert DOCKERFILE.count("/home/vocalyze") >= 4
