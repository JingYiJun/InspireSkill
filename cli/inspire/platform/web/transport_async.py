"""Async I/O interpreter for the shared JSON request program."""

from __future__ import annotations

import asyncio
import copy
import threading
from contextlib import AsyncExitStack
from typing import Any, TYPE_CHECKING

import httpx
import requests

from inspire.platform.web.transport_core import Observe, Send, Refresh, Sleep, Action
from inspire.platform.web.transport_policy import _CLI_POLICY, _SDK_POLICY, http_options
from inspire.platform.web.session.requests import build_requests_session

if TYPE_CHECKING:
    from inspire.platform.web.transport import Transport


class AsyncDriver:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.stack = AsyncExitStack()
        self.clients: dict[tuple[Any, ...], httpx.AsyncClient] = {}

    async def __aenter__(self) -> AsyncDriver:
        await self.stack.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stack.__aexit__(*args)

    async def _blocking(self, action: Observe | Refresh | Send) -> Any:
        # Phase S boundary: session acquisition/refresh and Chromium are still
        # synchronous. A disposable worker owns every browser it creates, so
        # Playwright objects never cross threads. The caller keeps its guard.
        owner = self.transport
        owner.check()

        def work() -> tuple[Any, Any]:
            worker = copy.copy(owner)
            worker._thread = threading.get_ident()
            worker._http = worker._browser = None
            try:
                if isinstance(action, Send):
                    result = worker._once(
                        action.method,
                        action.path,
                        action.body,
                        action.timeout,
                        action.browser,
                        action.referer,
                        _disposable=True,
                    )
                else:
                    result = worker._perform(action)
                return result, worker._session
            finally:
                if worker._browser is not None:
                    worker._browser.close()
                if worker._http is not None:
                    worker._http.close()

        result, session = await asyncio.to_thread(work)
        if session is not owner._session:
            owner.adopt_session(session)
        return result

    async def perform(self, action: Action) -> Any:
        owner = self.transport
        if isinstance(action, Observe):
            if owner._session is None:
                return await self._blocking(action)
            return owner._observe()
        if isinstance(action, Refresh):
            return await self._blocking(action)
        if isinstance(action, Sleep):
            await asyncio.sleep(action.delay)
            return None
        if isinstance(action, Send):
            if action.browser:
                return await self._blocking(action)
            return await self._send(action)
        raise AssertionError(action)

    async def _send(self, action: Send) -> Any:
        owner = self.transport
        owner.check()
        url = owner.base_url + action.path
        # requests only prepares bytes here; it never sends async API traffic.
        # This preserves JSON encoding, cookie domain/path matching, default
        # headers, netrc and explicit/system proxy precedence exactly.
        with build_requests_session(owner.session, url) as preparation:
            options = http_options(
                action.method,
                action.body,
                action.referer,
                owner.base_url,
                action.timeout,
                owner.cli_compat,
            )
            prepared = preparation.prepare_request(
                requests.Request(options.method, url, headers=options.headers, json=options.body)
            )
            settings = preparation.merge_environment_settings(url, {}, None, None, None)
            proxy = requests.utils.select_proxy(url, settings["proxies"])
            key = (proxy, settings["verify"], settings["cert"])
            client = self.clients.get(key)
            if client is None:
                client = await self.stack.enter_async_context(
                    httpx.AsyncClient(
                        proxy=proxy,
                        verify=settings["verify"],
                        cert=settings["cert"],
                        trust_env=False,
                        follow_redirects=False,
                    )
                )
                self.clients[key] = client
            timeout = httpx.Timeout(
                action.timeout,
                connect=options.connect_timeout,
            )
            request = httpx.Request(
                prepared.method or action.method,
                prepared.url or url,
                headers=dict(prepared.headers),
                content=prepared.body,
                extensions={"timeout": timeout.as_dict()},
            )
        response = await owner._dispatch(client.send, request, follow_redirects=False)
        policy = _CLI_POLICY if owner.cli_compat else _SDK_POLICY
        return policy.response(response)
