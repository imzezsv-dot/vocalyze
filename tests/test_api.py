"""The API contract, exercised end to end through the real app.

Everything below runs the actual FastAPI application with its lifespan — the
job store, the encryption, the pipeline and the retention sweeper are all
real. Only the three models are scripted.
"""

from __future__ import annotations

import time


def upload(client, name="meeting.wav", consent="true", data=None, **extra):
    payload = {"consent": consent, **extra}
    return client.post(
        "/v1/jobs",
        files={"file": (name, data if data is not None else b"RIFF$\x00\x00\x00WAVEfmt ", "audio/wav")},
        data=payload,
    )


def finished(client, job_id, token, timeout=20.0):
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        if summary["state"] in ("completed", "failed"):
            return summary
        time.sleep(0.05)
    raise AssertionError("the job never reached a terminal state")


# ----------------------------------------------------------------- service
def test_health_and_capabilities_describe_the_running_deployment(client):
    health = client.get("/v1/health").json()
    assert health["status"] == "ok"
    assert health["storage_writable"] is True

    capabilities = client.get("/v1/capabilities").json()
    assert capabilities["demo_mode"] is True
    assert capabilities["privacy"]["require_consent"] is True
    assert "wav" in capabilities["limits"]["allowed_extensions"]


def test_the_interface_is_served_by_the_same_process_as_the_api(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Vocalyze" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/demo-data.js").status_code == 200


def test_the_openapi_schema_documents_every_public_route(client):
    paths = client.get("/openapi.json").json()["paths"]
    for route in (
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/result",
        "/v1/jobs/{job_id}/speakers",
        "/v1/privacy/policy",
        "/v1/privacy/audit/verify",
    ):
        assert route in paths, f"{route} is missing from the OpenAPI schema"


# ------------------------------------------------------------ upload rules
def test_an_upload_without_consent_is_refused(client):
    response = upload(client, consent="false")
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "consent_required"
    assert body["fix"]


def test_an_unsupported_file_type_is_refused_with_the_list_of_accepted_ones(client):
    response = upload(client, name="notes.txt")
    assert response.status_code == 415
    assert "wav" in response.json()["fix"]


def test_an_empty_file_is_refused(client):
    response = upload(client, data=b"")
    assert response.status_code == 400
    assert response.json()["error"] == "empty_upload"


def test_a_file_over_the_size_limit_is_refused_while_it_uploads(client):
    from dataclasses import replace

    services = client.app.state.services
    original = services.settings
    services.settings = replace(original, max_upload_mb=0)
    try:
        response = upload(client, data=b"x" * 4096)
    finally:
        services.settings = original

    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"


# ------------------------------------------------------------- happy path
def test_the_whole_flow_from_upload_to_erasure(client):
    accepted = upload(client).json()
    job_id, token = accepted["job_id"], accepted["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert accepted["state"] == "queued"
    assert accepted["expires_at"]

    summary = finished(client, job_id, token)
    assert summary["state"] == "completed", summary.get("error")
    assert summary["progress"] == 1.0
    assert [stage["state"] for stage in summary["stages"]] == ["done"] * 5

    result = client.get(f"/v1/jobs/{job_id}/result", headers=headers).json()

    # a transcript, attributed
    utterances = result["transcript"]["utterances"]
    assert len(utterances) > 1
    assert all(u["speaker"] for u in utterances)
    assert [u["id"] for u in utterances] == [f"u{i}" for i in range(1, len(utterances) + 1)]

    # a brief in which every surviving claim cites a real line
    ids = {u["id"] for u in utterances}
    brief = result["brief"]
    for key in ("key_points", "decisions", "action_items"):
        for item in brief[key]:
            assert item["evidence"]["verified"] is True
            assert item["evidence"]["utterance_ids"]
            assert set(item["evidence"]["utterance_ids"]) <= ids

    # and the receipts the interface renders
    assert result["quality"]["speaker_count"] >= 2
    assert result["quality"]["grounded_claims"] > 0
    assert result["privacy"]["audio_retained"] is False
    assert result["privacy"]["encrypted_at_rest"] is True
    assert result["privacy"]["consent"] is True
    assert result["privacy"]["retention_hours"] == 24
    assert result["privacy"]["models"]["asr"]["backend"] == "mock"

    # erasure
    deleted = client.delete(f"/v1/jobs/{job_id}", headers=headers)
    assert deleted.status_code == 200
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).status_code == 410


def test_the_result_privacy_block_never_reports_an_unresolved_null(client):
    """`null` in the store means 'no per-job override'. The API must answer
    with the setting actually in force instead."""
    accepted = upload(client).json()
    finished(client, accepted["job_id"], accepted["access_token"])
    privacy = client.get(
        f"/v1/jobs/{accepted['job_id']}/result",
        headers={"Authorization": f"Bearer {accepted['access_token']}"},
    ).json()["privacy"]

    for key in ("redact_pii", "delete_audio_after_asr", "retention_hours", "consent"):
        assert privacy[key] is not None


def test_a_per_job_privacy_choice_is_honoured(client):
    accepted = upload(client, delete_audio_after_asr="false", retention_hours="2").json()
    finished(client, accepted["job_id"], accepted["access_token"])
    privacy = client.get(
        f"/v1/jobs/{accepted['job_id']}/result",
        headers={"Authorization": f"Bearer {accepted['access_token']}"},
    ).json()["privacy"]

    assert privacy["delete_audio_after_asr"] is False
    assert privacy["retention_hours"] == 2
    assert privacy["audio_retained"] is True


# ---------------------------------------------------------------- exports
def test_every_export_format_is_produced(client):
    accepted = upload(client).json()
    job_id, token = accepted["job_id"], accepted["access_token"]
    finished(client, job_id, token)
    headers = {"Authorization": f"Bearer {token}"}

    markdown = client.get(f"/v1/jobs/{job_id}/transcript.md", headers=headers)
    assert markdown.status_code == 200
    assert "# Meeting brief" in markdown.text
    assert "## Transcript" in markdown.text

    srt = client.get(f"/v1/jobs/{job_id}/transcript.srt", headers=headers)
    assert srt.status_code == 200
    assert " --> " in srt.text
    assert srt.text.startswith("1\n")

    vtt = client.get(f"/v1/jobs/{job_id}/transcript.vtt", headers=headers)
    assert vtt.text.startswith("WEBVTT")

    plain = client.get(f"/v1/jobs/{job_id}/transcript.txt", headers=headers)
    assert "SPEAKER_" in plain.text

    payload = client.get(f"/v1/jobs/{job_id}/transcript.json", headers=headers).json()
    assert payload["transcript"]["utterances"]


# --------------------------------------------------------------- speakers
def test_naming_a_speaker_rewrites_the_transcript_in_place(client):
    accepted = upload(client).json()
    job_id, token = accepted["job_id"], accepted["access_token"]
    finished(client, job_id, token)
    headers = {"Authorization": f"Bearer {token}"}

    renamed = client.post(f"/v1/jobs/{job_id}/speakers", headers=headers, json={"SPEAKER_00": "Layla"}).json()
    assert "Layla" in renamed["transcript"]["speakers"]
    assert "SPEAKER_00" not in renamed["transcript"]["speakers"]

    # persisted, not just returned
    again = client.get(f"/v1/jobs/{job_id}/result", headers=headers).json()
    assert "Layla" in again["transcript"]["speakers"]


# ----------------------------------------------------------------- access
def test_a_job_cannot_be_read_without_its_token(client):
    accepted = upload(client).json()
    job_id = accepted["job_id"]

    assert client.get(f"/v1/jobs/{job_id}").status_code == 401
    assert client.get(f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.delete(f"/v1/jobs/{job_id}", headers={"X-Job-Token": "wrong"}).status_code == 401


def test_an_unknown_job_is_a_404(client):
    assert client.get("/v1/jobs/nosuchjob", headers={"X-Job-Token": "x"}).status_code == 404


def test_the_result_is_a_409_until_the_job_finishes(client):
    from app.schemas import JobState

    accepted = upload(client).json()
    job_id, token = accepted["job_id"], accepted["access_token"]
    finished(client, job_id, token)

    services = client.app.state.services
    services.store.set_state(job_id, JobState.transcribing)
    response = client.get(f"/v1/jobs/{job_id}/result", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 409
    assert response.json()["error"] == "result_not_ready"


def test_every_error_carries_the_same_envelope(client):
    for response in (
        upload(client, consent="false"),
        upload(client, name="notes.txt"),
        client.get("/v1/jobs/nosuchjob", headers={"X-Job-Token": "x"}),
    ):
        body = response.json()
        assert set(body) == {"error", "detail", "fix"}
        assert body["error"] and body["detail"]


# ---------------------------------------------------------------- privacy
def test_the_policy_endpoint_reports_the_live_configuration(client):
    policy = client.get("/v1/privacy/policy").json()
    assert policy["consent_required"] is True
    assert policy["encryption_at_rest"]["cipher"] == "AES-256-GCM"
    assert policy["third_party_processing"]["external_llm_allowed"] is False


def test_the_audit_trail_for_a_job_is_readable_by_its_holder_only(client):
    accepted = upload(client).json()
    job_id, token = accepted["job_id"], accepted["access_token"]
    finished(client, job_id, token)

    trail = client.get(
        f"/v1/privacy/jobs/{job_id}/audit", headers={"Authorization": f"Bearer {token}"}
    ).json()
    actions = [entry["action"] for entry in trail["entries"]]
    assert "job.created" in actions
    assert "job.completed" in actions

    assert client.get(f"/v1/privacy/jobs/{job_id}/audit").status_code == 401


def test_the_audit_chain_verifies_after_a_real_run(client):
    accepted = upload(client).json()
    finished(client, accepted["job_id"], accepted["access_token"])

    verification = client.get("/v1/privacy/audit/verify").json()
    assert verification["chain_intact"] is True
    assert verification["entries_checked"] > 0
