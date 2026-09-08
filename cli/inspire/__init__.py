"""Inspire Training Platform SDK."""

from typing import TYPE_CHECKING

__version__ = "7.1.8"


def __getattr__(name: str):
    from importlib import import_module

    sdk = import_module(".sdk", __name__)
    if name in sdk.__all__:
        return getattr(sdk, name)
    raise AttributeError(name)


if TYPE_CHECKING:
    from .sdk import (
        InspireClient as InspireClient,
        ResourceRef as ResourceRef,
        WorkspaceRef as WorkspaceRef,
        ProjectRef as ProjectRef,
        ComputeGroupRef as ComputeGroupRef,
        ImageRef as ImageRef,
        QuotaRef as QuotaRef,
        JobRef as JobRef,
        Resource as Resource,
        Image as Image,
        Quota as Quota,
        QuotaOption as QuotaOption,
        ImageSelector as ImageSelector,
        Job as Job,
        JobHandle as JobHandle,
        Page as Page,
        JobCreateSpec as JobCreateSpec,
        JobPlan as JobPlan,
        LogResult as LogResult,
        EventResult as EventResult,
        InspireError as InspireError,
        ConfigurationError as ConfigurationError,
        AuthenticationError as AuthenticationError,
        AuthenticationCooldownError as AuthenticationCooldownError,
        ClientClosedError as ClientClosedError,
        ClientThreadError as ClientThreadError,
        ValidationError as ValidationError,
        ResourceNotFoundError as ResourceNotFoundError,
        AmbiguousResourceError as AmbiguousResourceError,
        ResolutionIncompleteError as ResolutionIncompleteError,
        TransportError as TransportError,
        SubmissionUncertainError as SubmissionUncertainError,
        MutationUncertainError as MutationUncertainError,
        WaitTimeoutError as WaitTimeoutError,
        JobFailedError as JobFailedError,
    )
