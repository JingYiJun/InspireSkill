"""Model registry reads using the CLI's public views."""

from __future__ import annotations

from inspire.platform.web import browser_api
from inspire.services import models as views
from inspire.services.collections import bound_collection
from .exceptions import ResourceNotFoundError, ValidationError
from .models import Page, WorkspaceRef, ProjectRef
from .models_resources import ModelRef, ModelInfo, ModelStatus, ModelVersion, ModelDeployConfig
from .resources import Service, operation, exact


class Models(Service):
    def _all(self, workspace, project=None, keyword=None):
        if isinstance(workspace, str) and workspace.strip().casefold() == "all":
            workspace = "all"
        workspaces = (
            self.client.workspaces._all()
            if workspace == "all"
            else [self.client.workspaces.get(workspace)]
        )
        user_id = views.current_user_id(self.session)
        items = []
        matched = project is None
        for ws in workspaces:
            try:
                project_id = (
                    self.client.projects.get(project, workspace=ws.ref).ref.key
                    if project is not None
                    else None
                )
            except ResourceNotFoundError:
                if workspace == "all":
                    continue
                raise
            matched = True
            kwargs = dict(
                workspace_id=ws.ref.key,
                keyword=keyword,
                project_ids=[project_id] if project_id else None,
                user_id=user_id,
                session=self.session,
            )
            rows = self.collect_pages(
                lambda **paging: browser_api.list_models(**paging, **kwargs), lambda x: x.model_id
            )
            items.extend(
                (
                    ModelInfo.from_view(
                        views.model_list_view(x, workspace=ws.name),
                        ref=self.ref(ModelRef, x.name, x.model_id, ws.ref.key),
                    ),
                    x,
                )
                for x in rows
            )
        if not matched:
            raise ResourceNotFoundError(
                f"Unknown project name {project!r} in the requested workspaces."
            )
        if workspace == "all":
            items.sort(key=lambda x: str(x[1].updated_at or x[1].created_at or ""), reverse=True)
        return items

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        project: str | ProjectRef | None = None,
        keyword: str | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[ModelInfo]:
        return self.page(
            [x[0] for x in self._all(workspace, project, keyword)],
            limit=limit,
            cursor=cursor,
            query=(workspace, project, keyword),
        )

    @operation
    def get(
        self,
        name_or_ref: str | ModelRef,
        workspace: str | WorkspaceRef,
        project: str | ProjectRef | None = None,
    ) -> ModelInfo:
        ws = self.client.workspaces.get(workspace)
        return exact(
            [x[0] for x in self._all(ws.ref, project)],
            name_or_ref,
            ModelRef,
            self.client,
            ws.ref.key,
        )

    def _ref(self, selector, workspace, project):
        ws = self.client.workspaces.get(workspace)
        if isinstance(selector, ModelRef):
            self.client._validate_ref(selector, ModelRef, ws.ref.key)
            return selector
        return self.get(selector, ws.ref, project).ref

    @operation
    def status(
        self,
        name_or_ref: str | ModelRef,
        workspace: str | WorkspaceRef,
        project: str | ProjectRef | None = None,
    ) -> ModelStatus:
        ref = self._ref(name_or_ref, workspace, project)
        kwargs = dict(session=self.session, workspace_id=ref.workspace_id)
        data = browser_api.get_model_detail(ref.key, **kwargs)
        records = browser_api.list_model_version_records(ref.key, **kwargs)
        compatibility = browser_api.get_model_vllm_compatibility(ref.key, **kwargs)
        view = views.model_detail_view(ref.name, data, records, vllm_compatibility=compatibility)
        pending = browser_api.check_model_inference_serving_pending(model_id=ref.key, **kwargs)
        view["pending_serving"] = pending.get("has_pending_serving") is True
        reported = views.reported_version(data, records)
        if reported is not None:
            servings, _ = browser_api.list_model_inference_servings(
                model_id=ref.key,
                version=reported,
                page=1,
                page_size=views.SERVING_PAGE_SIZE,
                **kwargs,
            )
            page = bound_collection(views.serving_views(servings), limit=20)
            view["servings"] = page.items
            view.update({f"servings_{k}": v for k, v in page.metadata().items()})
        in_use = views.other_versions_in_use(records, reported=reported)
        if in_use:
            view["other_versions_in_use"] = in_use
        return ModelStatus.from_view(view, ref=ref)

    @operation
    def versions(
        self,
        name_or_ref: str | ModelRef,
        workspace: str | WorkspaceRef,
        project: str | ProjectRef | None = None,
    ) -> tuple[ModelVersion, ...]:
        ref = self._ref(name_or_ref, workspace, project)
        records = browser_api.list_model_version_records(
            ref.key, session=self.session, workspace_id=ref.workspace_id
        )
        compatibility = browser_api.get_model_vllm_compatibility(
            ref.key, session=self.session, workspace_id=ref.workspace_id
        )
        return tuple(
            ModelVersion.from_view(x)
            for x in views.model_version_views(records, vllm_compatibility=compatibility)
        )

    @operation
    def deploy_config(
        self,
        name_or_ref: str | ModelRef,
        workspace: str | WorkspaceRef,
        project: str | ProjectRef | None = None,
        version: int | None = None,
    ) -> ModelDeployConfig:
        model = self.get(name_or_ref, workspace, project)
        if version is None:
            version = views.version_number(model.version)
        if version is None:
            raise ValidationError("Could not infer the model version. Pass --version explicitly.")
        kwargs = dict(version=version, session=self.session, workspace_id=model.ref.workspace_id)
        recommended = browser_api.get_model_recommended_config(model.ref.key, **kwargs)
        compatible = browser_api.check_model_vllm_compatible(model.ref.key, **kwargs)
        view = views.model_deploy_config_view(model.name, version, recommended, compatible)
        return ModelDeployConfig.from_view(view)
