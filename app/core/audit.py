"""Hash-chained, append-only audit log.

Each entry carries the SHA-256 of the entry before it, so removing or editing
one line breaks the chain — visible through `/v1/privacy/audit/verify`. The
chain detects tampering; it does not prevent it. Detection is what a
single-node service can honestly offer.

Content is never written here: the field names are checked and anything that
looks like transcript text is refused, so this file stays safe to keep after
the transcript itself is erased.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .logging import get_logger

log = get_logger("vocalyze.audit")

_FORBIDDEN_KEYS = {"text", "transcript", "utterance", "summary", "content", "body"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitise(details: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if key.lower() in _FORBIDDEN_KEYS:
            continue
        if isinstance(value, (dict, list, tuple)):
            try:
                json.dumps(value)
            except TypeError:
                value = str(value)
        elif isinstance(value, str) and len(value) > 300:
            value = value[:300] + "…"
        clean[key] = value
    return clean


class AuditLog:
    def __init__(self, path: Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)

    # ------------------------------------------------------------------
    def _tail_hash(self) -> str:
        with self.path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size == 0:
                return "0" * 64
            block = 4096
            handle.seek(max(0, size - block))
            tail = handle.read().splitlines()
        if not tail:
            return "0" * 64
        try:
            last = json.loads(tail[-1].decode())
        except Exception:  # noqa: BLE001
            return "0" * 64
        return last.get("hash", "0" * 64)

    def _line_hash(self, entry: dict[str, Any]) -> str:
        payload = {k: entry[k] for k in ("ts", "action", "details", "prev") if k in entry}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    # ------------------------------------------------------------------
    def record(self, action: str, **details: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            prev = self._tail_hash()
            entry = {
                "ts": _now(),
                "action": action,
                "details": _sanitise(details),
                "prev": prev,
            }
            entry["hash"] = self._line_hash(entry)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")

    def entries(self, job_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        results = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if job_id and entry.get("details", {}).get("job_id") != job_id:
                continue
            results.append(entry)
        return results

    def verify(self) -> tuple[bool, int]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return True, 0
        prev = "0" * 64
        count = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, count
            expected_prev = entry.get("prev")
            if expected_prev != prev:
                return False, count
            recomputed = self._line_hash(entry)
            if recomputed != entry.get("hash"):
                return False, count
            prev = entry["hash"]
            count += 1
        return True, count
