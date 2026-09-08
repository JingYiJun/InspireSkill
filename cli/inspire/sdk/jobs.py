"""Training-job discovery, submission and observation."""

from __future__ import annotations
from typing import Iterator, Sequence
import builtins
from .models import Page, WorkspaceRef, ComputeGroupRef
import math
import time
import uuid
from datetime import datetime, timezone, timedelta
from dataclasses import asdict
from .resources import Service, operation, exact, positive
from .models import (
    Job,
    JobRef,
    JobHandle,
    JobCreateSpec,
    JobPlan,
    Quota,
    QuotaRef,
    QuotaOption,
    ProjectRef,
    LogResult,
    EventResult,
)
from .exceptions import (
    ValidationError,
    ResolutionIncompleteError,
    ResourceNotFoundError,
    AmbiguousResourceError,
    SubmissionUncertainError,
    JobFailedError,
)
from inspire.services.job_status import normalize_status, TERMINAL_STATUSES


class Jobs(Service):
    def _all(self, ws, *, keyword=None):
        from inspire.platform.web.browser_api.jobs import list_jobs

        rows, seen, previous = [], set(), None
        for page in range(1, 101):
            items, total = list_jobs(
                workspace_id=ws.ref.key,
                keyword=keyword,
                page_num=page,
                page_size=100,
                session=self.session,
            )
            keys = tuple(x.job_id for x in items)
            if keys and keys == previous:
                raise ResolutionIncompleteError("Platform repeated a job page.")
            previous = keys
            for item in items:
                if item.job_id not in seen:
                    rows.append(self._job(asdict(item), ws.ref.key))
                    seen.add(item.job_id)
            if not items:
                if (page - 1) * 100 < total:
                    raise ResolutionIncompleteError("Platform omitted a job page.")
                return rows
            if page * 100 >= total:
                return rows
        raise ResolutionIncompleteError("Job scan exceeded 100 pages; narrow the query.")

    def _job(self, data, workspace_id, ref=None):
        name = str(data.get("name") or (ref.name if ref else ""))
        key = data.get("job_id") or (ref.key if ref else "")
        raw = str(data.get("status") or "")
        return Job(
            name,
            ref or self.ref(JobRef, name, key, workspace_id),
            normalize_status(raw),
            raw,
            str(data.get("project_name") or ""),
            str(data.get("created_at") or ""),
            str(data.get("finished_at") or ""),
        )

    @operation
    def list(
        self,
        *,
        workspace: str | WorkspaceRef,
        owner: str = "self",
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Job]:
        if owner != "self":
            raise ValidationError("Only owner='self' is supported.")
        positive(limit)
        if status and status not in (
            "PENDING",
            "QUEUING",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "UNKNOWN",
        ):
            raise ValidationError("Use a normalized Job status.")
        ws = self.client.workspaces.get(workspace)
        from inspire.platform.web.browser_api.jobs import list_jobs

        offset, fingerprint = self.cursor_offset(cursor, (ws.ref.key, owner, status, keyword))
        rows: builtins.list[Job] = []
        seen = set()
        previous = None
        for _ in range(100):
            page_num, skip = divmod(offset, 100)
            items, total = list_jobs(
                workspace_id=ws.ref.key,
                keyword=keyword,
                page_num=page_num + 1,
                page_size=100,
                session=self.session,
            )
            keys = tuple(item.job_id for item in items)
            if keys and keys == previous:
                raise ResolutionIncompleteError("Platform repeated a job page.")
            previous = keys
            if len(items) <= skip and offset < total:
                raise ResolutionIncompleteError("Platform omitted a job page.")
            for item in items[skip:]:
                job = self._job(asdict(item), ws.ref.key)
                if (status is None or job.status == status) and item.job_id not in seen:
                    if len(rows) == limit:
                        return Page(tuple(rows), self.encode_cursor(offset, fingerprint), None)
                    rows.append(job)
                    seen.add(item.job_id)
                offset += 1
            if offset >= total:
                return Page(tuple(rows), None, total if status is None else None)
            if len(items) < 100:
                raise ResolutionIncompleteError("Platform returned a short intermediate job page.")
        raise ResolutionIncompleteError("Job scan exceeded 100 pages; narrow the query.")

    def iter(
        self,
        *,
        workspace: str | WorkspaceRef,
        owner: str = "self",
        status: str | None = None,
        keyword: str | None = None,
        max_items: int | None = None,
    ) -> Iterator[Job]:
        if max_items is not None:
            positive(max_items, "max_items", 100000)
        cursor, seen, count = None, set(), 0
        while True:
            page = self.list(
                workspace=workspace,
                owner=owner,
                status=status,
                keyword=keyword,
                limit=min(100, max_items - count) if max_items else 100,
                cursor=cursor,
            )
            for item in page.items:
                if item.ref.key not in seen:
                    seen.add(item.ref.key)
                    count += 1
                    yield item
                    if max_items is not None and count >= max_items:
                        return
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def _resolve(self, selector, workspace=None):
        if isinstance(selector, JobRef):
            self.client._validate_ref(selector, JobRef)
            if workspace is not None:
                ws = self.client.workspaces.get(workspace)
                self.client._validate_ref(selector, JobRef, ws.ref.key)
            return selector
        if workspace is None:
            raise ValidationError("workspace is required when selecting a job by name.")
        ws = self.client.workspaces.get(workspace)
        return exact(self._all(ws, keyword=selector), selector, JobRef, self.client, ws.ref.key).ref

    @operation
    def get(self, selector: str | JobRef, *, workspace: str | WorkspaceRef | None = None) -> Job:
        from inspire.platform.web.browser_api.jobs import get_job_detail_v2

        ref = self._resolve(selector, workspace)
        data = get_job_detail_v2(ref.key, session=self.session)
        if not data:
            raise ResourceNotFoundError("Job no longer exists or is not visible.")
        if data.get("workspace_id") and data["workspace_id"] != ref.workspace_id:
            raise ValidationError("Job reference workspace does not match platform detail.")
        return self._job(data, ref.workspace_id, ref)

    def _quota_rows(self, ws, group):
        from inspire.platform.web.browser_api.notebooks import get_resource_prices
        from inspire.services.quotas import ResolvedQuota

        rows = get_resource_prices(
            workspace_id=ws.ref.key,
            logic_compute_group_id=group.ref.key,
            schedule_config_type="SCHEDULE_CONFIG_TYPE_TRAIN",
            session=self.session,
        )
        result = []
        for row in rows:
            key = str(row.get("quota_id") or row.get("spec_id") or "")
            if not key:
                raise ResolutionIncompleteError("Quota catalog omitted a tier identity.")
            quota = Quota(
                int(row.get("gpu_count") or 0),
                int(row.get("cpu_count") or 0),
                int(
                    row.get("memory_size_gib")
                    or row.get("memory_size")
                    or row.get("memory_size_gb")
                    or 0
                ),
            )
            gpu = str((row.get("gpu_info") or {}).get("gpu_type") or row.get("gpu_type") or "")
            name = f"{quota.gpu},{quota.cpu},{quota.memory_gib}"
            public = QuotaOption(
                name, self.ref(QuotaRef, name, key, ws.ref.key), quota, group.ref, gpu
            )
            resolved = ResolvedQuota(
                key, group.ref.key, group.name, quota.gpu, quota.cpu, quota.memory_gib, gpu, row
            )
            result.append((public, resolved))
        return result

    @operation
    def quotas(
        self,
        *,
        workspace: str | WorkspaceRef,
        group: str | ComputeGroupRef,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[QuotaOption]:
        ws = self.client.workspaces.get(workspace)
        selected_group = self.client.compute_groups.get(group, workspace=ws.ref)
        return self.page(
            [x[0] for x in self._quota_rows(ws, selected_group)],
            limit=limit,
            cursor=cursor,
            query=(ws.ref.key, selected_group.ref.key),
        )

    def _plan(self, spec):
        from inspire.platform.web.browser_api.workspaces import is_fair_scheduling_workspace
        from inspire.platform.web.browser_api.availability import get_quota_priority_levels
        from inspire.services.job_submission import build_training_job_plan
        from inspire.task_priority import resolve_task_priority

        if not isinstance(spec, JobCreateSpec):
            raise ValidationError("Pass a JobCreateSpec.")
        for name in ("name", "command"):
            if not isinstance(getattr(spec, name), str) or not getattr(spec, name).strip():
                raise ValidationError(f"{name} must be non-empty.")
        positive(spec.nodes, "nodes", 10000)
        if spec.shm_gib is not None:
            positive(spec.shm_gib, "shm_gib", 10000000)
        if spec.max_time_hours is not None and (
            isinstance(spec.max_time_hours, bool)
            or not isinstance(spec.max_time_hours, (int, float))
            or not math.isfinite(spec.max_time_hours)
            or spec.max_time_hours <= 0
        ):
            raise ValidationError("max_time_hours must be finite and positive.")
        if not isinstance(spec.quota, (Quota, QuotaRef)):
            raise ValidationError("quota must be Quota or QuotaRef.")
        ws = self.client.workspaces.get(spec.workspace)
        project_rows = self.client.projects._all(ws)
        project = exact(
            [x[0] for x in project_rows], spec.project, ProjectRef, self.client, ws.ref.key
        )
        project_data = next(x[1] for x in project_rows if x[0].ref.key == project.ref.key)
        from inspire.services.compute_groups import group_supports_workload

        group_rows = self.client.compute_groups._all(ws)
        group = exact(
            [x[0] for x in group_rows], spec.group, ComputeGroupRef, self.client, ws.ref.key
        )
        group_data = next(x[1] for x in group_rows if x[0].ref.key == group.ref.key)
        if not group_supports_workload(group_data, "job"):
            raise ValidationError("Selected compute group does not support training jobs.")
        image = self.client.images.get(spec.image, workspace=ws.ref)
        options = self._quota_rows(ws, group)
        if isinstance(spec.quota, QuotaRef):
            self.client._validate_ref(spec.quota, QuotaRef, ws.ref.key)
            matches = [x for x in options if x[0].ref.key == spec.quota.key]
        else:
            matches = [x for x in options if x[0].quota == spec.quota]
        if not matches:
            raise ResourceNotFoundError("No exact quota tier matches in the selected group.")
        if len(matches) > 1:
            raise AmbiguousResourceError("Multiple quota tiers match.", [x[0] for x in matches])
        public_quota, quota = matches[0]
        priority = resolve_task_priority(
            spec.priority,
            fair_scheduling=is_fair_scheduling_workspace(self.session, ws.ref.key),
            project_limit=project_data.priority_name,
        )
        levels = get_quota_priority_levels(
            ws.ref.key, spec_field="predef_train_spec", session=self.session
        ).get(quota.quota_id)
        if levels and all(x in ("low", "high") for x in levels):
            if ("low" if priority <= 1 else "high") not in levels:
                raise ValidationError("Requested priority is incompatible with this quota tier.")
        plan = build_training_job_plan(
            config=self.client._config,
            name=spec.name,
            command=spec.command,
            quota=quota,
            framework="pytorch",
            project_id=project.ref.key,
            workspace_id=ws.ref.key,
            image=image.url,
            priority=priority,
            nodes=spec.nodes,
            max_time_hours=spec.max_time_hours,
            shm_size=spec.shm_gib,
            description=spec.description,
        )
        return JobPlan(spec.name, ws, project, group, image, public_quota.quota, priority), plan

    @operation
    def plan(self, spec: JobCreateSpec) -> JobPlan:
        return self._plan(spec)[0]

    @operation
    def create(self, spec: JobCreateSpec, *, operation_id: str | None = None) -> JobHandle:
        from inspire.platform.web.browser_api.jobs import create_training_job

        try:
            identifier = str(uuid.UUID(operation_id)) if operation_id else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("operation_id must be a UUID.") from None
        public, plan = self._plan(spec)
        self.client._transport.operation_id = identifier
        data = create_training_job(payload=plan.create_kwargs, session=self.session)
        key = data.get("job_id") or data.get("id")
        if not isinstance(key, str) or not key:
            raise SubmissionUncertainError(identifier)
        return JobHandle(
            spec.name, self.ref(JobRef, spec.name, key, public.workspace.ref.key), identifier
        )

    def wait(
        self,
        selector: str | JobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
    ) -> Job:
        for value in (timeout, poll_interval):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (math.isfinite(value) and value > 0)
            ):
                raise ValidationError("Wait durations must be finite positive seconds.")
        with self.client._transport.scope(timeout=timeout):
            ref = self._resolve(selector, workspace)
            while True:
                self.client._transport.remaining()
                job = self.get(ref)
                if job.status in TERMINAL_STATUSES:
                    if raise_on_failure and job.status != "SUCCEEDED":
                        raise JobFailedError(job)
                    return job
                time.sleep(min(poll_interval, self.client._transport.remaining()))

    @operation
    def stop(self, selector: str | JobRef, *, workspace: str | WorkspaceRef | None = None) -> None:
        from inspire.platform.web.browser_api.jobs import stop_training_job

        ref = self._resolve(selector, workspace)
        self.get(ref)
        stop_training_job(ref.key, session=self.session)

    @operation
    def delete(
        self, selector: str | JobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> None:
        from inspire.platform.web.browser_api.jobs import delete_job

        job = self.get(selector, workspace=workspace)
        if job.status not in TERMINAL_STATUSES:
            raise ValidationError("Stop and wait for a terminal state before deleting a job.")
        delete_job(job.ref.key, session=self.session)

    @operation
    def instances(
        self, selector: str | JobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[str, ...]:
        from inspire.platform.web.browser_api.jobs import list_job_instances

        ref = self._resolve(selector, workspace)
        result, seen = [], set()
        for page in range(1, 101):
            rows, total = list_job_instances(
                ref.key, limit=100, page_num=page, session=self.session
            )
            for row in rows:
                name = str(row.get("name") or row.get("pod_name") or "")
                if not name or name in seen:
                    raise ResolutionIncompleteError("Instance page lacks unique names.")
                seen.add(name)
                result.append(name)
            if page * 100 >= total:
                return tuple(result)
            if not rows:
                break
        raise ResolutionIncompleteError("Instance scan is incomplete.")

    @operation
    def events(
        self,
        selector: str | JobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        limit: int = 100,
    ) -> EventResult:
        from inspire.platform.web.session.envelope import _v2_result

        positive(limit, maximum=500)
        ref = self._resolve(selector, workspace)
        data = _v2_result(
            self.client._transport.request(
                "POST",
                "/api/v2/train?Action=ListJobEvents",
                body={
                    "PageNumber": 1,
                    "page_size": limit + 1,
                    "filter": {"object_type": "job", "object_ids": [ref.key]},
                },
            )
        )
        rows = data.get("events")
        if not isinstance(rows, list):
            raise ResolutionIncompleteError("Event response omitted its collection.")
        return EventResult(tuple(rows[:limit]), len(rows) > limit or data.get("total", 0) > limit)

    @operation
    def logs(
        self,
        selector: str | JobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        instances: str | Sequence[str] = "all",
        window: str | None = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        max_chars: int = 16000,
    ) -> LogResult:
        """Bounded log sample; no unverified last-N or lossless-cursor claim."""
        from inspire.platform.web.browser_api.jobs import list_train_job_logs

        positive(limit, maximum=10000)
        positive(max_chars, "max_chars", 1000000)
        job = self.get(selector, workspace=workspace)
        available = self.instances(job.ref)
        if instances == "all":
            pods = available
        elif (
            isinstance(instances, (tuple, list))
            and instances
            and all(isinstance(x, str) and x in available for x in instances)
        ):
            pods = tuple(dict.fromkeys(instances))
        else:
            raise ValidationError("instances must be 'all' or discovered instance names.")
        if start is not None or end is not None:
            if window is not None or not all(
                isinstance(x, datetime) and x.tzinfo is not None for x in (start, end)
            ):
                raise ValidationError(
                    "Explicit start/end need timezone-aware times and window=None."
                )
        else:
            if window not in ("1h", "24h"):
                raise ValidationError("window must be '1h' or '24h'.")
            end = _timestamp(job.finished_at) if job.status in TERMINAL_STATUSES else None
            end = end or datetime.now(timezone.utc)
            start = end - timedelta(hours=1 if window == "1h" else 24)
            created = _timestamp(job.created_at)
            if created and created < end:
                start = max(start, created)
        if start is None or end is None:
            raise ValidationError("Both start and end are required.")
        if not 0 < (end - start).total_seconds() <= 86400:
            raise ValidationError("Log windows must be positive and at most 24 hours.")
        if not pods:
            return LogResult("", (), start.isoformat(), end.isoformat(), False, 0)
        rows, total = list_train_job_logs(
            pod_names=list(pods),
            job_id=job.ref.key,
            start_timestamp_ms=int(start.timestamp() * 1000),
            end_timestamp_ms=int(end.timestamp() * 1000),
            page_size=limit + 1,
            session=self.session,
        )
        rows.sort(
            key=lambda x: (
                int(x.get("timestamp_ms") or 0),
                str(x.get("pod_name") or ""),
                str(x.get("id") or ""),
            )
        )
        text = "\n".join(
            f"{x.get('timestamp_str', x.get('timestamp_ms', ''))} "
            f"{x.get('pod_name', '')} {x.get('message', '')}"
            for x in rows[:limit]
        )
        return LogResult(
            text[:max_chars],
            pods,
            start.isoformat(),
            end.isoformat(),
            len(text) > max_chars or total > limit or len(rows) > limit,
            total,
        )


def _timestamp(value):
    if not value:
        return None
    try:
        number = float(value)
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else None
        except ValueError:
            return None
