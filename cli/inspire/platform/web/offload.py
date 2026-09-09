"""Context-scoped local I/O offloads; SDK clients own their worker lifetime."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial
from typing import Any, Callable


LOCAL_IO_WORKERS = 4


class OffloadPool:
    """Small lazy pool for local I/O, never for SDK business logic or network I/O."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=LOCAL_IO_WORKERS, thread_name_prefix="inspire-local-io",
        )
        self._pending: set[Future[Any]] = set()

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # Keep the concurrent future even when its asyncio waiter is cancelled:
        # close must still join work that cannot be forcibly interrupted.
        self._pending = {future for future in self._pending if not future.done()}
        future = self._executor.submit(copy_context().run, partial(function, *args, **kwargs))
        self._pending.add(future)
        return await asyncio.wrap_future(future)

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        try:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in self._pending),
                return_exceptions=True,
            )
        finally:
            # All running functions have returned; joining no longer blocks I/O.
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._pending.clear()


current_pool: ContextVar[OffloadPool | None] = ContextVar("inspire_offload_pool", default=None)


async def offload(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    pool = current_pool.get()
    if pool is None:
        # Standalone async transport/CLI users retain their existing behavior.
        return await asyncio.to_thread(function, *args, **kwargs)
    return await pool.run(function, *args, **kwargs)
