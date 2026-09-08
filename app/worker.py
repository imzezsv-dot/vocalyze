"""Job queue.

Whisper and pyannote are blocking, CPU/GPU-bound calls that run for minutes.
Running them in the request handler would freeze the event loop and every
other request with it, so uploads return immediately with a job id and the
work happens on a worker thread.

An in-process queue is the right size for a single-node deployment and keeps
the demo to one command. The `submit`/`JobQueue` seam is where a Celery or RQ
broker would go if this ever ran on more than one machine — nothing else in
the codebase would change.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from .core.logging import get_logger
from .pipeline.orchestrator import Orchestrator

log = get_logger("vocalyze.worker")


class JobQueue:
    def __init__(self, orchestrator: Orchestrator, concurrency: int = 1):
        self.orchestrator = orchestrator
        self.concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="vocalyze")
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def submit(self, job_id: str) -> None:
        await self._queue.put(job_id)

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    async def _worker(self, index: int) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                log.info("worker %d picked up job %s", index, job_id)
                await loop.run_in_executor(self._executor, self.orchestrator.run, job_id)
            except Exception as exc:  # noqa: BLE001 - the orchestrator records job failures itself
                log.warning("worker %d crashed on %s: %s", index, job_id, exc)
            finally:
                self._queue.task_done()

    def start(self) -> None:
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"vocalyze-worker-{index}")
            for index in range(self.concurrency)
        ]
        log.info("started %d pipeline worker(s)", self.concurrency)

    async def stop(self) -> None:
        self._running = False
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
