"""Inference serving creation, lifecycle, observations and invocation metadata."""

from __future__ import annotations
import time
from dataclasses import asdict, replace
from uuid import uuid4
from datetime import datetime
from typing import Any, Iterator, Sequence
from inspire.platform.web import browser_api as api
from inspire.services import serving_submission as core, serving_status as statuses
from inspire.services import serving_logs as logs_core, serving_api_metrics as traffic
from inspire.services.serving_output import public_serving, public_configs, sanitize_public_data
from inspire.services.serving_views import public_serving_version, public_scale_history_entry
from inspire.services.serving_instances import (
    ServingInstanceView,
    fetch_serving_instances,
    serving_instance_views,
    select_serving_instance_views,
)
from inspire.services.serving_access import invocation_info
from inspire.services.serving_events import serving_events
from inspire.services.task_priority import resolve_workspace_task_priority
from inspire.services.workload_quota import ensure_priority_allowed
from .compute_jobs import ComputeJobs, WorkloadBinding, duration
from inspire.services.metrics import metric_group
from .resources import operation, exact
from .models import (
    Resource,
    WorkspaceRef,
    ProjectRef,
    Page,
    ComputeGroupRef,
    Quota,
    ImageRef,
    ImageSelector,
    EventResult,
    LogResult,
)
from .models_resources import ModelRef
from .models_serving import Serving, ServingRef, ServingCreateSpec, ServingPlan, ServingHandle
from .exceptions import ValidationError, SubmissionUncertainError, ServingFailedError


class Servings(ComputeJobs[ServingRef, Serving, ServingInstanceView]):
    _kind = "serving"
    _ref_type = ServingRef
    _model = Serving
    _failure = ServingFailedError
    _binding = WorkloadBinding[ServingInstanceView](
        list_page_size=20,
        expand_list=True,
        list_jobs=core.list_servings,
        get_detail=api.get_serving_detail,
        start=api.start_serving,
        stop=api.stop_serving,
        delete=api.delete_serving,
        create=core.create_serving,
        created_id=core.created_serving_id,
        normalize_status=statuses.normalize_status,
        matches_status=statuses.matches_status,
        terminal_statuses=statuses.TERMINAL_STATUSES,
        success_statuses=statuses.SUCCESS_STATUSES,
        public_status=public_serving,
        fetch_instances=fetch_serving_instances,
        instance_views=serving_instance_views,
        select_instance_views=select_serving_instance_views,
        list_logs=logs_core.list_serving_logs,
        labelled_logs=logs_core.labelled_logs,
        log_window=logs_core.workload_log_window,
        log_max_window_ms=30 * 86400 * 1000,
        schedule_config_type="SCHEDULE_CONFIG_TYPE_SERVE",
        task_type="inference_serving",
        metric_group=metric_group,
        expand_tail_fetch=False,
    )

    def _job(self, data, ws, ref=None):
        data = dict(data)
        data["job_id"] = data.get("inference_serving_id") or data.get("id") or ""
        return super()._job(data, ws, ref)

    def _all(self, ws, project=None, keyword=None):
        project_id = self._project(ws, project).ref.key if project is not None else None
        rows = self._collect_pages(
            lambda **paging: api.list_servings(
                workspace_id=ws.ref.key,
                project_ids=[project_id] if project_id else None,
                keyword=keyword,
                session=self.session,
                **paging,
            ),
            lambda row: row.inference_serving_id,
            page_size=self._binding.list_page_size,
            expand_list=self._binding.expand_list,
        )
        return [
            self._job(
                dict(row.raw or {}, **{k: v for k, v in asdict(row).items() if k != "raw"}),
                ws.ref.key,
            )
            for row in rows
        ]

    def _project(self, ws, selector):
        from .models import ProjectRef

        data = api.list_serving_user_project(workspace_id=ws.ref.key, session=self.session)
        rows = [
            Resource(
                str(row.get("project_name") or row.get("name") or ""),
                self._make_ref(
                    ProjectRef,
                    str(row.get("project_name") or row.get("name") or ""),
                    str(row.get("project_id") or row.get("id") or ""),
                    ws.ref.key,
                ),
            )
            for row in data.get("projects", [])
            if isinstance(row, dict)
        ]
        return exact(rows, selector, ProjectRef, self.client, ws.ref.key)

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        *,
        project: str | ProjectRef | None = None,
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Serving]:
        ws = self.client.workspaces.get(workspace)
        rows = [
            r
            for r in self._all(ws, project, keyword)
            if statuses.matches_status(r.raw_status, status)
        ]
        return self._page(
            rows, limit=limit, cursor=cursor, query=(ws.ref.key, project, status, keyword)
        )

    def iter(
        self, workspace: str | WorkspaceRef, *, project: str | ProjectRef | None = None,
        status: str | None = None, keyword: str | None = None, max_items: int | None = None,
    ) -> Iterator[Serving]:
        if max_items is not None and max_items < 1:
            raise ValidationError("max_items must be positive.")
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self.list(workspace, project=project, status=status, keyword=keyword, cursor=cursor)
            for row in page.items:
                if row.ref.key not in seen:
                    seen.add(row.ref.key)
                    yield row
                    if max_items is not None and len(seen) >= max_items:
                        return
            cursor = page.next_cursor
            if cursor is None:
                return

    def _priority_levels(self, ws):
        try:
            return api.get_quota_priority_levels(
                workspace_id=ws.ref.key, spec_field="serving_quota", session=self.session
            )
        except Exception:
            return None

    def _quota(self, ws, group, quota):
        from inspire.services.workload_quota import allowed_priority_levels_for

        resolved = super()._quota(ws, group, quota)
        return replace(
            resolved,
            allowed_priority_levels=allowed_priority_levels_for(
                self._priority_levels(ws), resolved.quota_id, workload="serving"
            ),
        )

    @operation
    def status(
        self,
        refs: Sequence[str | ServingRef],
        *,
        workspace: str | WorkspaceRef | None = None,
    ) -> tuple[Serving, ...]:
        return tuple(self.get(ref, workspace=workspace) for ref in refs)

    @operation
    def plan(self, spec: ServingCreateSpec) -> ServingPlan:
        if not isinstance(spec, ServingCreateSpec):
            raise ValidationError("Pass a ServingCreateSpec.")
        for label, value in [
            ("port", spec.port),
            ("replicas", spec.replicas),
            ("nodes_per_replica", spec.nodes_per_replica),
            ("shm_gib", spec.shm_gib),
            ("model_version", spec.model_version),
        ]:
            if value is not None and (type(value) is not int or value < 1):
                raise ValidationError(f"{label} must be a positive integer.")
        if spec.port > 65535:
            raise ValidationError("port must be between 1 and 65535.")
        domain = core.validate_custom_domain(spec.custom_domain)
        ws = self.client.workspaces.get(spec.workspace)
        project = self._project(ws, spec.project)
        quota = self._quota(ws, spec.group, spec.quota)
        from inspire.services.models import current_user_id

        user_id = current_user_id(self.session)
        if isinstance(spec.model, ModelRef):
            model = self.client.models.get(spec.model, ws.ref)
            model_id, model_label = model.ref.key, model.name
            from inspire.services.models import version_number

            latest = version_number(model.version)
        else:

            def resolve(candidates):
                return exact(
                    [
                        Resource(
                            row["name"], self._make_ref(ModelRef, row["name"], row["id"], ws.ref.key)
                        )
                        for row in candidates
                    ],
                    spec.model,
                    ModelRef,
                    self.client,
                    ws.ref.key,
                ).ref.key

            model_id, latest, model_label = core.resolve_model_for_create(
                name=spec.model,
                workspace_id=ws.ref.key,
                project_id=None,
                user_id=user_id,
                session=self.session,
                resolve=resolve,
            )
        version = spec.model_version or latest
        if version is None:
            raise ValidationError("Could not infer model version. Pass --model-version explicitly.")
        if isinstance(spec.image, (ImageRef, ImageSelector)):
            image = self.client.images.get(spec.image, workspace=ws.ref)
            image_id, image_label = image.ref.key, image.name
        else:
            image_id, image_label = core.resolve_image_for_create(
                spec.image, session=self.session, workspace_id=ws.ref.key
            )
        priority = resolve_workspace_task_priority(
            spec.priority, session=self.session, workspace_id=ws.ref.key, project_id=project.ref.key
        )
        ensure_priority_allowed(quota, priority, quota_command="inspire serving quota")
        kwargs = dict(
            name=spec.name,
            workspace_id=ws.ref.key,
            project_id=project.ref.key,
            logic_compute_group_id=quota.logic_compute_group_id,
            model_id=model_id,
            model_version=version,
            mirror_id=image_id,
            command=spec.command,
            port=spec.port,
            description=spec.description,
            replicas=spec.replicas,
            node_num_per_replica=spec.nodes_per_replica,
            shm_gi=spec.shm_gib,
            task_priority=priority,
            custom_domain=domain,
            resource_spec_price=core.build_resource_spec_price(quota),
            enable_auto_scaling=spec.auto_scaling,
            is_publicpath_readonly=spec.public_path_readonly,
        )
        payload = sanitize_public_data(
            dict(
                dry_run=True,
                name=spec.name,
                workspace=ws.name,
                project=project.name,
                compute_group=quota.compute_group_name,
                resource=dict(
                    gpu=quota.gpu_count, cpu=quota.cpu_count, memory_gib=quota.memory_gib
                ),
                image=image_label,
                model=model_label,
                model_version=version,
                command=spec.command,
                description=spec.description,
                port=spec.port,
                replicas=spec.replicas,
                nodes_per_replica=spec.nodes_per_replica,
                shared_memory_gib=spec.shm_gib,
                priority=priority,
                custom_domain=domain,
                auto_scaling=spec.auto_scaling,
                public_path_readonly=spec.public_path_readonly,
            ),
            omit_urls=True,
        )
        return ServingPlan(
            spec.name,
            ws,
            project,
            Resource(
                quota.compute_group_name,
                self._make_ref(
                    ComputeGroupRef,
                    quota.compute_group_name,
                    quota.logic_compute_group_id,
                    ws.ref.key,
                ),
            ),
            Quota(quota.gpu_count, quota.cpu_count, quota.memory_gib),
            image_id,
            priority,
            kwargs,
            payload,
            model_label,
            version,
        )

    @operation
    def create(
        self,
        spec: ServingCreateSpec,
        *,
        operation_id: str | None = None,
    ) -> ServingHandle:
        identifier = uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        plan = self.plan(spec)
        session = self.session
        with self.client._transport.single_send(identifier, create=True):
            result = api.create_serving(**plan.create_kwargs, session=session)
        key = core.created_serving_id(result)
        if not key:
            raise SubmissionUncertainError(identifier)
        return ServingHandle(
            plan.name, self._make_ref(ServingRef, plan.name, key, plan.workspace.ref.key), identifier
        )

    @operation
    def start(self, ref: str | ServingRef, *, workspace: str | WorkspaceRef | None = None) -> None:
        self._mutate(ref, api.start_serving, workspace)

    @operation
    def scale(
        self,
        ref: str | ServingRef,
        *,
        replicas: int,
        workspace: str | WorkspaceRef | None = None,
    ) -> None:
        if type(replicas) is not int or replicas < 0:
            raise ValidationError("replicas must be a non-negative integer.")
        self._mutate(
            ref, lambda key, **kw: api.scale_serving(key, replica=replicas, **kw), workspace
        )

    @operation
    def rollback(
        self,
        ref: str | ServingRef,
        *,
        version: int,
        workspace: str | WorkspaceRef | None = None,
    ) -> None:
        if type(version) is not int or version < 1:
            raise ValidationError("version must be a positive integer.")
        self._mutate(
            ref, lambda key, **kw: api.rollback_serving(key, version=version, **kw), workspace
        )

    def wait(
        self,
        ref: str | ServingRef,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
        target: str = "RUNNING",
    ) -> Serving:
        duration(timeout)
        duration(poll_interval)
        target = statuses.normalize_status(target)
        with self.client._transport.scope(timeout=timeout):
            resolved = self._resolve(ref, workspace)
            while True:
                self.client._transport.remaining()
                row = self.get(resolved)
                if row.status == target:
                    return row
                if row.status in statuses.TERMINAL_STATUSES:
                    if raise_on_failure:
                        raise ServingFailedError(row)
                    return row
                time.sleep(min(poll_interval, self.client._transport.remaining()))

    @operation
    def versions(
        self, ref: str | ServingRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[dict[str, Any], ...]:
        resolved = self._resolve(ref, workspace)
        rows, _ = api.list_serving_versions(resolved.key, session=self.session)
        return tuple(public_serving_version(row) for row in rows)

    @operation
    def scale_history(
        self, ref: str | ServingRef, *, workspace: str | WorkspaceRef | None = None,
        limit: int = 20, cursor: str | None = None,
    ) -> Page[dict[str, Any]]:
        resolved = self._resolve(ref, workspace)
        rows = self._collect_pages(
            lambda **kw: api.list_serving_scale_history(resolved.key, session=self.session, **kw),
            lambda row: row.get("id") or repr(row),
        )
        return self._page(
            [public_scale_history_entry(row) for row in rows],
            limit=limit,
            cursor=cursor,
            query=(resolved,),
        )

    @operation
    def configs(self, workspace: str | WorkspaceRef) -> dict[str, Any]:
        ws = self.client.workspaces.get(workspace)
        return public_configs(
            api.get_serving_configs(workspace_id=ws.ref.key, session=self.session)
        )

    @operation
    def api(
        self,
        ref: str | ServingRef,
        *,
        affinity_key: str | None = None,
        workspace: str | WorkspaceRef | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve(ref, workspace)
        if affinity_key is not None and (
            not affinity_key
            or len(affinity_key) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in affinity_key)
        ):
            raise ValidationError("Use 1-256 characters without control characters.")
        return invocation_info(self._detail(resolved.key), resolved.name, affinity=affinity_key)

    @operation
    def api_metrics(
        self,
        ref: str | ServingRef,
        *,
        metric: str | None = None,
        window: str = "1h",
        interval: str | None = None,
        workspace: str | WorkspaceRef | None = None,
    ) -> dict[str, Any]:
        from inspire.platform.web.browser_api.metrics import INTERVAL_CHOICES

        interval = interval or "1m"
        if interval not in INTERVAL_CHOICES:
            raise ValidationError(f"Invalid interval {interval!r}")
        metrics = traffic.resolve_api_metrics(metric)
        end = int(time.time())
        start = end - traffic.parse_window(window)
        resolved = self._resolve(ref, workspace)
        rows = api.get_serving_api_metrics(
            resolved.key,
            metric_types=metrics,
            start_timestamp=start,
            end_timestamp=end,
            interval_second=INTERVAL_CHOICES[interval],
            session=self.session,
        )
        return dict(
            resource="serving",
            name=resolved.name,
            metrics=metrics,
            time_range=dict(start=start, end=end, interval=interval),
            series=[traffic.group_summary(row) for row in rows],
        )

    @operation
    def instances(
        self, ref: str | ServingRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[ServingInstanceView, ...]:
        resolved = self._resolve(ref, workspace)
        rows, _ = fetch_serving_instances(resolved.key, session=self.session)
        return tuple(serving_instance_views(rows))

    @operation
    def events(
        self, ref: str | ServingRef, *, workspace: str | WorkspaceRef | None = None,
        type: str | None = None, reason: str | None = None,
        instance: str | Sequence[str] | None = None, workload_level: bool = False,
        limit: int = 100,
    ) -> EventResult:
        from inspire.services.job_events import matching_events

        if workload_level and instance:
            raise ValidationError("--workload-level and --instance cannot be used together.")
        resolved = self._resolve(ref, workspace)
        rows = serving_events(
            resolved.key,
            session=self.session,
            selectors=(instance,) if isinstance(instance, str) else tuple(instance or ()),
            workload_level=workload_level,
        )
        rows = matching_events(rows, type_filter=type, reason_filter=reason)
        selected = rows[-limit:] if limit > 0 else rows
        return EventResult(tuple(selected), len(selected) < len(rows))

    def _follow_event_batch(self, ref, **filters):
        return self.events(ref, **filters)

    @operation
    def logs(
        self, ref: str | ServingRef, *, workspace: str | WorkspaceRef | None = None,
        instance: str | Sequence[str] | None = None, window: str | None = None,
        start: datetime | None = None, end: datetime | None = None,
        tail: int | None = None, head: int | None = None, limit: int | None = None,
    ) -> LogResult:
        from datetime import datetime, timezone
        from inspire.services.job_logs import window_to_minutes, select_job_logs, format_log_line

        if tail is not None and head is not None:
            raise ValidationError("--tail and --head cannot be used together.")
        for value in (tail, head, limit):
            if value is not None and (type(value) is not int or value < 1):
                raise ValidationError("Log record counts must be positive integers.")
        resolved = self._resolve(ref, workspace)
        if start is not None or end is not None:
            if start is None or end is None or end <= start:
                raise ValidationError("Both start and end are required, with end after start.")
            start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        else:
            start_ms, end_ms = logs_core.log_window(window_to_minutes(window) if window else None)
        rows, total, pods = logs_core.fetch_logs(
            resolved.key,
            session=self.session,
            selectors=(instance,) if isinstance(instance, str) else tuple(instance or ()),
            start_ms=start_ms,
            end_ms=end_ms,
            fetch_size=max(limit or 100, tail or 0, head or 0),
        )
        if not pods:
            from .exceptions import ResourceNotFoundError

            raise ResourceNotFoundError(f"No instances found for serving {resolved.name}.")
        selected = select_job_logs(
            rows, total=total, tail=tail, head=head, record_limit=limit or 100, all_output=False
        )
        return LogResult(
            "\n".join(format_log_line(row) for row in selected.logs),
            tuple(pods),
            datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
            datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
            selected.truncated,
            selected.total,
            tuple(selected.logs),
        )
