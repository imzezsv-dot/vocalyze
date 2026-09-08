"""The privacy guarantees, as assertions rather than prose.

Each test here corresponds to a sentence in docs/PRIVACY.md. If a claim in
that document cannot be written as a test, it should not be in the document.
"""

from __future__ import annotations

import json

import pytest

from app.core.audit import AuditLog
from app.core.crypto import Sealer, SealerError, generate_service_key, new_data_key
from app.core.retention import RetentionSweeper
from app.pipeline.summarizer import ExternalCallBlocked, _assert_egress_allowed
from app.schemas import JobState


# ------------------------------------------------------------- encryption
def test_stored_bytes_are_unreadable_without_the_key(store):
    record, _token, data_key = store.create(filename="m.wav", size_bytes=10, privacy={"consent": True})
    store.write_blob(record.id, "audio.enc", b"the actual meeting audio", data_key)

    on_disk = (store.settings.jobs_dir / record.id / "audio.enc").read_bytes()
    assert b"the actual meeting audio" not in on_disk
    assert store.read_blob(record.id, "audio.enc", data_key) == b"the actual meeting audio"


def test_a_blob_cannot_be_moved_between_jobs(store):
    first, _t1, key_a = store.create(filename="a.wav", size_bytes=1, privacy={})
    second, _t2, _key_b = store.create(filename="b.wav", size_bytes=1, privacy={})
    store.write_blob(first.id, "audio.enc", b"secret", key_a)

    stolen = (store.settings.jobs_dir / first.id / "audio.enc").read_bytes()
    (store.settings.jobs_dir / second.id / "audio.enc").write_bytes(stolen)

    with pytest.raises(Exception):
        store.read_blob(second.id, "audio.enc", key_a)


def test_each_job_gets_its_own_data_key(store):
    first, _t1, key_a = store.create(filename="a.wav", size_bytes=1, privacy={})
    second, _t2, key_b = store.create(filename="b.wav", size_bytes=1, privacy={})
    assert key_a != key_b
    assert store.data_key(first) == key_a
    assert store.data_key(second) == key_b


def test_encryption_refuses_to_start_without_a_key():
    with pytest.raises(SealerError):
        Sealer.from_settings(None, enabled=True)
    with pytest.raises(SealerError):
        Sealer.from_settings("not-a-32-byte-key", enabled=True)


def test_a_wrapped_key_will_not_unwrap_under_another_job_id():
    sealer = Sealer.from_settings(generate_service_key(), enabled=True)
    wrapped = sealer.wrap(new_data_key(), b"job-a")
    with pytest.raises(Exception):
        sealer.unwrap(wrapped, b"job-b")


# ---------------------------------------------------------------- erasure
def test_erasure_destroys_the_key_and_the_blobs(store):
    record, _token, data_key = store.create(filename="m.wav", size_bytes=4, privacy={"consent": True})
    store.write_blob(record.id, "audio.enc", b"audio", data_key)
    store.write_json(record.id, "result.enc", {"transcript": "text"}, data_key)

    store.purge(record.id)

    after = store.get(record.id)
    assert after.state is JobState.purged
    assert after.wrapped_key == ""          # the key is gone, so any copy stays ciphertext
    assert not store.blob_exists(record.id, "audio.enc")
    assert not store.blob_exists(record.id, "result.enc")


def test_a_purged_job_keeps_no_privacy_detail(store):
    record, _token, _key = store.create(
        filename="m.wav", size_bytes=4, privacy={"consent": True, "retention_hours": 2}
    )
    store.purge(record.id)
    assert set(store.get(record.id).privacy) == {"purged_at"}


# -------------------------------------------------------------- retention
def test_the_sweeper_purges_a_job_past_its_window(store, tmp_path):
    from datetime import datetime, timedelta, timezone

    record, _token, _key = store.create(filename="m.wav", size_bytes=1, privacy={})
    store.update(record.id, expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

    audit = AuditLog(tmp_path / "audit.log", enabled=True)
    assert RetentionSweeper(store, audit, 3600).sweep_once() == 1
    assert store.get(record.id).state is JobState.purged


def test_the_sweeper_leaves_a_live_job_alone(store, tmp_path):
    record, _token, _key = store.create(filename="m.wav", size_bytes=1, privacy={})
    audit = AuditLog(tmp_path / "audit.log", enabled=True)
    assert RetentionSweeper(store, audit, 3600).sweep_once() == 0
    assert store.get(record.id).state is JobState.queued


def test_a_per_job_retention_choice_overrides_the_default(store):
    record, _token, _key = store.create(filename="m.wav", size_bytes=1, privacy={"retention_hours": 1})
    window = record.expires_at - record.created_at
    assert 0.9 < window.total_seconds() / 3600 < 1.1


# ------------------------------------------------------------ access control
def test_the_job_id_alone_grants_nothing(store):
    record, token, _key = store.create(filename="m.wav", size_bytes=1, privacy={})
    assert store.authorise(record.id, "") is None
    assert store.authorise(record.id, "guessed-token") is None
    assert store.authorise(record.id, token) is not None


def test_the_store_keeps_no_usable_copy_of_the_token(store):
    record, token, _key = store.create(filename="m.wav", size_bytes=1, privacy={})
    meta = json.loads((store.settings.jobs_dir / record.id / "meta.json").read_text())
    assert token not in json.dumps(meta)
    assert len(meta["token_hash"]) == 64


def test_a_purged_job_is_not_revealed_to_a_caller_without_the_token(store):
    """Answering 'gone' rather than 'never existed' would confirm to a stranger
    that a recording under this id once existed."""
    record, token, _key = store.create(filename="m.wav", size_bytes=1, privacy={})
    store.purge(record.id)
    assert store.authorise(record.id, "guessed-token") is None
    assert store.authorise(record.id, token) is not None


# ------------------------------------------------------------- audit trail
def test_the_audit_chain_detects_an_edited_line(tmp_path):
    audit = AuditLog(tmp_path / "audit.log", enabled=True)
    for index in range(4):
        audit.record("job.created", job_id=f"j{index}")
    assert audit.verify() == (True, 4)

    lines = (tmp_path / "audit.log").read_text().splitlines()
    entry = json.loads(lines[1])
    entry["details"]["job_id"] = "someone-else"
    lines[1] = json.dumps(entry)
    (tmp_path / "audit.log").write_text("\n".join(lines) + "\n")

    intact, _count = audit.verify()
    assert intact is False


def test_the_audit_chain_detects_a_removed_line(tmp_path):
    audit = AuditLog(tmp_path / "audit.log", enabled=True)
    for index in range(4):
        audit.record("job.created", job_id=f"j{index}")

    lines = (tmp_path / "audit.log").read_text().splitlines()
    del lines[2]
    (tmp_path / "audit.log").write_text("\n".join(lines) + "\n")

    assert audit.verify()[0] is False


def test_the_log_filter_scrubs_identifiers_and_tokens():
    """Logs are the least protected place personal data can land — they are
    tailed, shipped and kept long past any retention window."""
    import logging

    from app.core.logging import RedactionFilter

    def scrubbed(message: str) -> str:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)
        RedactionFilter().filter(record)
        return record.getMessage()

    assert "nora@example.com" not in scrubbed("read by nora@example.com")
    assert "+966501234567" not in scrubbed("called +966501234567 back")
    assert "SA0380000000608010167519" not in scrubbed("paid to SA0380000000608010167519")
    assert scrubbed("Authorization: Bearer abc123.def-456") == "Authorization: Bearer [redacted]"
    assert scrubbed("GET /events?token=s3cr3t-value") == "GET /events?token=[redacted]"
    # and the ordinary case is left readable
    assert scrubbed("job aX9 completed in 4.2s") == "job aX9 completed in 4.2s"


def test_transcript_text_never_reaches_the_audit_log(tmp_path):
    audit = AuditLog(tmp_path / "audit.log", enabled=True)
    audit.record("result.read", job_id="j1", text="what someone actually said", count=3)

    written = (tmp_path / "audit.log").read_text()
    assert "what someone actually said" not in written
    assert '"count": 3' in written


# ------------------------------------------------------------ egress guard
def test_a_local_summariser_is_allowed():
    _assert_egress_allowed("http://127.0.0.1:11434/v1", allow_external=False)
    _assert_egress_allowed("http://localhost:8080/v1", allow_external=False)


def test_an_external_summariser_is_refused_unless_configured():
    with pytest.raises(ExternalCallBlocked):
        _assert_egress_allowed("https://api.openai.com/v1", allow_external=False)


def test_an_external_summariser_is_allowed_once_it_is_configured():
    _assert_egress_allowed("https://api.openai.com/v1", allow_external=True)
