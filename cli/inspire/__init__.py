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
        AccountInfo as AccountInfo,
        AccountCheck as AccountCheck,
        AccountContext as AccountContext,
        Permission as Permission,
        APIKeyInfo as APIKeyInfo,
        APIKeyRef as APIKeyRef,
        ProjectInfo as ProjectInfo,
        ProjectDetail as ProjectDetail,
        ProjectOwner as ProjectOwner,
        ProjectOwnerRef as ProjectOwnerRef,
        ImageDetail as ImageDetail,
        DatasetInfo as DatasetInfo,
        DatasetDetail as DatasetDetail,
        DatasetRef as DatasetRef,
        DatasetVersion as DatasetVersion,
        DatasetVersionRef as DatasetVersionRef,
        DatasetTag as DatasetTag,
        DatasetTagRef as DatasetTagRef,
        DatasetApplication as DatasetApplication,
        DatasetApplicationRef as DatasetApplicationRef,
        DatasetValidation as DatasetValidation,
        ModelInfo as ModelInfo,
        ModelRef as ModelRef,
        ModelStatus as ModelStatus,
        ModelVersion as ModelVersion,
        ModelDeployConfig as ModelDeployConfig,
        ResourceAvailability as ResourceAvailability,
        ResourceUsage as ResourceUsage,
        WorkloadSchedulePolicy as WorkloadSchedulePolicy,
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
        DatasetMount as DatasetMount,
        MetricGroup as MetricGroup,
        JobInstance as JobInstance,
        JobEvent as JobEvent,
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
