"""Async contracts on local accounts and fake backends only."""
from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
import inspect
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time
import types
from typing import TypeVar, Union, get_args, get_origin, get_type_hints

import pytest
import inspire
from inspire.sdk import (
    Accounts, ClientClosedError, ClientThreadError, EventResult, InspireAsyncClient,
    InspireClient, Resource, ValidationError, WorkspaceRef,
)
from inspire.sdk import _async_runtime
from inspire.sdk.resources import Workspaces
from test_sdk import client as client
from test_sdk_signatures import facade_methods


def specialize(value, bindings):
    if isinstance(value, TypeVar):
        return bindings[value]
    origin, args = get_origin(value), get_args(value)
    if not args:
        return value
    args = tuple(specialize(arg, bindings) for arg in args)
    if origin in (types.UnionType, Union):
        origin = Union
    if origin is Iterator:
        origin = AsyncIterator
    return origin, args


def test_every_facade_and_signature_has_typed_async_mirror(client):
    async_client = InspireAsyncClient("alpha")
    expected = {(facade, method): bound for facade, method, bound in facade_methods(client)}
    actual = {(facade, method): bound for facade, method, bound in facade_methods(async_client)}
    assert {key[0] for key in expected} == {key[0] for key in actual}
    assert set(actual) - set(expected) == {
        (name, "exec_stream") for name in ("jobs", "notebooks", "hpc", "ray", "servings")
    }
    for key, bound in expected.items():
        mirror = actual[key]
        bindings = {}
        for base in getattr(type(bound.__self__), "__orig_bases__", ()):
            bindings.update(zip(getattr(get_origin(base), "__parameters__", ()), get_args(base)))
        hints = {name: specialize(value, bindings) for name, value in get_type_hints(bound).items()}
        assert {name: specialize(value, {}) for name, value in get_type_hints(mirror).items()} == hints, key
        sync_params = inspect.signature(bound).parameters
        async_params = inspect.signature(mirror).parameters
        assert list(sync_params) == list(async_params), key
        for name, param in sync_params.items():
            assert param.kind == async_params[name].kind, (key, name)
            assert param.default == async_params[name].default, (key, name)
        assert inspect.iscoroutinefunction(mirror) or inspect.isasyncgenfunction(mirror), key
    sync_constructor = inspect.signature(InspireClient).parameters
    async_constructor = dict(inspect.signature(InspireAsyncClient).parameters)
    assert async_constructor.pop("concurrency").default == 1
    assert sync_constructor == async_constructor
    root_methods = {name for name, _ in inspect.getmembers(InspireClient, inspect.isroutine)
                    if not name.startswith("_")}
    assert root_methods == {"from_credentials", "login", "init", "close"}
    for name in root_methods:
        sync_sig = inspect.signature(getattr(InspireClient, name))
        async_sig = inspect.signature(getattr(InspireAsyncClient, name))
        assert sync_sig.parameters == async_sig.parameters
    assert InspireAsyncClient.accounts is InspireClient.accounts is Accounts
    assert inspire.InspireAsyncClient is InspireAsyncClient
    tree = ast.parse(Path(inspire.__file__).read_text())
    checking = next(node for node in tree.body if isinstance(node, ast.If))
    assert any(isinstance(node, ast.alias) and node.name == "InspireAsyncClient"
               for node in ast.walk(checking))


def test_checked_in_wrappers_are_current():
    generate = runpy.run_path("scripts/generate_sdk_async.py")["generate"]
    assert Path("inspire/sdk/async_client.py").read_text() == generate()


@pytest.fixture
def tracked(client, monkeypatch):
    records = []

    class TrackedClient(InspireClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = threading.get_ident()
            self.touches = []
            self.closed = False
            records.append(self)
            check = self._transport.check

            def checked():
                self.touches.append(threading.get_ident())
                assert threading.get_ident() == self.owner
                check()

            self._transport.check = checked

        def close(self):
            assert threading.get_ident() == self.owner
            super().close()
            self.closed = True

    monkeypatch.setattr(_async_runtime, "InspireClient", TrackedClient)
    return records


def test_returns_sync_value_without_blocking_loop(client, tracked, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    rows = [Resource("workspace", WorkspaceRef("workspace", "alpha", client.base_url, "ws", "ws"))]

    def slow(self):
        entered.set()
        assert release.wait(3), "event loop was blocked"
        return rows

    monkeypatch.setattr(Workspaces, "_all", slow)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            task = asyncio.create_task(c.workspaces.list())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            release.set()
            result = await task
            assert result == client.workspaces.list()
            assert c.account == "alpha" and c.base_url == client.base_url
        assert all(not w.thread.is_alive() for w in c._workers)
        await c.close()
        with pytest.raises(ClientClosedError):
            await c.workspaces.list()

    asyncio.run(run())
    assert len(tracked) == 1
    assert tracked[0].closed and tracked[0].touches
    assert set(tracked[0].touches) == {tracked[0].owner}
    assert tracked[0].owner != threading.get_ident()


@pytest.mark.parametrize("concurrency, overlap", [(1, 1), (2, 2)])
def test_pool_concurrency_and_affinity(tracked, monkeypatch, concurrency, overlap):
    active = maximum = 0
    lock = threading.Lock()

    def slow(self):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return []

    monkeypatch.setattr(Workspaces, "_all", slow)

    async def run():
        async with InspireAsyncClient("alpha", concurrency=concurrency) as c:
            await asyncio.gather(*(c.workspaces.list() for _ in range(6)))
        assert all(not w.thread.is_alive() for w in c._workers)

    asyncio.run(run())
    assert maximum == overlap
    assert len(tracked) == concurrency
    assert len({c.owner for c in tracked}) == concurrency
    assert all(c.closed and set(c.touches) == {c.owner} for c in tracked)


def test_exceptions_preserve_instance_type_and_message(tracked, monkeypatch):
    error = ValidationError("same fake failure")

    def fail(self):
        raise error

    monkeypatch.setattr(Workspaces, "_all", fail)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            with pytest.raises(ValidationError, match="same fake failure") as caught:
                await c.workspaces.list()
            assert caught.value is error

    asyncio.run(run())


@pytest.mark.parametrize("facade", ["jobs", "notebooks", "hpc", "ray", "servings"])
def test_follow_cancel_interrupts_real_sleep_and_closes_workers(tracked, monkeypatch, facade):
    from inspire.sdk.jobs import Jobs
    from inspire.sdk.notebooks import Notebooks
    from inspire.sdk.hpc import HPC
    from inspire.sdk.ray import Ray
    from inspire.sdk.servings import Servings
    cls = {"jobs": Jobs, "notebooks": Notebooks, "hpc": HPC, "ray": Ray,
           "servings": Servings}[facade]
    polled = threading.Event()
    polls = []

    def resolve(self, ref, workspace):
        self.client._transport.check()
        return ref

    def events(self, ref, **kwargs):
        self.client._transport.check()
        polls.append(1)
        polled.set()
        return EventResult(())

    monkeypatch.setattr(cls, "_resolve", resolve)
    monkeypatch.setattr(cls, "events", events)
    if facade in {"hpc", "ray", "servings"}:
        monkeypatch.setattr(cls, "_follow_event_batch", events)
    if facade == "jobs":
        monkeypatch.setattr(cls, "get", lambda *a, **k: types.SimpleNamespace(status="RUNNING"))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            stream = getattr(c, facade).follow_events("fake", interval=60)
            task = asyncio.create_task(anext(stream))
            while not polled.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            await stream.aclose()
        assert all(not worker.thread.is_alive() for worker in c._workers)

    asyncio.run(run())
    assert polls == [1]
    assert all(c.closed for c in tracked)


def test_iterator_runs_next_and_close_on_owner_thread(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    closed = threading.Event()

    def rows(self, **kwargs):
        try:
            for value in range(100):
                self.client._transport.check()
                yield value
        finally:
            self.client._transport.check()
            closed.set()

    monkeypatch.setattr(Jobs, "iter", rows)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            async with aclosing(c.jobs.iter("fake")) as stream:
                assert await anext(stream) == 0
            assert closed.is_set()
            assert await c.cache.stats() == {"hits": 0, "misses": 0, "entries": 0}

    asyncio.run(run())


def test_exec_callback_and_async_chunks(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    from inspire.services.remote_exec import ExecResult
    callback_threads = []
    expected = ExecResult(output="onetwo", stdout="onetwo", stderr="", returncode=0,
                          completed=True, transport="fake")

    def execute(self, *, on_output, **kwargs):
        self.client._transport.check()
        for chunk in ("one", "two"):
            on_output(chunk)
        return expected

    monkeypatch.setattr(Jobs, "exec", execute)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            result = await c.jobs.exec("fake", command="fake",
                                       on_output=lambda _: callback_threads.append(threading.get_ident()))
            assert result is expected
            chunks = [chunk async for chunk in c.jobs.exec_stream("fake", command="fake")]
            assert chunks == ["one", "two"]

    asyncio.run(run())
    assert callback_threads == [tracked[0].owner] * 2


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_invalid_concurrency(concurrency):
    with pytest.raises(ValidationError, match="positive integer"):
        InspireAsyncClient("alpha", concurrency=concurrency)


def test_process_and_loop_affinity(tracked, monkeypatch):
    async def run():
        async with InspireAsyncClient("alpha") as c:
            with monkeypatch.context() as patch:
                patch.setattr(c, "_pid", -1)
                with pytest.raises(ClientThreadError):
                    await c.cache.stats()
        return c

    c = asyncio.run(run())
    with pytest.raises(ClientThreadError):
        asyncio.run(c.close())


def test_unclosed_worker_does_not_hold_interpreter_open():
    script = """
import asyncio
from unittest.mock import patch
from types import SimpleNamespace
from inspire.sdk import InspireAsyncClient
async def main():
    with patch('inspire.accounts.account_exists', return_value=True), patch(
        'inspire.config.Config.from_files_and_env',
        return_value=(SimpleNamespace(base_url='https://example.invalid', username='fake'), None),
    ):
        client = InspireAsyncClient('fake')
        await client.cache.stats()
asyncio.run(main())
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=5)


def test_follow_logs_cancel_and_finite_iterator(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    from inspire.sdk import LogResult
    polled = threading.Event()

    def logs(self, *args, **kwargs):
        self.client._transport.check()
        polled.set()
        return LogResult("", (), "", "", False, 0, ())

    monkeypatch.setattr(Jobs, "_resolve", lambda self, ref, ws: ref)
    monkeypatch.setattr(Jobs, "logs", logs)
    monkeypatch.setattr(Jobs, "get", lambda *a, **k: types.SimpleNamespace(status="RUNNING"))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            task = asyncio.create_task(anext(c.jobs.follow_logs("fake", interval=60)))
            while not polled.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            # Pool remains usable after iteration cancellation.
            assert (await c.cache.stats())["entries"] == 0
        assert all(not w.thread.is_alive() for w in c._workers)

    asyncio.run(run())


def test_stream_error_and_early_exec_close(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    closed = threading.Event()
    error = ValidationError("stream failure")

    def fail(self, **kwargs):
        yield "before"
        raise error

    monkeypatch.setattr(Jobs, "iter", fail)

    def execute(self, *, on_output, **kwargs):
        try:
            for _ in range(100000):
                self.client._transport.check()
                on_output("chunk")
        finally:
            closed.set()

    monkeypatch.setattr(Jobs, "exec", execute)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            stream = c.jobs.iter("fake")
            assert await anext(stream) == "before"
            with pytest.raises(ValidationError) as caught:
                await anext(stream)
            assert caught.value is error
            async with aclosing(c.jobs.exec_stream("fake", command="fake")) as chunks:
                assert await anext(chunks) == "chunk"
            assert closed.is_set()

    asyncio.run(run())


def test_partial_initialization_failure_closes_created_workers(tracked, monkeypatch):
    factory = _async_runtime.InspireClient
    error = ValidationError("second worker failed")

    def create(**options):
        if tracked:
            raise error
        return factory(**options)

    monkeypatch.setattr(_async_runtime, "InspireClient", create)

    async def run():
        c = InspireAsyncClient("alpha", concurrency=2)
        with pytest.raises(ValidationError) as caught:
            await c.__aenter__()
        assert caught.value is error
        assert tracked[0].closed
        assert all(not w.thread.is_alive() for w in c._workers)
        await c.close()

    asyncio.run(run())


def test_close_cancels_active_follow_and_rejects_queued_call(tracked, monkeypatch):
    from inspire.sdk.notebooks import Notebooks
    polled = threading.Event()

    def events(self, *args, **kwargs):
        polled.set()
        return EventResult(())

    monkeypatch.setattr(Notebooks, "_resolve", lambda self, ref, ws: ref)
    monkeypatch.setattr(Notebooks, "events", events)

    async def run():
        c = InspireAsyncClient("alpha")
        follow = asyncio.create_task(anext(c.notebooks.follow_events("fake", interval=60)))
        while not polled.is_set():
            await asyncio.sleep(0.001)
        queued = asyncio.create_task(c.workspaces.list())
        await asyncio.sleep(0)
        await asyncio.wait_for(c.close(), 1)
        with pytest.raises(StopAsyncIteration):
            await follow
        with pytest.raises(ClientClosedError):
            await queued
        assert all(not w.thread.is_alive() for w in c._workers)

    asyncio.run(run())


def test_cancelled_close_still_joins_workers(tracked, monkeypatch):
    original = InspireClient.close

    def slow_close(self):
        time.sleep(0.05)
        original(self)

    monkeypatch.setattr(InspireClient, "close", slow_close)

    async def run():
        c = InspireAsyncClient("alpha", concurrency=2)
        await c.__aenter__()
        closing = asyncio.create_task(c.close())
        await asyncio.sleep(0.005)
        closing.cancel()
        await asyncio.sleep(0.005)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert all(not w.thread.is_alive() for w in c._workers)
        assert all(c.closed for c in tracked)
        await c.close()

    asyncio.run(run())


def test_cache_controls_cover_all_sessions(tracked, monkeypatch):
    def rows(self):
        self.client.cache._get(("fake",), lambda: "snapshot")
        return []

    monkeypatch.setattr(Workspaces, "_all", rows)

    async def run():
        async with InspireAsyncClient("alpha", concurrency=2) as c:
            await asyncio.gather(c.workspaces.list(), c.workspaces.list())
            assert (await c.cache.stats())["entries"] == 2
            await c.cache.clear()
            assert (await c.cache.stats())["entries"] == 0

    asyncio.run(run())


def test_credentials_factory_initializes_once_on_worker(tracked, monkeypatch):
    calls = []

    def credentials(account, **options):
        calls.append((threading.get_ident(), account, options))
        return "alpha"

    monkeypatch.setattr("inspire.sdk.client.ensure_credentials", credentials)

    async def run():
        c = InspireAsyncClient.from_credentials("fake", "unused", concurrency=2)
        assert calls == [] and tracked == []
        async with c:
            assert c.account == "alpha"
        assert len(calls) == 1 and calls[0][0] == tracked[0].owner

    asyncio.run(run())


def test_cancelled_initialization_cleans_up(tracked, monkeypatch):
    factory = _async_runtime.InspireClient
    entered = threading.Event()

    def create(**options):
        entered.set()
        time.sleep(0.05)
        return factory(**options)

    monkeypatch.setattr(_async_runtime, "InspireClient", create)

    async def run():
        c = InspireAsyncClient("alpha", concurrency=2)
        task = asyncio.create_task(c.__aenter__())
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not w.thread.is_alive() for w in c._workers)
        assert tracked[0].closed
        await c.close()

    asyncio.run(run())
