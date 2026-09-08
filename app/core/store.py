"""Encrypted job store.

Every job has its own directory under `data_dir/jobs/<id>/` containing:
    meta.json    — metadata plus the wrapped data key and the token hash
    original.enc — the uploaded recording (deleted once normalised)
    audio.enc    — the 16 kHz mono working copy (deleted after ASR/diarize)
    result.enc   — transcript + brief + quality

The job id is used as AEAD associated data everywhere, so a ciphertext blob
cannot be moved between jobs undetected. Only the SHA-256 of the access token
is stored — the store cannot issue access to itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ..config import Settings
from ..schemas import JobState, JobSummary, STAGE_ORDER, StageStatus
from .crypto import Sealer, new_data_key
from .logging import get_logger

log = get_logger("vocalyze.store")

TOKEN_BYTES = 24  # 192 bits, more than the 96-bit id
ID_BYTES = 9      # ~12 base64url chars


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short_id() -> str:
    import base64

    raw = secrets.token_bytes(ID_BYTES)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _shred(path: Path) -> None:
    """Best-effort overwrite before unlink.

    Overwriting is not a guarantee on SSDs or journalling filesystems; the
    stronger guarantee is that the wrapped data key is destroyed on erasure,
    making the ciphertext unreadable regardless of whether the bytes linger.
    """
    try:
        if not path.exists():
            return
        if path.is_dir():
            for child in path.iterdir():
                _shred(child)
            path.rmdir()
            return
        size = path.stat().st_size
        with path.open("r+b", buffering=0) as handle:
            handle.write(os.urandom(size))
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("shred %s failed: %s", path, exc)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class JobRecord:
    id: str
    filename: str
    size_bytes: int
    state: JobState = JobState.queued
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    expires_at: datetime | None = None
    duration_seconds: float | None = None
    error: str | None = None
    token_hash: str = ""
    wrapped_key: str = ""
    privacy: dict = field(default_factory=dict)
    stages: dict[str, dict] = field(default_factory=dict)

    def summary(self) -> JobSummary:
        stage_list = []
        for stage in STAGE_ORDER:
            data = self.stages.get(stage.value, {})
            stage_list.append(
                StageStatus(
                    name=stage.value,
                    state=data.get("state", "pending"),
                    started_at=_dt(data.get("started_at")),
                    finished_at=_dt(data.get("finished_at")),
                    detail=data.get("detail"),
                )
            )
        progress = _progress(self.state, stage_list)
        return JobSummary(
            id=self.id,
            state=self.state,
            filename=self.filename,
            size_bytes=self.size_bytes,
            duration_seconds=self.duration_seconds,
            created_at=self.created_at,
            updated_at=self.updated_at,
            expires_at=self.expires_at,
            error=self.error,
            stages=stage_list,
            progress=progress,
        )


def _dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _progress(state: JobState, stages: list[StageStatus]) -> float:
    if state is JobState.completed:
        return 1.0
    if state in (JobState.failed, JobState.purged):
        return 0.0
    done = sum(1 for s in stages if s.state == "done")
    running = 1 if any(s.state == "running" for s in stages) else 0
    return min(1.0, (done + 0.5 * running) / max(1, len(stages)))


class JobStore:
    def __init__(self, settings: Settings, sealer: Sealer):
        self.settings = settings
        self.sealer = sealer
        self._lock = threading.RLock()
        settings.ensure_dirs()

    # ------------------------------------------------------------------
    def _job_dir(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    def _meta_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "meta.json"

    def _blob_path(self, job_id: str, name: str) -> Path:
        return self._job_dir(job_id) / name

    # ------------------------------------------------------------------
    def create(self, filename: str, size_bytes: int, privacy: dict) -> tuple[JobRecord, str, bytes]:
        job_id = _short_id()
        data_key = new_data_key()
        token = _new_token()

        retention_hours = privacy.get("retention_hours") or self.settings.retention_hours
        expires = _now() + timedelta(hours=int(retention_hours))

        record = JobRecord(
            id=job_id,
            filename=filename,
            size_bytes=size_bytes,
            state=JobState.queued,
            expires_at=expires,
            token_hash=_hash_token(token),
            wrapped_key=self.sealer.wrap(data_key, job_id.encode()),
            privacy=privacy,
        )
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(job_dir, 0o700)
        except OSError:
            pass
        self._write_meta(record)
        return record, token, data_key

    # ------------------------------------------------------------------
    def _write_meta(self, record: JobRecord) -> None:
        payload = {
            "id": record.id,
            "filename": record.filename,
            "size_bytes": record.size_bytes,
            "state": record.state.value,
            "created_at": record.created_at.isoformat(),
            "updated_at": _now().isoformat(),
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "duration_seconds": record.duration_seconds,
            "error": record.error,
            "token_hash": record.token_hash,
            "wrapped_key": record.wrapped_key,
            "privacy": record.privacy,
            "stages": record.stages,
        }
        path = self._meta_path(record.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)

    def _read_meta(self, job_id: str) -> JobRecord:
        path = self._meta_path(job_id)
        if not path.exists():
            raise KeyError(job_id)
        raw = json.loads(path.read_text())
        return JobRecord(
            id=raw["id"],
            filename=raw["filename"],
            size_bytes=raw["size_bytes"],
            state=JobState(raw["state"]),
            created_at=datetime.fromisoformat(raw["created_at"]),
            updated_at=datetime.fromisoformat(raw["updated_at"]),
            expires_at=_dt(raw.get("expires_at")),
            duration_seconds=raw.get("duration_seconds"),
            error=raw.get("error"),
            token_hash=raw.get("token_hash", ""),
            wrapped_key=raw.get("wrapped_key", ""),
            privacy=raw.get("privacy", {}),
            stages=raw.get("stages", {}),
        )

    # ------------------------------------------------------------------
    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._read_meta(job_id)

    def all(self) -> Iterable[JobRecord]:
        for entry in self.settings.jobs_dir.iterdir():
            if entry.is_dir() and (entry / "meta.json").exists():
                try:
                    yield self._read_meta(entry.name)
                except (KeyError, json.JSONDecodeError):
                    continue

    def authorise(self, job_id: str, token: str) -> JobRecord | None:
        try:
            record = self._read_meta(job_id)
        except KeyError:
            return None
        # The token is checked before the purge tombstone is revealed. Answering
        # "410 Gone" to someone without the token would tell them a recording
        # under that id once existed, which erasure is supposed to end.
        if not secrets.compare_digest(record.token_hash, _hash_token(token or "")):
            return None
        return record

    def data_key(self, record: JobRecord) -> bytes:
        return self.sealer.unwrap(record.wrapped_key, record.id.encode())

    # ------------------------------------------------------------------
    def update(self, job_id: str, **fields) -> JobRecord:
        with self._lock:
            record = self._read_meta(job_id)
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = _now()
            self._write_meta(record)
            return record

    def set_state(self, job_id: str, state: JobState, *, error: str | None = None) -> JobRecord:
        with self._lock:
            record = self._read_meta(job_id)
            record.state = state
            if error is not None:
                record.error = error
            record.updated_at = _now()
            self._write_meta(record)
            return record

    def mark_stage(self, job_id: str, stage: JobState, state: str, *, detail: str | None = None) -> None:
        with self._lock:
            record = self._read_meta(job_id)
            entry = record.stages.get(stage.value, {})
            if state == "running" and not entry.get("started_at"):
                entry["started_at"] = _now().isoformat()
            if state in ("done", "failed", "skipped"):
                entry.setdefault("started_at", _now().isoformat())
                entry["finished_at"] = _now().isoformat()
            entry["state"] = state
            if detail is not None:
                entry["detail"] = detail
            record.stages[stage.value] = entry
            record.updated_at = _now()
            self._write_meta(record)

    # ------------------------------------------------------------------
    def write_blob(self, job_id: str, name: str, plaintext: bytes, data_key: bytes) -> Path:
        ciphertext = self.sealer.seal(plaintext, data_key, f"{job_id}:{name}".encode())
        path = self._blob_path(job_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ciphertext)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def read_blob(self, job_id: str, name: str, data_key: bytes) -> bytes:
        path = self._blob_path(job_id, name)
        if not path.exists():
            raise FileNotFoundError(name)
        return self.sealer.open(path.read_bytes(), data_key, f"{job_id}:{name}".encode())

    def blob_exists(self, job_id: str, name: str) -> bool:
        return self._blob_path(job_id, name).exists()

    def delete_blob(self, job_id: str, name: str) -> None:
        _shred(self._blob_path(job_id, name))

    def write_json(self, job_id: str, name: str, payload: dict, data_key: bytes) -> None:
        self.write_blob(job_id, name, json.dumps(payload).encode(), data_key)

    def read_json(self, job_id: str, name: str, data_key: bytes) -> dict:
        return json.loads(self.read_blob(job_id, name, data_key).decode())

    # ------------------------------------------------------------------
    def purge(self, job_id: str) -> None:
        """Erasure: shred every blob, destroy the wrapped data key, keep a tombstone."""
        with self._lock:
            try:
                record = self._read_meta(job_id)
            except KeyError:
                return
            job_dir = self._job_dir(job_id)
            for path in list(job_dir.glob("*.enc")):
                _shred(path)
            record.state = JobState.purged
            record.wrapped_key = ""
            record.error = None
            record.privacy = {"purged_at": _now().isoformat()}
            record.updated_at = _now()
            self._write_meta(record)
