"""Client-owned transport. Writes are sent once, with no implicit replay."""

from __future__ import annotations
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, NoReturn

from .exceptions import (
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


class _SingleSendViolation(RuntimeError):
    pass


class Transport:
    def __init__(
        self,
        account: str,
        base_url: str,
        *,
        username: str,
        allow_browser: bool = False,
        timeout: float = 30,
    ):
        self.account, self.base_url = account, base_url.rstrip("/")
        self.username, self.allow_browser, self.timeout = username, allow_browser, timeout
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._closed = False
        self._session: Any = None
        self._http: Any = None
        self._browser: Any = None
        self.deadline: float | None = None
        self._write: dict[str, Any] | None = None
        self._last_success: float | None = None

    def check(self) -> None:
        if (self._pid, self._thread) != (os.getpid(), threading.get_ident()):
            raise ClientThreadError("Create and use each Client in the same process and thread.")
        if self._closed:
            raise ClientClosedError("Client is closed.")

    def remaining(self) -> float:
        self.check()
        remaining = self.timeout if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise WaitTimeoutError("Operation deadline exceeded; remote resources are unchanged.")
        return remaining

    @contextmanager
    def scope(self, *, timeout: float = 120) -> Iterator[None]:
        from inspire.accounts import account_scope
        from inspire.platform.web.runtime import active_transport

        self.check()
        old = self.deadline
        self.deadline = min(old, time.monotonic() + timeout) if old else time.monotonic() + timeout
        token = active_transport.set(self)
        try:
            with account_scope(self.account):
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
        # The probe is a READ. Once dispatched, a write (including a 401)
        # remains uncertain and must never be replayed.
        state = {"operation_id": operation_id, "create": create, "used": False, "sent": False}
        self._write = state
        try:
            yield
        except (SubmissionUncertainError, MutationUncertainError, _SingleSendViolation):
            raise
        except Exception as error:
            if state["sent"]:
                self._uncertain(state, error)
            raise
        finally:
            self._write = None

    def _uncertain(self, state: dict[str, Any], error: Exception) -> NoReturn:
        if state["create"]:
            raise SubmissionUncertainError(state["operation_id"]) from error
        raise MutationUncertainError("Mutation may have succeeded; inspect state.") from error

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
            from inspire.platform.web.session.models import WebSession

            cached = WebSession.load(allow_expired=True, account=self.account)
            try:
                self._session = self._validate_session(cached)
            except AuthenticationError:
                if not self.allow_browser:
                    raise
                self._refresh()
        return self._session

    def _adopt_session(self, session):
        from inspire.platform.web.session.requests import _configure

        self._session = self._validate_session(session)
        if self._http is not None:
            _configure(self._http, self._session, self.base_url)

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
                    if (
                        not self.allow_browser
                        or getattr(error, "retry_at", None) is not None
                        or isinstance(error.__cause__, auth._CasVerificationRequired)
                    ):
                        raise
                    self._adopt_session(
                        auth.get_web_session(force_refresh=True, account=self.account)
                    )
        except AuthenticationError:
            raise
        except Exception as error:
            retry_at = getattr(error, "retry_at", None)
            if isinstance(retry_at, (int, float)):
                raise AuthenticationCooldownError(retry_at) from None
            raise AuthenticationError(str(error)) from error
        finally:
            if self._browser is not None:
                self._browser.close()
                self._browser = None

    def _once(self, method, path, body, timeout, browser=False, referer=None):
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError

        if browser:
            from inspire.platform.web.session.browser_client import (
                _BrowserRequestClient,
                _BrowserHTTPError,
            )

            if self._browser is None:
                self._browser = _BrowserRequestClient(self.session)
            try:
                return self._dispatch(
                    self._browser.request_json,
                    method,
                    self.base_url + path,
                    body=body,
                    timeout=timeout,
                )
            except _BrowserHTTPError as error:
                if error.status == 403:
                    raise AuthenticationError(str(error)) from error
                if error.status == 429 or error.status >= 500:
                    raise TransientAPIError(str(error), status=error.status) from error
                raise ValidationError(f"HTTP {error.status}: {error.body[:500]}") from error
        from inspire.platform.web.session.requests import build_requests_session, _configure

        if self._http is None:
            self._http = build_requests_session(self.session, self.base_url)
        else:
            _configure(self._http, self.session, self.base_url)
        response = self._dispatch(
            self._http.request,
            method,
            self.base_url + path,
            json=body,
            headers={"Referer": referer or self.base_url + "/jobs/distributedTraining"},
            allow_redirects=False,
            timeout=(min(10, timeout), timeout),
        )
        if response.status_code == 401 or 300 <= response.status_code < 400:
            raise SessionExpiredError("Authentication expired.")
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientAPIError("Temporary platform failure.", status=response.status_code)
        if response.status_code == 403:
            raise AuthenticationError("Platform access denied.")
        if response.status_code >= 400:
            raise ValidationError(f"HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    def _dispatch(self, send: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._write is not None:
            self._write["sent"] = True
        return send(*args, **kwargs)

    def request(
        self, method: str, path: str, *, body=None, timeout: float = 30, referer=None
    ) -> dict:
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError
        from inspire.platform.web.session.envelope import _is_transient_v2_error_code
        from requests.exceptions import RequestException

        state = self._write
        if state is not None:
            if state["used"]:
                raise _SingleSendViolation("single_send allows exactly one request.")
            state["used"] = True
        self.session
        browser, refreshed = False, False
        for attempt in range(3 if state is None else 1):
            request_timeout = min(timeout, self.timeout, self.remaining())
            try:
                payload = self._once(method, path, body, request_timeout, browser, referer)
                if state is None and isinstance(payload, dict):
                    metadata = payload.get("ResponseMetadata")
                    error = metadata.get("Error") if isinstance(metadata, dict) else None
                    if isinstance(error, dict) and _is_transient_v2_error_code(
                        str(error.get("Code") or "")
                    ):
                        raise TransientAPIError(str(error.get("Message") or error.get("Code")))
                self._last_success = time.monotonic()
                return payload
            except Exception as error:
                if state is not None:
                    if state["sent"]:
                        self._uncertain(state, error)
                    raise
                if isinstance(error, SessionExpiredError):
                    if refreshed:
                        raise AuthenticationError(str(error)) from error
                    self._refresh()
                    refreshed = True
                elif isinstance(error, (ValidationError, AuthenticationError)):
                    raise
                elif isinstance(error, (RequestException, ValueError)) and not isinstance(
                    error, TransientAPIError
                ):
                    if self.allow_browser:
                        browser = True
                elif not isinstance(error, TransientAPIError):
                    raise TransportError(str(error)) from error
                if attempt == 2:
                    raise TransportError(str(error)) from error
                time.sleep(min(0.1 * (2**attempt), self.remaining()))
        raise AssertionError("unreachable")

    def close(self) -> None:
        if self._closed:
            return
        self.check()
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._http is not None:
                self._http.close()
            self._session = self._browser = self._http = None
            self._closed = True
