"""Shared client/transport errors, independent of front-end resource models."""

class InspireError(Exception):
    retryable = False


class ConfigurationError(InspireError):
    pass


class AuthenticationError(InspireError):
    pass


class AuthenticationCooldownError(AuthenticationError):
    def __init__(self, retry_at: float):
        super().__init__(
            "Account authentication is cooling down; inspect credentials before retrying."
        )
        self.retry_at = retry_at


class ClientClosedError(InspireError):
    pass


class ClientThreadError(InspireError):
    pass


class ValidationError(InspireError, ValueError):
    pass


class ResourceNotFoundError(InspireError):
    pass


class AmbiguousResourceError(InspireError):
    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = tuple(candidates)


class ResolutionIncompleteError(InspireError):
    pass


class TransportError(InspireError):
    retryable = True


class SubmissionUncertainError(InspireError):
    def __init__(self, operation_id: str):
        super().__init__("Submission may have succeeded; inspect jobs before submitting again.")
        self.operation_id = operation_id


class MutationUncertainError(InspireError):
    pass


class WaitTimeoutError(InspireError, TimeoutError):
    pass


