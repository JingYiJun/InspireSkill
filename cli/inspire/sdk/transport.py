"""Compatibility exports for the shared platform transport."""

from inspire.platform.web.transport import (
    Transport as Transport,
    _classify_after_dispatch as _classify_after_dispatch,
    _SingleSendViolation as _SingleSendViolation,
    _NonJSONResponse as _NonJSONResponse,
    os as os,
    threading as threading,
    time as time,
    AuthenticationError as AuthenticationError,
    AuthenticationCooldownError as AuthenticationCooldownError,
    ClientClosedError as ClientClosedError,
    ClientThreadError as ClientThreadError,
    TransportError as TransportError,
    ValidationError as ValidationError,
    SubmissionUncertainError as SubmissionUncertainError,
    MutationUncertainError as MutationUncertainError,
    WaitTimeoutError as WaitTimeoutError,
)
