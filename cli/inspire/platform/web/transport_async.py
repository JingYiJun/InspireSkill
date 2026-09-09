"""Async I/O interpreter for the shared JSON request program."""

from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import AsyncExitStack
from typing import Any, TYPE_CHECKING

import httpx
import requests

from inspire.platform.web.transport_core import ApplicationRequest, Observe, Send, Refresh, Sleep, Action
from inspire.platform.web.flow import Call, call, program_for, enter_context, exit_context
from inspire.platform.web.transport_policy import _CLI_POLICY, _SDK_POLICY, http_options
from inspire.platform.web.session.requests import build_requests_session

if TYPE_CHECKING:
    from inspire.platform.web.transport import Transport


class AsyncDriver:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.stack = AsyncExitStack()
        self.clients: dict[tuple[Any, ...], httpx.AsyncClient] = {}
        self.contexts: dict[int, Any] = {}

    async def __aenter__(self) -> AsyncDriver:
        await self.stack.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stack.__aexit__(*args)

    async def _browser_send(self, action: Send) -> Any:
        from inspire.platform.web.session.browser_client import AsyncBrowserRequestClient

        owner = self.transport
        owner.check()
        if isinstance(action.body, ApplicationRequest):
            return await self._send(action)
        policy = _CLI_POLICY if owner.cli_compat else _SDK_POLICY
        url = action.path if action.path.startswith(("https://", "http://")) else owner.base_url + action.path
        kwargs = {"headers": {"Referer": action.referer} if action.referer else {}} if owner.cli_compat else {}
        try:
            async with AsyncBrowserRequestClient(owner.session) as client:
                return await owner._dispatch(
                    client.request_json, action.method, url, body=action.body,
                    timeout=action.timeout, **kwargs,
                )
        except Exception as error:
            from inspire.platform.web import session as web_session

            if owner.cli_compat and web_session.is_playwright_browser_runtime_error(error):
                # The async disposable context already closed its own resources.
                web_session.raise_browser_runtime_error(error)
            policy.browser_error(error)

    async def perform(self, action: Action) -> Any:
        owner = self.transport
        if isinstance(action, Observe):
            if owner._session is None:
                await self.execute(call(owner._acquire_session))
            return owner._observe()
        if isinstance(action, Refresh):
            function = owner._refresh_cli if owner.cli_compat else owner._refresh
            args = (action.observed_generation,) if owner.cli_compat else ()
            kwargs = {"can_refresh": action.can_refresh} if owner.cli_compat else {}
            return await self.execute(call(function, *args, **kwargs))
        if isinstance(action, Sleep):
            await asyncio.sleep(action.delay)
            return None
        if isinstance(action, Send):
            if action.browser:
                return await self._browser_send(action)
            return await self._send(action)
        raise AssertionError(action)

    async def _send(self, action: Send) -> Any:
        owner = self.transport
        owner.check()
        if isinstance(action.body, ApplicationRequest):
            return await self.execute(call(
                owner._application_send, action.method, action.path, action.body, action.timeout,
            ))
        url = action.path if action.path.startswith(("https://", "http://")) else owner.base_url + action.path
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

    async def execute(self, action: Call) -> Any:
        """Interpret nested workflows without moving authentication into a worker."""
        from inspire.services import remote_exec
        from inspire.platform.web.browser_api import jupyter_terminal

        adapters: dict[Any, Any] = {
            remote_exec.exec_over_pty_websocket: remote_exec.exec_over_pty_websocket_async,
            remote_exec.exec_in_notebook_jupyter: remote_exec.exec_in_notebook_jupyter_async,
            jupyter_terminal.run_command_capture_in_notebook: jupyter_terminal.run_command_capture_in_notebook_async,
        }
        if action.function in {remote_exec.exec_in_notebook_ssh, remote_exec.cached_notebook_bridge}:
            return await asyncio.to_thread(action.function, *action.args, **action.kwargs)
        if inspect.iscoroutinefunction(action.function):
            return await action.function(*action.args, **action.kwargs)
        adapter = adapters.get(action.function)
        if adapter is not None:
            return await adapter(*action.args, **action.kwargs)
        if action.http:
            return await self._http(action)
        if action.function is enter_context:
            from inspire.platform.web.async_context import authentication_context

            context = action.args[0]
            if not hasattr(context, "func"):
                return context.__enter__()
            asynchronous = authentication_context(context, self.transport)
            await asynchronous.__aenter__()
            self.contexts[id(context)] = asynchronous
            return None
        if action.function is exit_context:
            context, *error = action.args
            asynchronous = self.contexts.pop(id(context), None)
            if asynchronous is None:
                return context.__exit__(*error)
            return await asynchronous.__aexit__(*error)
        if getattr(action.function, "__name__", "") == "_login_with_browser":
            return await asyncio.to_thread(action.function, *action.args, **action.kwargs)
        if getattr(action.function, "__name__", "") in {"_borrow_plaza_slot", "reset_plaza_client"}:
            while self.transport._plaza_busy:
                self.transport.check_deadline()
                await asyncio.sleep(0.01)
        if action.function is time.sleep:
            await asyncio.sleep(*action.args)
            return None
        program = program_for(action)
        if program is None:
            return action.function(*action.args, **action.kwargs)
        try:
            step = next(program)
            while True:
                try:
                    result = await self.execute(step)
                except BaseException as error:
                    step = program.throw(error)
                else:
                    step = program.send(result)
        except StopIteration as done:
            return done.value
        finally:
            program.close()

    async def _http(self, action: Call) -> requests.Response:
        """Send prepared requests with native redirects and a caller-owned cookie jar."""
        owner = self.transport
        http = getattr(action.function, "__self__")
        method = action.function.__name__
        args = action.args
        if method == "request":
            method, url, *rest = args
        else:
            url, *rest = args
        kwargs = dict(action.kwargs)
        follow = kwargs.pop("allow_redirects", True)
        budget = kwargs.pop("timeout", owner.timeout)
        if isinstance(budget, tuple):
            connect, budget = budget
        else:
            connect = budget
        budget = min(budget, owner.remaining())
        prepared = http.prepare_request(requests.Request(method.upper(), url, **kwargs))
        settings = http.merge_environment_settings(prepared.url, {}, None, None, None)
        proxy = requests.utils.select_proxy(prepared.url, settings["proxies"])
        key = (proxy, settings["verify"], settings["cert"])
        client = self.clients.get(key)
        if client is None:
            client = await self.stack.enter_async_context(httpx.AsyncClient(
                proxy=proxy, verify=settings["verify"], cert=settings["cert"],
                trust_env=False, follow_redirects=False,
            ))
            self.clients[key] = client
        client.cookies.clear()
        client.cookies.update(http.cookies)
        request = httpx.Request(
            prepared.method, prepared.url, headers=dict(prepared.headers), content=prepared.body,
            extensions={"timeout": httpx.Timeout(budget, connect=min(connect, budget)).as_dict()},
        )
        try:
            response = await asyncio.wait_for(client.send(request, follow_redirects=follow), budget)
        except (httpx.RequestError, TimeoutError) as error:
            owner.check_deadline()
            if isinstance(error, (httpx.TimeoutException, TimeoutError)):
                raise requests.Timeout(str(error)) from error
            if isinstance(error, httpx.TooManyRedirects):
                raise requests.TooManyRedirects(str(error)) from error
            raise requests.ConnectionError(str(error)) from error
        owner.check_deadline()
        for cookie in client.cookies.jar:
            http.cookies.set_cookie(cookie)
        for item in [*response.history, response]:
            for cookie in item.cookies.jar:
                http.cookies.set_cookie(cookie)
        result = requests.Response()
        result.status_code = response.status_code
        result.headers.update(response.headers)
        result._content = response.content
        result.url = str(response.url) if response._request is not None else str(prepared.url)
        result.encoding = response.encoding
        result.request = prepared
        return result
