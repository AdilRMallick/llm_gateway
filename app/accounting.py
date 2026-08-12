"""Off-the-hot-path accounting writer.

The first version of this wrote each `requests` row inline, before returning the
response. On this machine a single durable commit costs ~66ms — more than the
mocked provider call — so the gateway was charging every client for its own
bookkeeping, and a cache hit that took 4ms of real work took 70ms to return.

Now rows go onto a bounded queue and a single background task drains it, batching
whatever has accumulated into one commit. Batching is what makes this cheap: the
fsync is paid once per batch rather than once per request, so throughput improves
with load instead of degrading.

Three properties worth stating, because each one is a deliberate trade:

* **A full queue drops rows, it never blocks.** Accounting is not worth failing a
  served request over. Drops are counted and surfaced in /stats, so the number is
  visibly incomplete rather than quietly wrong.
* **Rows are not durable at response time.** A crash between response and flush
  loses at most one batch. If this were billing rather than cost attribution,
  that trade would go the other way.
* **/stats and /requests flush before reading**, so an inspection endpoint never
  shows a stale log — which is what keeps the benchmarks honest.
"""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RequestRecord

log = structlog.get_logger(__name__)

_SENTINEL = object()


class AccountingWriter:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        max_queue: int = 10_000,
        max_batch: int = 200,
    ):
        self.sessionmaker = sessionmaker
        self.max_batch = max_batch
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task | None = None
        self.written = 0
        self.dropped = 0
        self.failed = 0
        self.batches = 0

    async def start(self) -> None:
        if self.sessionmaker is not None and self._task is None:
            self._task = asyncio.create_task(self._run(), name="accounting-writer")

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(_SENTINEL)
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except TimeoutError:
            self._task.cancel()
        self._task = None

    def submit(self, record: RequestRecord) -> bool:
        if self.sessionmaker is None:
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("accounting.dropped", queued=self._queue.qsize(), dropped=self.dropped)
            return False

    async def flush(self) -> None:
        """Block until everything submitted so far has been written."""
        if self._task is not None:
            await self._queue.join()

    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
            "batches": self.batches,
            "avg_batch_size": round(self.written / self.batches, 2) if self.batches else None,
        }

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            if first is _SENTINEL:
                self._queue.task_done()
                return

            batch = [first]
            while len(batch) < self.max_batch:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is _SENTINEL:
                    await self._write(batch)
                    for _ in batch:
                        self._queue.task_done()
                    self._queue.task_done()
                    return
                batch.append(item)

            await self._write(batch)
            for _ in batch:
                self._queue.task_done()

    async def _write(self, batch: list[RequestRecord]) -> None:
        try:
            async with self.sessionmaker() as session:
                session.add_all(batch)
                await session.commit()
            self.written += len(batch)
            self.batches += 1
        except Exception as e:  # noqa: BLE001 - the served requests are already gone
            self.failed += len(batch)
            log.error("accounting.write_failed", rows=len(batch), error=str(e))
