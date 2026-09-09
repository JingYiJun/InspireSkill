"""Offload caller-owned synchronous output sinks without changing capture policy."""
from __future__ import annotations

from inspire.platform.web.offload import offload

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from inspire.exec_output import OutputTarget, output_writer


class AsyncOutputWriter:
    def __init__(self, writer: Any):
        self.writer = writer

    async def write(self, text: str) -> None:
        await _finish_io(self.writer.write, text)


@asynccontextmanager
async def async_output_writer(target: OutputTarget) -> AsyncIterator[AsyncOutputWriter | None]:
    context = output_writer(target)
    try:
        writer = await _finish_io(context.__enter__) if target is not None else None
        yield AsyncOutputWriter(writer) if writer is not None else None
    finally:
        if target is not None:
            await _finish_io(context.__exit__, None, None, None)


async def _finish_io(function: Callable[..., Any], *args: Any) -> Any:
    task = asyncio.create_task(offload(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        task.exception()
        raise asyncio.CancelledError
    return task.result()


async def deliver_output(callback: Callable[[str], Any], chunk: str) -> None:
    """Internal streaming callbacks may await bounded-queue backpressure."""
    result = callback(chunk)
    if inspect.isawaitable(result):
        await result
