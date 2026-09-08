"""Background sweeper.

Data expires without anyone remembering to delete it. The sweeper runs on
startup (in case the service was offline past a job's expiry) and every
`RETENTION_SWEEP_SECONDS` after, purging jobs past their retention window.
Purge is the same code path the erasure endpoint uses, so the guarantee is
identical: blobs shredded, wrapped key destroyed, tombstone kept.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from ..schemas import JobState
from .audit import AuditLog
from .logging import get_logger
from .store import JobStore, _shred

log = get_logger("vocalyze.retention")


class RetentionSweeper:
    def __init__(self, store: JobStore, audit: AuditLog, interval_seconds: int):
        self.store = store
        self.audit = audit
        self.interval = max(30, int(interval_seconds))
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="vocalyze-retention")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        # One sweep at boot, then on the interval.
        await asyncio.sleep(1)
        while self._running:
            try:
                self.sweep_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("retention sweep failed: %s", exc)
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                return

    def sweep_once(self) -> int:
        now = datetime.now(timezone.utc)
        purged = 0
        for record in list(self.store.all()):
            if record.state is JobState.purged:
                continue
            if record.expires_at and record.expires_at <= now:
                self.store.purge(record.id)
                self.audit.record("job.expired", job_id=record.id)
                purged += 1
        if purged:
            log.info("retention sweep removed %d job(s)", purged)
        self.sweep_abandoned_workdirs()
        return purged

    def sweep_abandoned_workdirs(self, older_than_seconds: int = 3600) -> int:
        """Remove pipeline scratch directories left behind by a killed process.

        The orchestrator decodes each upload to a plaintext 16 kHz WAV inside a
        scratch directory and shreds it in a `finally`. A `finally` does not run
        when the process is killed — an out-of-memory kill during a long Whisper
        pass, a container recycle — and nothing else knew about that directory,
        so the one place plaintext audio touches disk could outlive the job that
        justified it. This closes that gap: any scratch directory older than a
        job could plausibly still be using is shredded.

        The age floor matters. A running job owns its directory, and a sweep
        firing every five minutes must not delete audio out from under a
        ninety-minute meeting still being transcribed.
        """
        from ..pipeline.orchestrator import WORKDIR_PREFIX

        cutoff = time.time() - max(older_than_seconds, 60)
        removed = 0
        try:
            candidates = list(self.store.settings.data_dir.glob(f"{WORKDIR_PREFIX}*"))
        except OSError as exc:
            log.warning("could not scan for abandoned work directories: %s", exc)
            return 0

        for path in candidates:
            try:
                if not path.is_dir() or path.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            _shred(path)
            removed += 1
            self.audit.record("workdir.abandoned_removed", path=path.name)
        if removed:
            log.info("retention sweep shredded %d abandoned work director(ies)", removed)
        return removed
