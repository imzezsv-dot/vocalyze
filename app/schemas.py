"""The data contract.

These models are the single source of truth shared by the UI, the API and
the three model components (ASR, diarization, LLM). A teammate can swap a
model implementation freely as long as it returns these shapes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class JobState(str, Enum):
    queued = "queued"
    normalizing = "normalizing"
    transcribing = "transcribing"
    diarizing = "diarizing"
    aligning = "aligning"
    summarizing = "summarizing"
    completed = "completed"
    failed = "failed"
    purged = "purged"


TERMINAL_STATES = {JobState.completed, JobState.failed, JobState.purged}

STAGE_ORDER: list[JobState] = [
    JobState.normalizing,
    JobState.transcribing,
    JobState.diarizing,
    JobState.aligning,
    JobState.summarizing,
]


# --------------------------------------------------------------------------
# ASR contract  (owned by the ASR component — Whisper)
# --------------------------------------------------------------------------
class Word(BaseModel):
    start: float = Field(ge=0, description="Seconds from start of audio")
    end: float = Field(ge=0)
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class ASRSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    words: list[Word] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    no_speech_prob: float | None = Field(default=None, ge=0, le=1)


class ASRResult(BaseModel):
    language: str | None = None
    duration: float = 0.0
    segments: list[ASRSegment] = Field(default_factory=list)
    model: str = "unknown"
    backend: str = "unknown"


# --------------------------------------------------------------------------
# Diarization contract  (owned by the diarization component — pyannote)
# --------------------------------------------------------------------------
class SpeakerTurn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class DiarizationResult(BaseModel):
    turns: list[SpeakerTurn] = Field(default_factory=list)
    num_speakers: int = 0
    model: str = "unknown"
    backend: str = "unknown"


# --------------------------------------------------------------------------
# Aligned transcript  (produced here, in the integration layer)
# --------------------------------------------------------------------------
class Utterance(BaseModel):
    id: str = Field(description="Stable id such as u12 — referenced as evidence by the brief")
    speaker: str
    start: float
    end: float
    text: str
    confidence: float | None = None
    speaker_confidence: float | None = Field(
        default=None, description="Share of the utterance covered by the assigned speaker turn"
    )
    overlapped: bool = Field(default=False, description="Two or more speakers active in this span")
    redacted: bool = False


class Transcript(BaseModel):
    language: str | None = None
    duration: float = 0.0
    speakers: list[str] = Field(default_factory=list)
    utterances: list[Utterance] = Field(default_factory=list)

    def by_id(self) -> dict[str, Utterance]:
        return {u.id: u for u in self.utterances}

    def as_prompt_text(self) -> str:
        return "\n".join(f"[{u.id}] {u.speaker} ({u.start:.1f}-{u.end:.1f}s): {u.text}" for u in self.utterances)


# --------------------------------------------------------------------------
# Meeting brief  (owned by the LLM component, verified here)
# --------------------------------------------------------------------------
class Evidence(BaseModel):
    utterance_ids: list[str] = Field(default_factory=list)
    grounding: float = Field(default=0.0, ge=0, le=1, description="Overlap between the claim and its evidence")
    verified: bool = False


class BriefItem(BaseModel):
    text: str
    evidence: Evidence = Field(default_factory=Evidence)


class ActionItem(BriefItem):
    owner: str | None = None
    due: str | None = None


class MeetingBrief(BaseModel):
    summary: str = ""
    summary_evidence: Evidence = Field(default_factory=Evidence)
    key_points: list[BriefItem] = Field(default_factory=list)
    decisions: list[BriefItem] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    dropped_claims: int = Field(default=0, description="Claims removed because no transcript evidence supported them")
    model: str = "unknown"
    backend: str = "unknown"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
class PrivacyOptions(BaseModel):
    consent: bool = Field(default=False, description="Recorded confirmation that participants agreed")
    redact_pii: bool | None = None
    delete_audio_after_asr: bool | None = None
    retention_hours: int | None = Field(default=None, ge=1, le=720)


class StageStatus(BaseModel):
    name: str
    state: Literal["pending", "running", "done", "failed", "skipped"] = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str | None = None

    @property
    def seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class QualityReport(BaseModel):
    """Visible honesty: what the system knows about its own output."""

    asr_mean_confidence: float | None = None
    low_confidence_utterances: int = 0
    speaker_count: int = 0
    overlapped_utterances: int = 0
    unassigned_speech_seconds: float = 0.0
    grounded_claims: int = 0
    dropped_claims: int = 0
    redactions: int = 0


class JobSummary(BaseModel):
    id: str
    state: JobState
    filename: str
    size_bytes: int
    duration_seconds: float | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    error: str | None = None
    stages: list[StageStatus] = Field(default_factory=list)
    progress: float = Field(default=0.0, ge=0, le=1)


class JobResult(BaseModel):
    job: JobSummary
    transcript: Transcript | None = None
    brief: MeetingBrief | None = None
    quality: QualityReport | None = None
    privacy: dict[str, Any] = Field(default_factory=dict)


class UploadAccepted(BaseModel):
    job_id: str
    access_token: str = Field(description="Bearer token for this job — the id alone grants nothing")
    state: JobState
    expires_at: datetime | None
    poll: str


class ErrorBody(BaseModel):
    error: str
    detail: str | None = None
    fix: str | None = Field(default=None, description="What the caller can do about it")
