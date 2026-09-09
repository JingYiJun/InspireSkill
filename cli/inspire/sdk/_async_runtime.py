"""Thread-affine execution of the existing synchronous SDK (no async transport)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager
import os
from functools import partial
from queue import Queue
import threading
from types import FunctionType, SimpleNamespace
from typing import Any

from .client import InspireClient
from .exceptions import ClientClosedError, ClientThreadError, ValidationError


class _Stopped(BaseException):
    """Internal cooperative cancellation; never converted into an SDK error."""


class _Worker:
    def __init__(self) -> None:
        self.queue: Queue[tuple[Callable[[], Any], Future[Any]] | None] = Queue()
        self.thread = threading.Thread(target=self._run, name="inspire-sdk", daemon=True)
        self.thread.start()
        self.client: InspireClient

    def _run(self) -> None:
        while (item := self.queue.get()) is not None:
            fn, future = item
            try:
                future.set_result(fn())
            except BaseException as error:
                future.set_exception(error)

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        future: Future[Any] = Future()
        self.queue.put((fn, future))
        return future

    def create(self, options: dict[str, Any]) -> tuple[str, str]:
        self.client = InspireClient(**options)
        return self.client.account, self.client.base_url

    def invoke(self, facade: str, method: str, args: tuple[Any, ...],
               kwargs: dict[str, Any]) -> Any:
        target = getattr(self.client, facade) if facade else self.client
        return getattr(target, method)(*args, **kwargs)


async def _finish(future: asyncio.Future[Any]) -> Any:
    """Finish cleanup even if the owner is cancelled more than once."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve failures too, so no unobserved task exception is left behind.
        future.exception()
        raise asyncio.CancelledError
    return future.result()


def _interruptible_follow(method: Any, stop: threading.Event) -> Any:
    """Give only this generator a private time namespace with interruptible sleep.

    The function code, validation, deduplication and terminal/draining logic remain
    the sync implementation. Neither its module nor process-wide time is patched.
    Scope this adapter to follow methods: ordinary writes must finish classification.
    """
    fn = method.__func__

    def sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise _Stopped

    namespace = dict(fn.__globals__)
    clock = namespace.get("time")
    if clock is not None:
        namespace["time"] = SimpleNamespace(**{**vars(clock), "sleep": sleep})
    clone = FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
    clone.__kwdefaults__ = fn.__kwdefaults__
    return clone.__get__(method.__self__, type(method.__self__))


class AsyncRuntime:
    def __init__(self, options: dict[str, Any], concurrency: int) -> None:
        if type(concurrency) is not int or concurrency < 1:
            raise ValidationError("concurrency must be a positive integer.")
        self._options = options
        self._concurrency = concurrency
        self._pid = os.getpid()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._workers: list[_Worker] = []
        self._available: asyncio.Queue[_Worker] = asyncio.Queue()
        self._stops: set[threading.Event] = set()
        self._closed = False
        self._started = False
        self._closing: asyncio.Task[None] | None = None
        self._account: str | None = None
        self._base_url: str | None = None

    def _check(self) -> None:
        loop = asyncio.get_running_loop()
        if self._pid != os.getpid() or self._loop not in (None, loop):
            raise ClientThreadError("Use each async Client in the same process and event loop.")
        self._loop = loop

    async def _start(self) -> None:
        self._check()
        async with self._lock:
            if self._closed:
                raise ClientClosedError("Client is closed.")
            if self._started:
                return
            try:
                options = dict(self._options)
                for _ in range(self._concurrency):
                    worker = _Worker()
                    self._workers.append(worker)
                    self._account, self._base_url = await _finish(asyncio.wrap_future(
                        worker.submit(lambda: worker.create(options))
                    ))
                    # Resolve the default and persist explicit credentials just once.
                    options = {k: v for k, v in options.items() if k not in ("username", "password")}
                    options["account"] = self._account
                    self._available.put_nowait(worker)
                self._started = True
            except BaseException:
                self._closed = True
                self._closing = asyncio.create_task(self._shutdown())
                await _finish(self._closing)
                raise
            finally:
                self._options = {}

    @asynccontextmanager
    async def _lease(self) -> AsyncIterator[_Worker]:
        await self._start()
        # Poll only the async availability queue so close also wakes queued callers.
        while True:
            if self._closed:
                raise ClientClosedError("Client is closed.")
            try:
                worker = await asyncio.wait_for(self._available.get(), 0.05)
                break
            except asyncio.TimeoutError:
                pass
        try:
            if self._closed:
                raise ClientClosedError("Client is closed.")
            yield worker
        finally:
            self._available.put_nowait(worker)

    async def _call(self, facade: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if facade == "cache":
            await self._start()
            futures = [asyncio.wrap_future(worker.submit(
                partial(worker.invoke, facade, method, args, kwargs)
            )) for worker in self._workers]
            results = await _finish(asyncio.gather(*futures))
            if method == "stats":
                return {key: sum(row[key] for row in results) for key in results[0]}
            return None
        async with self._lease() as worker:
            future = asyncio.wrap_future(worker.submit(
                lambda: worker.invoke(facade, method, args, kwargs)
            ))
            try:
                return await asyncio.shield(future)
            finally:
                # Never reuse a session while a cancelled call is still executing.
                await _finish(future)

    async def _stream(self, facade: str, method: str, *args: Any,
                      output: bool = False, **kwargs: Any) -> AsyncGenerator[Any, None]:
        async with self._lease() as worker:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
            stop = threading.Event()
            self._stops.add(stop)
            end = object()

            def emit(value: Any) -> None:
                if stop.is_set():
                    raise _Stopped
                pending = asyncio.run_coroutine_threadsafe(queue.put(value), loop)
                try:
                    while True:
                        try:
                            pending.result(timeout=0.05)
                            return
                        except TimeoutError:
                            if stop.is_set():
                                raise _Stopped
                finally:
                    if not pending.done():
                        pending.cancel()

            def produce() -> None:
                iterator = None
                try:
                    target = getattr(worker.client, facade)
                    bound = getattr(target, method)
                    if output:
                        callback = kwargs.get("on_output")

                        def on_output(chunk: str) -> None:
                            if callback is not None:
                                callback(chunk)
                            emit(chunk)

                        bound(*args, **dict(kwargs, on_output=on_output))
                    else:
                        if method.startswith("follow_"):
                            bound = _interruptible_follow(bound, stop)
                        iterator = bound(*args, **kwargs)
                        while not stop.is_set():
                            try:
                                value = next(iterator)
                            except StopIteration:
                                break
                            emit(value)
                    emit(end)
                except _Stopped:
                    pass
                finally:
                    if iterator is not None:
                        close = getattr(iterator, "close", None)
                        if close is not None:
                            close()

            future = asyncio.wrap_future(worker.submit(produce))
            try:
                while True:
                    read = asyncio.create_task(queue.get())
                    try:
                        done, _ = await asyncio.wait((read, future), return_when=asyncio.FIRST_COMPLETED)
                        if read in done:
                            value = read.result()
                            if value is end:
                                break
                            yield value
                        elif future.done():
                            future.result()
                            if queue.empty():
                                break
                    finally:
                        read.cancel()
                        await asyncio.gather(read, return_exceptions=True)
                await asyncio.shield(future)
            finally:
                stop.set()
                try:
                    await _finish(future)
                finally:
                    self._stops.discard(stop)

    async def _shutdown(self) -> None:
        for stop in self._stops:
            stop.set()
        errors = []
        for worker in self._workers:
            try:
                def close(w: _Worker = worker) -> None:
                    if hasattr(w, "client"):
                        w.client.close()

                await asyncio.wrap_future(worker.submit(close))
            except BaseException as error:
                errors.append(error)
            finally:
                worker.queue.put(None)
        # Joining in small async steps needs no executor (whose threads block exit).
        while any(worker.thread.is_alive() for worker in self._workers):
            await asyncio.sleep(0.001)
        for worker in self._workers:
            worker.thread.join()
        if errors:
            raise errors[0]

    async def close(self) -> None:
        self._check()
        async with self._lock:
            if self._closing is None:
                self._closed = True
                self._closing = asyncio.create_task(self._shutdown())
        await _finish(self._closing)


class AsyncFacade:
    def __init__(self, client: AsyncRuntime) -> None:
        self._client = client
