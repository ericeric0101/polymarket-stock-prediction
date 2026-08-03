"""Async boundary for non-critical journal writes."""

from __future__ import annotations
import asyncio
from collections.abc import Callable

WriteOperation = Callable[[], None]
ErrorHandler = Callable[[Exception], None]


class JournalWriter:
    def __init__(self, *, on_error: ErrorHandler, maxsize: int = 10_000) -> None:
        self._queue: asyncio.Queue[WriteOperation] = asyncio.Queue(maxsize=maxsize)
        self._on_error = on_error
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def submit(self, operation: WriteOperation) -> bool:
        if self._task is None:
            raise RuntimeError("JournalWriter must be started before submit")
        try:
            self._queue.put_nowait(operation)
            return True
        except asyncio.QueueFull:
            return False

    async def drain(self, *, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        await asyncio.wait_for(self._queue.join(), timeout_seconds)

    async def close(self) -> None:
        await self.drain()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            operation = await self._queue.get()
            try:
                await asyncio.to_thread(operation)
            except Exception as error:
                try:
                    self._on_error(error)
                except Exception:
                    # Observability failures must not kill the writer or strand queue.join().
                    pass
            finally:
                self._queue.task_done()
