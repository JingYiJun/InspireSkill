"""Public SDK errors, preserving platform and validation messages."""


from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models_notebooks import Notebook


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


class JobFailedError(InspireError):
    def __init__(self, job):
        super().__init__(f"Job reached terminal state {job.status}.")
        self.job = job


class NotebookFailedError(InspireError):
    def __init__(self, notebook: Notebook):
        super().__init__(f"Notebook reached terminal state {notebook.status}.")
        self.notebook = notebook


class HPCJobFailedError(InspireError):
    def __init__(self, job):
        super().__init__(f"HPC job reached terminal state {job.status}.")
        self.job = job


class RayJobFailedError(InspireError):
    def __init__(self, job):
        super().__init__(f"Ray job reached terminal state {job.status}.")
        self.job = job


class ServingFailedError(InspireError):
    def __init__(self, serving):
        self.serving = serving
        super().__init__(f"Serving {serving.name!r} reached {serving.status}.")
