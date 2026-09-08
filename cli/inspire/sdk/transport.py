"""Client-owned transport. Writes are sent once, with no implicit replay."""

from __future__ import annotations
import os
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, parse_qs
from typing import Any
from enum import Enum

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


class OperationPolicy(Enum):
    READ = "read"
    CREATE = "create"
    MUTATION = "mutation"


# Reviewed SDK Actions only. Unknown endpoints never gain retries from their
# HTTP verb or from a suggestive name. Add new read capabilities explicitly.
_READ_ACTIONS = frozenset(
    {
        "GetRoutes",
        "GetUserDetail",
        "ListProjects",
        "GetProjectForPage",
        "GetProjectDetail",
        "ListLogicComputeGroups",
        "ListImages",
        "GetLogicComputeGroupResourceSpecPrices",
        "GetTrainScheduleConfig",
        "GetScheduleConfig",
        "ListJobs",
        "GetJob",
        "ListJobInstances",
        "ListJobEvents",
        "GetJobLog",
    }
)


def operation_policy(path: str) -> OperationPolicy:
    action = parse_qs(urlsplit(path).query).get("Action", [""])[0]
    if action in _READ_ACTIONS:
        return OperationPolicy.READ
    if action == "CreateJobConsole":
        return OperationPolicy.CREATE
    return OperationPolicy.MUTATION


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
        self.operation_id = ""

    def check(self):
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
    def scope(self, *, timeout: float = 120):
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

    def _validate_session(self, session):
        if (
            session is None
            or not session.storage_state.get("cookies")
            or (session.base_url or "").rstrip("/") != self.base_url
            or session.account not in (None, self.account)
            or (self.username and (session.login_username or "") != self.username)
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

    def _refresh(self):
        if not self.allow_browser:
            raise AuthenticationError(
                "Authentication requires browser access; allow_browser is False."
            )
        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.session.refresh_lock import exclusive_session_refresh
        from inspire.platform.web.session.auth import get_web_session

        previous = self._session.created_at if self._session else None
        try:
            with exclusive_session_refresh(self.account, timeout=self.remaining()):
                cached = WebSession.load(allow_expired=True, account=self.account)
                if cached and cached.created_at != previous:
                    try:
                        self._session = self._validate_session(cached)
                        return
                    except AuthenticationError:
                        pass
                self._session = self._validate_session(
                    get_web_session(force_refresh=True, account=self.account)
                )
        except AuthenticationError:
            raise
        except Exception as error:
            retry_at = getattr(error, "retry_at", None)
            if isinstance(retry_at, (int, float)):
                raise AuthenticationCooldownError(retry_at) from None
            raise AuthenticationError(
                "Account refresh failed or is cooling down; inspect account authentication."
            ) from None
        finally:
            if self._browser is not None:
                self._browser.close()
                self._browser = None

    def _once(self, method, path, body, timeout, browser=False, referer=None):
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError

        if browser:
            from inspire.platform.web.session.browser_client import _BrowserRequestClient

            if self._browser is None:
                self._browser = _BrowserRequestClient(self.session)
            return self._browser.request_json(
                method,
                self.base_url + path,
                body=body,
                timeout=timeout,
            )
        from inspire.platform.web.session.requests import build_requests_session, _configure

        if self._http is None:
            self._http = build_requests_session(self.session, self.base_url)
        else:
            _configure(self._http, self.session, self.base_url)
        response = self._http.request(
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
            raise ValidationError(f"Platform rejected request (HTTP {response.status_code}).")
        return response.json()

    def request(
        self, method: str, path: str, *, body=None, timeout: float = 30, referer=None
    ) -> dict:
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError
        from inspire.platform.web.session.envelope import _v2_result
        from requests.exceptions import RequestException

        policy = operation_policy(path)
        read = policy is OperationPolicy.READ
        create = policy is OperationPolicy.CREATE
        browser, refreshed = False, False
        # Authentication happens before dispatch and cannot be mistaken for a sent write.
        self.session
        for attempt in range(3 if read else 1):
            request_timeout = min(timeout, self.timeout, self.remaining())
            try:
                payload = self._once(method, path, body, request_timeout, browser, referer)
                if not isinstance(payload, dict) or not any(
                    k in payload for k in ("Result", "ResponseMetadata", "data", "code")
                ):
                    raise ValueError("Invalid platform envelope.")
                _v2_result(payload)  # Envelope errors share the same attempt budget.
                return payload
            except (ValidationError, AuthenticationError):
                raise
            except Exception as error:
                if (
                    isinstance(error, ValueError)
                    and not isinstance(error, TransientAPIError)
                    and str(error).startswith("API error:")
                ):
                    raise ValidationError("Platform rejected the operation.") from None
                if not read:
                    # No auth replay, browser fallback or transient retry after dispatch.
                    if create:
                        raise SubmissionUncertainError(self.operation_id) from None
                    raise MutationUncertainError(
                        "Mutation may have succeeded; inspect state."
                    ) from None
                if isinstance(error, SessionExpiredError):
                    if refreshed or not self.allow_browser:
                        raise AuthenticationError(
                            "Session expired; authenticate this account."
                        ) from None
                    self._refresh()
                    refreshed = True
                elif isinstance(error, (RequestException, ValueError)) and not isinstance(
                    error, TransientAPIError
                ):
                    # Only transport/invalid JSON errors merit browser fallback; explicit
                    # business errors must not silently become a second request.
                    if isinstance(error, ValueError) and str(error).startswith("API error:"):
                        raise ValidationError("Platform rejected the read operation.") from None
                    if self.allow_browser:
                        browser = True
                if attempt == 2:
                    raise TransportError("Platform read failed after bounded retries.") from None
                time.sleep(min(0.5 * (2**attempt), self.remaining()))
        raise AssertionError("unreachable")

    def close(self):
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
