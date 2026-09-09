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
    from inspire.platform.web.session.models import WebSession

class _NonJSONResponse(ValueError):
    """Only a decoded HTTP body failure is eligible for CLI browser fallback."""


class _SDKResponsePolicy:
    def response(self, response: Any) -> Any:
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError

        if response.status_code == 401 or 300 <= response.status_code < 400:
            raise SessionExpiredError("Authentication expired.")
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientAPIError("Temporary platform failure.", status=response.status_code)
        if response.status_code == 403:
            raise AuthenticationError("Platform access denied.")
        if response.status_code >= 400:
            raise ValidationError(f"HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    def browser_error(self, error: Exception) -> NoReturn:
        from inspire.platform.web.session.browser_client import _BrowserHTTPError
        from inspire.platform.web.session.models import TransientAPIError

        if isinstance(error, _BrowserHTTPError):
            if error.status == 403:
                raise AuthenticationError(str(error)) from error
            if error.status == 429 or error.status >= 500:
                raise TransientAPIError(str(error), status=error.status) from error
            raise ValidationError(f"HTTP {error.status}: {error.body[:500]}") from error
        raise error


class _CLIResponsePolicy(_SDKResponsePolicy):
    def response(self, response: Any) -> Any:
        from inspire.platform.web import session as web_session

        if response.status_code == 401 or 300 <= response.status_code < 400:
            raise web_session.SessionExpiredError("Session expired or invalid")
        if response.status_code >= 400:
            message = f"API returned {response.status_code}: {response.text}"
            if response.status_code in web_session.TRANSIENT_HTTP_STATUSES:
                raise web_session.TransientAPIError(
                    message, status=response.status_code,
                    retry_after=web_session.retry_after_seconds(response.headers),
                )
            raise ValueError(message)
        try:
            return response.json()
        except ValueError as error:
            raise _NonJSONResponse(str(error)) from error

    def browser_error(self, error: Exception) -> NoReturn:
        from inspire.platform.web import session as web_session

        if web_session.is_playwright_browser_runtime_error(error):
            web_session.close_browser_client()
            web_session.raise_browser_runtime_error(error)
        raise error


_SDK_POLICY = _SDKResponsePolicy()
_CLI_POLICY = _CLIResponsePolicy()


class _SingleSendViolation(RuntimeError):
    pass


def _classify_after_dispatch(error: Exception) -> Exception | None:
    """Return a definite rejection, or None when the write outcome is unknown."""
    from inspire.platform.web.session.models import TransientAPIError

    if isinstance(error, (AuthenticationError, TransportError)):
        return error
    # TransientAPIError is also a ValueError; classify it before business errors.
    if isinstance(error, TransientAPIError):
        if error.status in (None, 429) or str(error).startswith("API error:"):
            return TransportError(str(error))
        return None
    if isinstance(error, ValidationError) or (
        isinstance(error, ValueError) and str(error).startswith("API error:")
    ):
        return ValidationError(str(error))
    return None


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
        self._force_browser = False
        self._unproven_rebuild: float | None = None
        self._generation_lock = threading.RLock()
        self._closed = False
        self._session: Any = None
        self._http: Any = None
        self._browser: Any = None
        self.deadline: float | None = None
        self._write: dict[str, Any] | None = None
        self._last_success: float | None = None

    def check(self) -> None:
        if self._pid != os.getpid() or (
            not self.cli_compat and self._thread != threading.get_ident()
        ):
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
            if (
                self._unproven_rebuild is not None
                and observed_created_at >= self._unproven_rebuild
            ):
                raise web_session.SessionExpiredError(
                    "The session the last rebuild produced was refused as well. Not logging "
                    "in again to replace a login nothing has been able to use."
                )
            if not can_refresh:
                raise web_session.SessionExpiredError(
                    "Session expired again after a single authentication refresh"
                )
            web_session.logger.debug("Web session expired; rebuilding it once for this call.")
            refreshed = self._refresh_expired_session(observed_created_at)
            web_session.refresh_session_in_place(self.session, refreshed)
            self._unproven_rebuild = self.session.created_at
            self._force_browser = False

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

    def _once(self, method, path, body, timeout, browser=False, referer=None, *, policy=None):
        from inspire.platform.web import session as web_session

        policy = policy or (_CLI_POLICY if self.cli_compat else _SDK_POLICY)
        url = self.base_url + path
        headers = {"Referer": referer} if referer else {}
        if not browser:
            kwargs = {"headers": headers, "timeout": timeout, "allow_redirects": False}
            args: tuple[Any, ...]
            if self.cli_compat:
                http = web_session.pooled_requests_session(self.session, url)
                method_upper = method.upper()
                if method_upper not in {"GET", "POST", "DELETE"}:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                if method_upper == "POST":
                    headers["Content-Type"] = "application/json"
                    kwargs["json"] = body or {}
                http_send, args = getattr(http, method_upper.lower()), (url,)
            else:
                from inspire.platform.web.session.requests import build_requests_session, _configure

                if self._http is None:
                    self._http = build_requests_session(self.session, self.base_url)
                else:
                    _configure(self._http, self.session, self.base_url)
                headers["Referer"] = referer or self.base_url + "/jobs/distributedTraining"
                kwargs.update(json=body, timeout=(min(10, timeout), timeout))
                http_send, args = self._http.request, (method, url)
            return policy.response(self._dispatch(http_send, *args, **kwargs))

        from inspire.platform.web.browser_api.core import _in_asyncio_loop, _run_in_thread

        disposable = self.cli_compat and _in_asyncio_loop()

        def send():
            if self.cli_compat:
                client = (
                    web_session.create_browser_client(self.session) if disposable
                    else web_session.get_browser_client(self.session)
                )
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
            return _run_in_thread(send) if disposable else send()
        except Exception as error:
            policy.browser_error(error)

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
        browser, refreshed = self._force_browser if self.cli_compat else False, False
        attempt = 0
        while attempt < (3 if state is None else 1):
            request_timeout = (
                timeout if self.cli_compat and self.deadline is None
                else min(timeout, self.timeout, self.remaining())
            )
            observed_created_at = self.session.created_at
            try:
                payload = self._once(method, path, body, request_timeout, browser, referer)
                if not self.cli_compat and state is None and isinstance(payload, dict):
                    metadata = payload.get("ResponseMetadata")
                    error = metadata.get("Error") if isinstance(metadata, dict) else None
                    if isinstance(error, dict) and _is_transient_v2_error_code(
                        str(error.get("Code") or "")
                    ):
                        raise TransientAPIError(str(error.get("Message") or error.get("Code")))
                if self.cli_compat:
                    with self._generation_lock:
                        if (
                            self._unproven_rebuild is not None
                            and observed_created_at >= self._unproven_rebuild
                        ):
                            self._unproven_rebuild = None
                self._last_success = time.monotonic()
                return payload
            except Exception as error:
                if state is not None:
                    if state["sent"]:
                        classified = _classify_after_dispatch(error)
                        if classified is error:
                            raise
                        if classified is not None:
                            raise classified from error
                        self._uncertain(state, error)
                    raise
                if self.cli_compat:
                    if isinstance(error, SessionExpiredError):
                        self._refresh_cli(observed_created_at, can_refresh=not refreshed)
                        refreshed = True
                        browser = False
                        continue
                    if not browser and isinstance(error, (RequestException, _NonJSONResponse)):
                        if self.allow_browser:
                            self._force_browser = browser = True
                            continue
                    if not isinstance(error, TransientAPIError) or attempt == 2:
                        raise
                    from inspire.platform.web.session.retry import backoff_delay

                    time.sleep(backoff_delay(attempt, error))
                    attempt += 1
                    continue
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
                attempt += 1
        raise AssertionError("unreachable")

    def close(self) -> None:
        if self._closed:
            return
        self.check()
        try:
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
