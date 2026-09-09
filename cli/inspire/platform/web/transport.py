"""Shared web dispatcher. SDK writes are sent once, with no implicit replay."""

from __future__ import annotations
import os
import threading
import time
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Any, Callable, Iterator, NoReturn

from inspire.platform.errors import (
    AuthenticationError,
    AuthenticationCooldownError,
    ClientClosedError,
    ClientThreadError,
    TransportError,
    ValidationError,
    SubmissionUncertainError,
    MutationUncertainError,
    WaitTimeoutError,
)


if TYPE_CHECKING:
    from inspire.platform.web.plaza.core import PlazaClient
    from inspire.platform.web.session.models import WebSession

from inspire.platform.web.transport_policy import (  # noqa: F401 - compatibility imports
    _NonJSONResponse, _SDK_POLICY, _CLI_POLICY, http_options,
)
from inspire.platform.web.transport_core import (
    RequestCore, SharedState, Observe, Observation, Send, Refresh, Sleep, Return, Raise, Action,
    _SingleSendViolation, _classify_after_dispatch, claim_write, remaining, uncertain,
)

class Transport:
    """Own dispatch and authentication state for a caller.

    ``cli_compat`` preserves the CLI's HTTP messages, Retry-After waits, shared
    thread-local pools and renewal policy. The SDK defaults and single-send
    contract remain independent of that presentation/compatibility policy.
    """

    def __init__(
        self,
        account: str | None,
        base_url: str,
        *,
        username: str,
        allow_browser: bool = False,
        timeout: float = 30,
        cli_compat: bool = False,
    ):
        self.account, self.base_url = account, base_url.rstrip("/")
        self.username, self.allow_browser, self.timeout = username, allow_browser, timeout
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self.cli_compat = cli_compat
        self._decisions = SharedState()
        self._generation_lock = threading.RLock()
        self._plaza_lock = threading.Condition()
        self._plaza_busy = False
        self._closed = False
        self._session: Any = None
        self._http: Any = None
        self._browser: Any = None
        self._plaza: PlazaClient | None = None
        self._plaza_key: tuple[str | None, float] | None = None
        self.deadline: float | None = None

    @property
    def _force_browser(self) -> bool:
        return self._decisions.force_browser

    @_force_browser.setter
    def _force_browser(self, value: bool) -> None:
        self._decisions.force_browser = value

    @property
    def _unproven_rebuild(self) -> float | None:
        return self._decisions.unproven_rebuild

    @_unproven_rebuild.setter
    def _unproven_rebuild(self, value: float | None) -> None:
        self._decisions.unproven_rebuild = value

    @property
    def _last_success(self) -> float | None:
        return self._decisions.last_success

    @_last_success.setter
    def _last_success(self, value: float | None) -> None:
        self._decisions.last_success = value

    @property
    def _write(self) -> dict[str, Any] | None:
        return self._decisions.write

    @_write.setter
    def _write(self, value: dict[str, Any] | None) -> None:
        self._decisions.write = value

    def check(self) -> None:
        if self._pid != os.getpid() or (
            not self.cli_compat and self._thread != threading.get_ident()
        ):
            raise ClientThreadError("Create and use each Client in the same process and thread.")
        if self._closed:
            raise ClientClosedError("Client is closed.")

    def remaining(self) -> float:
        self.check()
        return remaining(self.timeout, self.deadline, time.monotonic())

    def check_deadline(self) -> None:
        """Raise WaitTimeoutError when the operation budget is exhausted."""
        self.remaining()

    @contextmanager
    def scope(self, *, timeout: float | None = 120) -> Iterator[None]:
        """Bind this transport; None adds no deadline and preserves any outer one."""
        from inspire.accounts import account_scope
        from inspire.platform.web.runtime import active_transport

        self.check()
        old = self.deadline
        if timeout is not None:
            self.deadline = min(old, time.monotonic() + timeout) if old else time.monotonic() + timeout
        token = active_transport.set(self)
        try:
            with nullcontext() if self.cli_compat else account_scope(self.account):
                yield
        finally:
            active_transport.reset(token)
            self.deadline = old

    @contextmanager
    def single_send(self, operation_id: str = "", *, create: bool = False) -> Iterator[None]:
        self.check()
        if self._write is not None:
            raise _SingleSendViolation("single_send blocks cannot be nested.")
        if self._last_success is None or time.monotonic() - self._last_success >= 60:
            from inspire.platform.web.session.auth import USER_DETAIL_PATH
            from inspire.platform.web.session.envelope import _v2_result

            _v2_result(self.request("POST", USER_DETAIL_PATH, body={}))
        # The probe is a READ. A dispatched write must never be replayed;
        # only failures without a definite rejection remain uncertain.
        state = {"operation_id": operation_id, "create": create, "used": False, "sent": False}
        self._write = state
        try:
            yield
        except (
            SubmissionUncertainError,
            MutationUncertainError,
            _SingleSendViolation,
            ValidationError,
            AuthenticationError,
            TransportError,
        ):
            raise
        except Exception as error:
            if state["sent"]:
                classified = _classify_after_dispatch(error)
                if classified is not None:
                    raise classified from error
                self._uncertain(state, error)
            raise
        finally:
            self._write = None

    def _uncertain(self, state: dict[str, Any], error: Exception) -> NoReturn:
        uncertain(state, error)

    def _validate_session(self, session):
        if (
            session is None
            or not session.storage_state.get("cookies")
            or (session.base_url or "").rstrip("/") != self.base_url
            or session.account not in (None, self.account)
        ):
            raise AuthenticationError("No matching cached session; initialize this account first.")
        return session

    @property
    def session(self):
        self.check()
        if self._session is None:
            if self.cli_compat:
                from inspire.platform.web.session import get_web_session

                self._session = get_web_session(account=self.account)
                return self._session
            from inspire.platform.web.session.models import WebSession

            cached = WebSession.load(allow_expired=True, account=self.account)
            try:
                self._session = self._validate_session(cached)
            except AuthenticationError:
                if not self.allow_browser:
                    raise
                self._refresh()
        return self._session

    def adopt_session(self, session: WebSession) -> None:
        """Use an already acquired session without consulting disk or logging in."""
        self.check()
        if self.cli_compat:
            self._session = session
        else:
            self._adopt_session(session)

    def _adopt_session(self, session):
        from inspire.platform.web.session.requests import _configure

        self._session = self._validate_session(session)
        if self._http is not None:
            _configure(self._http, self._session, self.base_url)

    def _refresh_expired_session(self, observed_created_at: float) -> WebSession:
        from inspire.platform.web import session as web_session

        session = self.session
        if session.created_at > observed_created_at:
            return session
        with web_session.exclusive_session_refresh(session.account):
            if session.created_at > observed_created_at:
                return session
            cached = web_session.WebSession.load(allow_expired=True, account=session.account)
            if (
                cached is not None
                and cached.storage_state.get("cookies")
                and cached.created_at > observed_created_at
            ):
                return cached
            renewed = web_session.renew_web_session_without_credentials(session)
            if renewed is not None:
                web_session.logger.debug(
                    "Web session renewed through cached SSO state without credentials."
                )
                return renewed
            return web_session.acquire_web_session(force_refresh=True, account=session.account)

    def _refresh_cli(self, observed_created_at: float, *, can_refresh: bool) -> None:
        from inspire.platform.web import session as web_session

        # Capture the sent generation, not the current fields of a shared object.
        web_session.close_browser_client()
        with self._generation_lock:
            self._decisions.refresh_guard(observed_created_at, can_refresh)
            web_session.logger.debug("Web session expired; rebuilding it once for this call.")
            refreshed = self._refresh_expired_session(observed_created_at)
            web_session.refresh_session_in_place(self.session, refreshed)
            self._decisions.rebuilt(self.session.created_at)

    def _refresh(self):
        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.session.refresh_lock import exclusive_session_refresh
        from inspire.platform.web.session import auth

        previous = self._session.created_at if self._session else None
        try:
            with exclusive_session_refresh(self.account, timeout=self.remaining()):
                try:
                    cached = WebSession.load(allow_expired=True, account=self.account)
                    if cached and (previous is None or cached.created_at > previous):
                        self._adopt_session(cached)
                        return
                except Exception as error:
                    self.check_deadline()
                    if getattr(error, "retry_at", None) is not None:
                        raise
                try:
                    if self._session is not None:
                        renewed = auth.renew_web_session_without_credentials(self._session)
                        if renewed is not None:
                            self._validate_session(renewed)
                            auth._persist(renewed, account=self.account)
                            self._adopt_session(renewed)
                            return
                except Exception as error:
                    self.check_deadline()
                    if getattr(error, "retry_at", None) is not None:
                        raise
                try:
                    username, password = auth.get_credentials(self.account)
                    self._adopt_session(
                        auth.login_without_browser(
                            username, password, base_url=self.base_url, account=self.account
                        )
                    )
                except Exception as error:
                    self.check_deadline()
                    if (
                        not self.allow_browser
                        or getattr(error, "retry_at", None) is not None
                        or isinstance(error.__cause__, auth._CasVerificationRequired)
                    ):
                        raise
                    self._adopt_session(
                        auth.get_web_session(force_refresh=True, account=self.account)
                    )
        except (AuthenticationError, WaitTimeoutError):
            raise
        except Exception as error:
            self.check_deadline()
            retry_at = getattr(error, "retry_at", None)
            if isinstance(retry_at, (int, float)):
                raise AuthenticationCooldownError(retry_at) from None
            raise AuthenticationError(str(error)) from error
        finally:
            if self._browser is not None:
                self._browser.close()
                self._browser = None

    def _once(
        self, method, path, body, timeout, browser=False, referer=None, *, policy=None,
        _disposable=False,
    ):
        from inspire.platform.web import session as web_session

        policy = policy or (_CLI_POLICY if self.cli_compat else _SDK_POLICY)
        url = self.base_url + path
        headers = {"Referer": referer} if referer else {}
        if not browser:
            args: tuple[Any, ...]
            if self.cli_compat:
                http = web_session.pooled_requests_session(self.session, url)
            else:
                from inspire.platform.web.session.requests import build_requests_session, _configure

                if self._http is None:
                    self._http = build_requests_session(self.session, self.base_url)
                else:
                    _configure(self._http, self.session, self.base_url)
                http = self._http
            options = http_options(method, body, referer, self.base_url, timeout, self.cli_compat)
            kwargs: dict[str, Any] = {
                "headers": options.headers, "timeout": timeout, "allow_redirects": False,
            }
            if options.include_json:
                kwargs["json"] = options.body
            if self.cli_compat:
                http_send, args = getattr(http, options.method.lower()), (url,)
            else:
                kwargs["timeout"] = (options.connect_timeout, timeout)
                http_send, args = http.request, (options.method, url)
            return policy.response(self._dispatch(http_send, *args, **kwargs))

        from inspire.platform.web.browser_api.core import _in_asyncio_loop, _run_in_thread

        disposable = _disposable or (self.cli_compat and _in_asyncio_loop())

        def send():
            if self.cli_compat:
                client = (
                    web_session.create_browser_client(self.session) if disposable
                    else web_session.get_browser_client(self.session)
                )
            elif disposable:
                client = web_session.create_browser_client(self.session)
            else:
                if self._browser is None:
                    self._browser = web_session.create_browser_client(self.session)
                client = self._browser
            try:
                # The SDK historically sends no browser Referer override.
                kwargs = {"headers": headers} if self.cli_compat else {}
                return self._dispatch(
                    client.request_json, method, url, body=body, timeout=timeout, **kwargs
                )
            finally:
                if disposable:
                    client.close()

        try:
            return _run_in_thread(send) if disposable and not _disposable else send()
        except Exception as error:
            policy.browser_error(error)

    def _dispatch(self, send: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._decisions.dispatched()
        return send(*args, **kwargs)

    def _core(self) -> RequestCore:
        return RequestCore(
            self._decisions, cli_compat=self.cli_compat, allow_browser=self.allow_browser,
            timeout=self.timeout, deadline=self.deadline,
        )

    def _observe(self) -> Observation:
        import random

        self.check()
        generation = self.session.created_at
        return Observation(time.monotonic(), generation, random.random())

    def _finish(self, action: Return) -> Any:
        with self._generation_lock:
            self._decisions.success(action.observed_generation, time.monotonic(), self.cli_compat)
        return action.payload

    def _perform(self, action: Action) -> Any:
        if isinstance(action, Observe):
            return self._observe()
        if isinstance(action, Send):
            return self._once(
                action.method, action.path, action.body, action.timeout, action.browser, action.referer
            )
        if isinstance(action, Refresh):
            if self.cli_compat:
                return self._refresh_cli(
                    action.observed_generation, can_refresh=action.can_refresh
                )
            return self._refresh()
        if isinstance(action, Sleep):
            time.sleep(action.delay)
            return None
        raise AssertionError(action)

    def request(
        self, method: str, path: str, *, body=None, timeout: float = 30, referer=None
    ) -> dict:
        program = self._core().run(method, path, body, timeout, referer)
        try:
            action = next(program)
            while True:
                if isinstance(action, Return):
                    return self._finish(action)
                if isinstance(action, Raise):
                    raise action.error
                try:
                    result = self._perform(action)
                except Exception as error:
                    action = program.throw(error)
                else:
                    action = program.send(result)
        finally:
            program.close()

    async def request_async(
        self, method: str, path: str, *, body=None, timeout: float = 30, referer=None
    ) -> dict:
        """Internal native JSON path; not yet wired to InspireAsyncClient."""
        from inspire.platform.web.transport_async import AsyncDriver

        self.check()
        if not (path == "/api/v2" or path.startswith(("/api/v2/", "/api/v2?"))):
            raise ValueError("Async transport supports only /api/v2 JSON requests.")
        program = self._core().run(method, path, body, timeout, referer)
        async with AsyncDriver(self) as driver:
            try:
                action = next(program)
                while True:
                    if isinstance(action, Return):
                        return self._finish(action)
                    if isinstance(action, Raise):
                        raise action.error
                    try:
                        result = await driver.perform(action)
                    except Exception as error:
                        action = program.throw(error)
                    else:
                        action = program.send(result)
            finally:
                program.close()

    def reset_plaza_client(self) -> None:
        """Discard only this transport's signed-in data plaza connection."""
        self.check()
        with self._plaza_lock:
            self._plaza_lock.wait_for(lambda: not self._plaza_busy)
            stale, self._plaza, self._plaza_key = self._plaza, None, None
            if stale is not None:
                stale.close()

    @contextmanager
    def _plaza_client(self, session: WebSession, timeout: float) -> Iterator[PlazaClient]:
        from inspire.platform.web.plaza.core import sign_in

        key = (session.account, session.created_at)
        with self._plaza_lock:
            self._plaza_lock.wait_for(lambda: not self._plaza_busy)
            if self._plaza_key != key:
                if self._plaza is not None:
                    self._plaza.close()
                self._plaza, self._plaza_key = None, None
            client = self._plaza
            # Reserve the cookie jar, but release the state lock during I/O.
            # Reset waits for the borrower before closing or replacing it.
            self._plaza_busy = True
        try:
            if client is None:
                client = sign_in(session, self, timeout)
                with self._plaza_lock:
                    self._plaza, self._plaza_key = client, key
            yield client
        finally:
            with self._plaza_lock:
                self._plaza_busy = False
                self._plaza_lock.notify_all()

    def plaza_request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None, timeout: float = 30
    ) -> Any:
        """Dispatch plaza calls with caller-owned authentication and retry budgets."""
        import requests
        from inspire.platform.web.plaza.core import (
            PLAZA_BASE_URL, PlazaError, PlazaNotSignedIn, unwrap,
        )
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError
        from inspire.platform.web.session.retry import backoff_delay

        self.check()
        state = self._write
        claim_write(state)
        auth_attempt, transient_attempt = 0, 0
        while True:
            self.check_deadline()
            session = self.session
            try:
                with self._plaza_client(session, timeout) as client:
                    response = self._dispatch(
                        client.http.request, method.upper(), PLAZA_BASE_URL + path,
                        params=params, json=body, timeout=min(timeout, self.remaining()),
                        allow_redirects=False,
                    )
                    self.check_deadline()
                    return unwrap(response)
            except Exception as error:
                if state is not None and state["sent"]:
                    classified = _classify_after_dispatch(error)
                    if classified is error:
                        raise
                    if classified is not None:
                        raise classified from error
                    self._uncertain(state, error)
                self.check_deadline()
                if isinstance(error, (SessionExpiredError, PlazaNotSignedIn)):
                    self.reset_plaza_client()
                    if auth_attempt == 2:
                        raise SessionExpiredError(
                            "The data plaza rejected the refreshed platform session."
                        ) from error
                    auth_attempt += 1
                    if auth_attempt == 2:
                        try:
                            self._refresh()
                        except AuthenticationError as refresh_error:
                            if self.cli_compat:
                                raise SessionExpiredError(str(refresh_error)) from refresh_error
                            raise
                    continue
                if isinstance(error, TransientAPIError):
                    if transient_attempt == 2 or state is not None:
                        raise
                    time.sleep(min(backoff_delay(transient_attempt, error), self.remaining()))
                    transient_attempt += 1
                    continue
                if isinstance(error, requests.RequestException):
                    raise PlazaError("The data plaza did not answer.") from error
                raise

    def close(self) -> None:
        if self._closed:
            return
        self.check()
        try:
            self.reset_plaza_client()
            if self.cli_compat:
                from inspire.platform.web import session as web_session

                web_session.close_browser_client()
                web_session.close_pooled_requests_session()
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._http is not None:
                self._http.close()
            self._session = self._browser = self._http = None
            self._closed = True
