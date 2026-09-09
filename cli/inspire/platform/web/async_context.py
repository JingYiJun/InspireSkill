"""Native waiting for the same cross-process authentication locks."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Generator, cast

from inspire.accounts import cache_lock


@asynccontextmanager
async def cache_lock_async(path: Any, transport: Any) -> AsyncIterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        while not cache_lock._try_acquire(descriptor):
            await asyncio.sleep(min(0.05, transport.remaining()))
        acquired = True
        transport.check_deadline()
        yield
    finally:
        if acquired:
            cache_lock._release(descriptor)
        os.close(descriptor)


@asynccontextmanager
async def authentication_context(context: Any, transport: Any) -> AsyncIterator[None]:
    from inspire.platform.web.session import login_guard, refresh_lock

    name = context.func.__name__
    if name == "exclusive_session_refresh":
        account = context.args[0] if context.args else context.kwds.get("account")
        cache_file = refresh_lock.get_session_cache_file(account)
        if cache_file is None or not cache_file.parent.is_dir():
            yield
            return
        async with cache_lock_async(cache_file.with_name(f"{cache_file.name}.refresh"), transport):
            yield
        return
    if name != "guarded_credential_submission":
        # Test/integration contexts may supply a no-op context manager.
        with context:
            yield
        return
    username, password = context.args
    account = context.kwds["account"]
    path = login_guard.block_file(account)
    if path is None:
        yield
        return
    async with cache_lock_async(path, transport):
        # `_guarded` is the single cooldown/fingerprint decision program.
        guard = cast(
            Generator[None, Any, None],
            login_guard._guarded(
                username,
                password,
                path,
                account=account,
                now=context.kwds.get("now"),
            ),
        )
        next(guard)
        try:
            yield
        except BaseException as error:
            try:
                guard.throw(error)
            except StopIteration:
                pass
            raise
        else:
            try:
                next(guard)
            except StopIteration:
                pass
        finally:
            guard.close()
