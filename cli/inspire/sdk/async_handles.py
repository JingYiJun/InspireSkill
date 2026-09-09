"""Client-bound submission handles. Persist their pure-data refs, not handles.

Waiting polls the platform with the facade's budgets and renewal rules.
CANCELLING A WAIT DOES NOT STOP THE REMOTE WORKLOAD: paid compute keeps running.
Use the workload facade's stop() explicitly to stop it. Repeated awaits poll again.
Default raise_on_failure=False returns failed workloads rather than raising.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TYPE_CHECKING, TypeVar

from .models import JobHandle, Job, WorkspaceRef
from .models_compute import HPCJobHandle, HPCJob, RayJobHandle, RayJob
from .models_notebooks import NotebookHandle, Notebook, ImageSaveHandle
from .models_serving import (
    ServingHandle,
    Serving,
    TensorboardHandle,
    Tensorboard,
    ImageRegisterHandle,
)
from inspire.platform.web.browser_api.images import CustomImageInfo

if TYPE_CHECKING:
    from . import async_client

T = TypeVar("T")


class _AwaitableHandle(Generic[T]):
    async def wait(self) -> T:
        raise NotImplementedError

    def __await__(self) -> Generator[Any, None, T]:
        """Poll again using facade defaults; failed workloads return by default.

        CANCELLATION ONLY STOPS WAITING, NOT THE WORKLOAD. Use facade.stop().
        Polling uses the same request budgets and session renewal as facade.wait().
        """
        return self.wait().__await__()

    def future(self) -> asyncio.Future[T]:
        """Schedule a fresh wait on the running loop (no cached/shared future).

        Suitable for asyncio.wait/as_completed. Cancelling this future only stops
        waiting: the remote workload keeps running and may keep charging.
        This is an asyncio Future, not a concurrent.futures.Future for threads.
        """
        return asyncio.get_running_loop().create_task(self.wait())


@dataclass(frozen=True)
class AsyncJobHandle(_AwaitableHandle[Job], JobHandle):
    """Awaitable jobs submission; cancellation never stops remote work."""

    _facade: async_client.AsyncJobs = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        workspace: str | WorkspaceRef | None = None,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
    ) -> Job:
        """Poll via jobs.wait with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            workspace=workspace,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
        )


@dataclass(frozen=True)
class AsyncNotebookHandle(_AwaitableHandle[Notebook], NotebookHandle):
    """Awaitable notebooks submission; cancellation never stops remote work."""

    _facade: async_client.AsyncNotebooks = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        target: Literal["RUNNING", "STOPPED"] = "RUNNING",
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> Notebook:
        """Poll via notebooks.wait with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            target=target,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncHPCJobHandle(_AwaitableHandle[HPCJob], HPCJobHandle):
    """Awaitable hpc submission; cancellation never stops remote work."""

    _facade: async_client.AsyncHPC = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> HPCJob:
        """Poll via hpc.wait with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncRayJobHandle(_AwaitableHandle[RayJob], RayJobHandle):
    """Awaitable ray submission; cancellation never stops remote work."""

    _facade: async_client.AsyncRay = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> RayJob:
        """Poll via ray.wait with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncServingHandle(_AwaitableHandle[Serving], ServingHandle):
    """Awaitable servings submission; cancellation never stops remote work."""

    _facade: async_client.AsyncServings = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
        target: str = "RUNNING",
    ) -> Serving:
        """Poll via servings.wait with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
            target=target,
        )


@dataclass(frozen=True)
class AsyncTensorboardHandle(_AwaitableHandle[Tensorboard], TensorboardHandle):
    """Awaitable tensorboards submission; cancellation never stops remote work."""

    _facade: async_client.AsyncTensorboards = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        target: str = "running",
        raise_on_failure: bool = False,
        timeout: float = 60,
        poll_interval: float = 3,
        workspace: str | WorkspaceRef | None = None,
    ) -> Tensorboard:
        """Poll via tensorboards.wait with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            target=target,
            raise_on_failure=raise_on_failure,
            timeout=timeout,
            poll_interval=poll_interval,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncImageSaveHandle(_AwaitableHandle[CustomImageInfo], ImageSaveHandle):
    """Awaitable notebooks submission; cancellation never stops remote work."""

    _facade: async_client.AsyncNotebooks = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
    ) -> CustomImageInfo:
        """Poll via notebooks.wait_image_ready with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait_image_ready(
            self,
            timeout=timeout,
            poll_interval=poll_interval,
        )


@dataclass(frozen=True)
class AsyncImageRegisterHandle(_AwaitableHandle[CustomImageInfo], ImageRegisterHandle):
    """Awaitable images submission; cancellation never stops remote work."""

    _facade: async_client.AsyncImages = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        workspace: str | WorkspaceRef | None = None,
    ) -> CustomImageInfo:
        """Poll via images.wait_ready with identical defaults and budgets.

        CANCELLATION DOES NOT STOP REMOTE WORK; explicitly use facade.stop().
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait_ready(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            workspace=workspace,
        )
