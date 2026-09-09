"""Sans-I/O JSON request decisions; all time and I/O outcomes come from drivers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generator, NoReturn

from inspire.platform.errors import (
    AuthenticationError,
    TransportError,
    ValidationError,
    SubmissionUncertainError,
    MutationUncertainError,
    WaitTimeoutError,
)


class _SingleSendViolation(RuntimeError):
    pass


def _classify_after_dispatch(error: Exception) -> Exception | None:
    """Return a definite rejection, or None when the write outcome is unknown."""
    from inspire.platform.web.session.models import TransientAPIError

    from inspire.platform.web.plaza.core import PlazaRejected

    # Plaza business rejections carry raw envelope messages, without the
    # platform envelope's "API error:" prefix; HTTP status alone is insufficient.
    if isinstance(error, PlazaRejected):
        if error.status == 403:
            return AuthenticationError(str(error))
        if error.status is not None and error.status >= 500:
            return None
        return ValidationError(str(error))
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


@dataclass
class SharedState:
    """Pure cross-call state; the owner serializes generation transitions."""

    force_browser: bool = False
    unproven_rebuild: float | None = None
    last_success: float | None = None
    write: dict[str, Any] | None = None

    def dispatched(self) -> None:
        if self.write is not None:
            self.write["sent"] = True

    def refresh_guard(self, observed: float, can_refresh: bool) -> None:
        from inspire.platform.web.session.models import SessionExpiredError

        if self.unproven_rebuild is not None and observed >= self.unproven_rebuild:
            raise SessionExpiredError(
                "The session the last rebuild produced was refused as well. Not logging "
                "in again to replace a login nothing has been able to use."
            )
        if not can_refresh:
            raise SessionExpiredError("Session expired again after a single authentication refresh")

    def rebuilt(self, generation: float) -> None:
        self.unproven_rebuild = generation
        self.force_browser = False

    def success(self, observed: float, now: float, cli_compat: bool) -> None:
        if cli_compat and self.unproven_rebuild is not None and observed >= self.unproven_rebuild:
            self.unproven_rebuild = None
        self.last_success = now


def claim_write(state: dict[str, Any] | None) -> None:
    if state is not None:
        if state["used"]:
            raise _SingleSendViolation("single_send allows exactly one request.")
        state["used"] = True


def uncertain(state: dict[str, Any], error: Exception) -> NoReturn:
    if state["create"]:
        raise SubmissionUncertainError(state["operation_id"]) from error
    raise MutationUncertainError("Mutation may have succeeded; inspect state.") from error


def remaining(timeout: float, deadline: float | None, now: float) -> float:
    value = timeout if deadline is None else deadline - now
    if value <= 0:
        raise WaitTimeoutError("Operation deadline exceeded; remote resources are unchanged.")
    return value


@dataclass(frozen=True)
class Observe:
    """Acquire the session and sample time and its current generation."""


@dataclass(frozen=True)
class Observation:
    now: float
    generation: float
    jitter: float


@dataclass(frozen=True)
class Send:
    method: str
    path: str
    body: Any
    timeout: float
    browser: bool
    referer: str | None


@dataclass(frozen=True)
class Refresh:
    observed_generation: float
    can_refresh: bool


@dataclass(frozen=True)
class Sleep:
    delay: float


@dataclass(frozen=True)
class Return:
    payload: Any
    observed_generation: float


@dataclass(frozen=True)
class Raise:
    error: Exception


Action = Observe | Send | Refresh | Sleep | Return | Raise
Program = Generator[Action, Any, None]


class RequestCore:
    def __init__(
        self,
        shared: SharedState,
        *,
        cli_compat: bool,
        allow_browser: bool,
        timeout: float,
        deadline: float | None,
    ) -> None:
        self.shared = shared
        self.cli_compat = cli_compat
        self.allow_browser = allow_browser
        self.timeout = timeout
        self.deadline = deadline

    def run(
        self,
        method: str,
        path: str,
        body: Any,
        timeout: float,
        referer: str | None,
    ) -> Program:
        try:
            yield from self._run(method, path, body, timeout, referer)
        except Exception as error:
            yield Raise(error)

    def _run(
        self,
        method: str,
        path: str,
        body: Any,
        timeout: float,
        referer: str | None,
    ) -> Program:
        from inspire.platform.web.transport_policy import is_request_error
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError
        from inspire.platform.web.session.envelope import _is_transient_v2_error_code
        from inspire.platform.web.session.retry import backoff_delay

        state = self.shared.write
        claim_write(state)
        browser = self.shared.force_browser if self.cli_compat else False
        refreshed = False
        attempt = 0
        while attempt < (3 if state is None else 1):
            observation: Observation = yield Observe()
            request_timeout = (
                timeout
                if self.cli_compat and self.deadline is None
                else min(
                    timeout, self.timeout, remaining(self.timeout, self.deadline, observation.now)
                )
            )
            observed = observation.generation
            try:
                payload = yield Send(method, path, body, request_timeout, browser, referer)
                if not self.cli_compat and state is None and isinstance(payload, dict):
                    metadata = payload.get("ResponseMetadata")
                    envelope_error = metadata.get("Error") if isinstance(metadata, dict) else None
                    if isinstance(envelope_error, dict) and _is_transient_v2_error_code(
                        str(envelope_error.get("Code") or "")
                    ):
                        raise TransientAPIError(
                            str(envelope_error.get("Message") or envelope_error.get("Code"))
                        )
                yield Return(payload, observed)
                return
            except Exception as error:
                if state is not None:
                    if state["sent"]:
                        classified = _classify_after_dispatch(error)
                        if classified is error:
                            raise
                        if classified is not None:
                            raise classified from error
                        uncertain(state, error)
                    raise
                if self.cli_compat:
                    if isinstance(error, SessionExpiredError):
                        yield Refresh(observed, not refreshed)
                        refreshed = True
                        browser = False
                        continue
                    # Body decoding is distinguished from local configuration errors.
                    from inspire.platform.web.transport_policy import _NonJSONResponse

                    if (
                        not browser
                        and (is_request_error(error) or isinstance(error, _NonJSONResponse))
                        and self.allow_browser
                    ):
                        self.shared.force_browser = browser = True
                        continue
                    if not isinstance(error, TransientAPIError) or attempt == 2:
                        raise
                    yield Sleep(backoff_delay(attempt, error, jitter=observation.jitter))
                    attempt += 1
                    continue
                if isinstance(error, SessionExpiredError):
                    if refreshed:
                        raise AuthenticationError(str(error)) from error
                    yield Refresh(observed, True)
                    refreshed = True
                elif isinstance(error, (ValidationError, AuthenticationError)):
                    raise
                elif (is_request_error(error) or isinstance(error, ValueError)) and not isinstance(
                    error, TransientAPIError
                ):
                    if self.allow_browser:
                        browser = True
                elif not isinstance(error, TransientAPIError):
                    raise TransportError(str(error)) from error
                if attempt == 2:
                    raise TransportError(str(error)) from error
                clock: Observation = yield Observe()
                yield Sleep(
                    min(0.1 * (2**attempt), remaining(self.timeout, self.deadline, clock.now))
                )
                attempt += 1
        raise AssertionError("unreachable")
