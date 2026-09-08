"""One error shape for every failure the API returns.

Each error carries `error` (a slug), `detail` (what went wrong) and `fix`
(what the caller can do about it). The exception classes below are raised
inside handlers; `main.http_error` turns them into that envelope.
"""

from __future__ import annotations

from fastapi import HTTPException


class ApiError(HTTPException):
    error: str = "error"
    fix: str | None = None
    status: int = 400

    def __init__(self, detail: str, *, fix: str | None = None, status: int | None = None):
        super().__init__(
            status_code=status or self.status,
            detail={"error": self.error, "detail": detail, "fix": fix or self.fix},
        )


class ConsentRequired(ApiError):
    error = "consent_required"
    status = 403
    fix = "Send consent=true with the upload after confirming everyone in the recording agreed."

    def __init__(self):
        super().__init__(
            "This deployment processes recordings only with recorded consent from the meeting participants."
        )


class EmptyUpload(ApiError):
    error = "empty_upload"
    status = 400
    fix = "Attach a non-empty audio file to the `file` field."

    def __init__(self):
        super().__init__("The upload was empty.")


class FileTooLarge(ApiError):
    error = "file_too_large"
    status = 413

    def __init__(self, limit_mb: int):
        super().__init__(
            f"This deployment accepts recordings up to {limit_mb} MB.",
            fix=f"Trim the recording or split it into files of at most {limit_mb} MB.",
        )


class UnsupportedFormat(ApiError):
    error = "unsupported_format"
    status = 415

    def __init__(self, allowed: list[str]):
        super().__init__(
            "This file type is not one the pipeline decodes.",
            fix=f"Convert the recording to one of: {', '.join(allowed)}.",
        )


class NotAuthorized(ApiError):
    error = "not_authorized"
    status = 401
    fix = "Send the access_token from the upload response as `Authorization: Bearer <token>`."

    def __init__(self):
        super().__init__("A valid job token is required.")


class JobNotFound(ApiError):
    error = "job_not_found"
    status = 404
    fix = "Check the job id, or upload the recording again — retention windows are short by design."

    def __init__(self):
        super().__init__("No job with that id.")


class JobPurged(ApiError):
    error = "job_purged"
    status = 410

    def __init__(self):
        super().__init__(
            "This recording was deleted and its key destroyed. It cannot be recovered.",
            fix="Upload the recording again if you still need it processed.",
        )


class ResultNotReady(ApiError):
    error = "result_not_ready"
    status = 409

    def __init__(self, state: str):
        super().__init__(
            f"The job is still {state}.",
            fix="Poll /v1/jobs/{id} or subscribe to /v1/jobs/{id}/events until state is completed.",
        )


class PipelineError(RuntimeError):
    """Raised inside a pipeline stage. Caught by the orchestrator."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
