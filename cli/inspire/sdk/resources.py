"""Name resolution and resource discovery; no Click or output dependencies."""

from __future__ import annotations

import base64
import json
from functools import wraps
from typing import Callable, TypeVar, cast, Any
from inspire.platform.web.browser_api.images import CustomImageInfo
from .models_serving import ImageRegisterHandle
from .models_resources import ProjectInfo, ProjectDetail, ProjectOwner, ProjectOwnerRef, ImageDetail
from .exceptions import (
    InspireError,
    ValidationError,
    ResourceNotFoundError,
    AmbiguousResourceError,
    ResolutionIncompleteError,
)
from .models import (
    ResourceRef,
    WorkspaceRef,
    ProjectRef,
    ComputeGroupRef,
    ImageRef,
    Resource,
    Image,
    ImageSelector,
    Page,
)


F = TypeVar("F", bound=Callable[..., Any])


def operation(fn: F) -> F:
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self.client._transport.scope(timeout=self.client.operation_timeout):
            outer = self.client._catalog_context is None
            if outer:
                self.client._catalog_context = {}
            try:
                return fn(self, *args, **kwargs)
            except InspireError:
                raise
            except Exception as error:
                cause = error.__cause__
                while cause is not None:
                    if isinstance(cause, InspireError):
                        raise cause from None
                    cause = cause.__cause__
                if isinstance(error, ValueError):
                    raise ValidationError(str(error)) from None
                if type(error).__module__.startswith("inspire.platform"):
                    raise ResolutionIncompleteError(str(error)) from None
                raise
            finally:
                if outer:
                    self.client._catalog_context = None

    return cast(F, wrapped)


def image_mutation(fn: F) -> F:
    """Fence readers both before a write and after its outcome, including errors."""
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        self._invalidate_images()
        try:
            result = fn(self, *args, **kwargs)
        except BaseException as error:
            try:
                self._invalidate_images()
            except Exception as cache_error:
                # Preserve submission uncertainty if disk invalidation also fails.
                raise error from cache_error
            raise
        self._invalidate_images()
        return result

    return cast(F, wrapped)


def positive(value, name="limit", maximum=10000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValidationError(f"{name} must be an integer between 1 and {maximum}.")


def exact(items, selector, ref_type, client, workspace_id=None):
    if isinstance(selector, ResourceRef):
        client._validate_ref(selector, ref_type, workspace_id)
        matches = [x for x in items if x.ref.key == selector.key]
    elif isinstance(selector, str) and selector.strip():
        matches = [x for x in items if x.name.casefold() == selector.strip().casefold()]
    else:
        raise ValidationError("Use a non-empty name or the matching resource reference.")
    matches = list({x.ref.key: x for x in matches}.values())
    if not matches:
        raise ResourceNotFoundError("No resource matches this selection in the requested scope.")
    if len(matches) != 1:
        raise AmbiguousResourceError("Multiple resources match; select a reference.", matches)
    return matches[0]


PLATFORM_MAX_ROWS = 5000
"""Deepest row the platform list APIs serve: ``page_num * page_size`` must not exceed it."""


def platform_page(page: int, page_size: int) -> int:
    """Return ``page`` when the platform can serve it; raise before dispatching otherwise."""
    if page * page_size > PLATFORM_MAX_ROWS:
        raise ResolutionIncompleteError(
            f"Platform lists at most {PLATFORM_MAX_ROWS} rows per query; "
            "narrow with status/keyword or stop at max_items."
        )
    return page


class Service:
    def __init__(self, client):
        self.client = client

    @property
    def session(self):
        return self.client._transport.session

    def _catalog(self, kind: str, scope: tuple[Any, ...], load: Callable[[], Any]) -> Any:
        def complete() -> Any:
            rows = load()
            identity_fields = {
                "workspaces": ("id",),
                "projects": ("project_id",),
                "compute_groups": ("id", "logic_compute_group_id"),
                "prices": ("quota_id", "spec_id"),
                "images": ("image_id",),
            }.get(kind)
            if identity_fields:
                for row in rows:
                    if not any(
                        row.get(field) if isinstance(row, dict) else getattr(row, field, None)
                        for field in identity_fields
                    ):
                        raise ResolutionIncompleteError("Catalog omitted a resource identity.")
            if kind == "current_user" and not (rows.get("id") or rows.get("user_id")):
                raise ResolutionIncompleteError("Cannot determine the current user.")
            return rows

        return self.client.cache._get(
            (kind, self.client.account, self.client.base_url, *scope), complete
        )

    def _current_user(self) -> dict[str, Any]:
        from inspire.platform.web import browser_api

        return self._catalog(
            "current_user",
            (),
            lambda: browser_api.get_current_user(session=self.session, refresh=True),
        )

    def _current_user_id(self) -> str:
        user = self._current_user()
        return str(user.get("id") or user.get("user_id")).strip()

    def _current_user_ids(self) -> list[str]:
        return [self._current_user_id()]

    def _fair_scheduling(self, ws: Resource[WorkspaceRef]) -> bool:
        from inspire.platform.web.browser_api.workspaces import is_fair_scheduling_workspace

        def load() -> bool:
            flags = self.client._catalog_context or {}
            if ws.ref.key in flags:
                return flags[ws.ref.key]
            # The browser API also keeps flags on the session, without a TTL.
            # Only reuse GetRoutes data from this operation; otherwise refresh it.
            session = self.session
            session_flags = getattr(session, "all_workspace_fair_scheduling", None)
            if session_flags is not None:
                session_flags.pop(ws.ref.key, None)
            return is_fair_scheduling_workspace(session, ws.ref.key)

        return self._catalog("fair_scheduling", (ws.ref.key,), load)

    def _resolve_priority(self, requested: int | None, ws: Resource[WorkspaceRef], project: Any) -> int:
        from inspire.services.task_priority import resolve_workspace_task_priority

        return resolve_workspace_task_priority(
            requested,
            session=self.session,
            workspace_id=ws.ref.key,
            project_id=project.ref.key,
            fair_scheduling_loader=lambda: self._fair_scheduling(ws),
            projects_loader=lambda: [row[1] for row in self.client.projects._all(ws)],
        )

    def _invalidate_images(self, workspace_id: str | None = None) -> None:
        self.client.cache._invalidate(
            "images",
            self.client.account,
            self.client.base_url,
            *((workspace_id,) if workspace_id else ()),
        )

    def _make_ref(self, cls, name, key, workspace_id=""):
        if not key:
            raise ResolutionIncompleteError("Platform omitted a resource identity.")
        return cls(
            name=name,
            account=self.client.account,
            base_url=self.client.base_url,
            key=str(key),
            workspace_id=workspace_id,
        )

    def _cursor_offset(self, cursor, query):
        query = json.loads(
            json.dumps(
                [self.client.account, self.client.base_url, type(self).__name__, query],
                default=lambda value: (
                    value.to_dict() if isinstance(value, ResourceRef) else str(value)
                ),
            )
        )
        offset = 0
        if cursor is not None:
            try:
                data = json.loads(base64.urlsafe_b64decode(cursor))
                if data["query"] != query or type(data["offset"]) is not int:
                    raise ValueError()
                offset = data["offset"]
                if offset < 0:
                    raise ValueError()
            except Exception:
                raise ValidationError("Cursor does not match this query and account.") from None
        return offset, query

    def _encode_cursor(self, offset, query):
        return base64.urlsafe_b64encode(
            json.dumps({"query": query, "offset": offset}).encode()
        ).decode()

    def _page(self, items, *, limit=20, cursor=None, query=()):
        positive(limit)
        offset, query = self._cursor_offset(cursor, query)
        end = offset + limit
        next_cursor = self._encode_cursor(end, query) if end < len(items) else None
        return Page(tuple(items[offset:end]), next_cursor, len(items))

    def _server_page(
        self, fetch, convert, *, page_size, limit, cursor, query, matches=None,
    ):
        """Page by platform row offsets, applying optional filters after fetching."""
        positive(limit)
        offset, fingerprint = self._cursor_offset(cursor, query)
        rows, seen, previous = [], set(), None
        for _ in range(100):
            page_num, skip = divmod(offset, page_size)
            items, total = fetch(platform_page(page_num + 1, page_size), page_size)
            values = [convert(item) for item in items]
            keys = tuple(item.ref.key for item in values)
            if keys and keys == previous:
                raise ResolutionIncompleteError("Platform repeated a resource page.")
            previous = keys
            if len(items) <= skip and total is not None and offset < total:
                raise ResolutionIncompleteError("Platform omitted a resource page.")
            exhausted = (
                (page_num * page_size + len(items) >= total)
                if total is not None else len(items) < page_size
            )
            for item in values[skip:]:
                offset += 1
                if item.ref.key not in seen and (matches is None or matches(item)):
                    rows.append(item)
                    seen.add(item.ref.key)
                    if len(rows) == limit:
                        more = offset < page_num * page_size + len(items) or not exhausted
                        return Page(
                            tuple(rows),
                            self._encode_cursor(offset, fingerprint) if more else None,
                            total if matches is None else None,
                        )
            if exhausted:
                return Page(tuple(rows), None, total if matches is None else None)
            if len(items) < page_size:
                raise ResolutionIncompleteError("Platform omitted a resource page.")
            offset = (page_num + 1) * page_size
        raise ResolutionIncompleteError("Resource scan exceeded 100 pages; narrow the query.")

    def _collect_pages(self, fetch, identity, *, page_size=100):
        """Enumerate before local paging or exact selection; never hide a partial catalog."""
        items = []
        seen = set()
        for page in range(1, 101):
            rows, total = fetch(page=page, page_size=page_size)
            for row in rows:
                key = identity(row)
                if not key:
                    raise ResolutionIncompleteError("Platform omitted a resource identity.")
                if key in seen:
                    continue
                seen.add(key)
                items.append(row)
            if len(items) >= total:
                return items
            if not rows:
                break
        raise ResolutionIncompleteError("Resource catalog enumeration is incomplete.")


class Workspaces(Service):
    def _routes(self) -> list[dict[str, Any]]:
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

        rows = self._catalog(
            "workspaces",
            (),
            lambda: try_enumerate_workspaces(self.session, base_url=self.client.base_url),
        )
        if self.client._catalog_context is not None:
            self.client._catalog_context.update(
                {
                    row["id"]: row["is_fair_workspace"] is True
                    for row in rows
                    if "is_fair_workspace" in row
                }
            )
        return rows

    def _all(self):
        rows = self._routes()
        return [
            Resource(x["name"], self._make_ref(WorkspaceRef, x["name"], x["id"], x["id"]))
            for x in rows
        ]

    @operation
    def list(self, *, limit: int = 20, cursor: str | None = None) -> Page[Resource[WorkspaceRef]]:
        return self._page(self._all(), limit=limit, cursor=cursor)

    @operation
    def get(self, ref: str | WorkspaceRef) -> Resource[WorkspaceRef]:
        if isinstance(ref, WorkspaceRef):
            self.client._validate_ref(ref, WorkspaceRef)
            return Resource(ref.name, ref)
        return exact(self._all(), ref, WorkspaceRef, self.client)


class Projects(Service):
    def _all(self, ws=None):
        from inspire.platform.web import browser_api
        from inspire.services.projects import project_to_dict
        from .models_resources import ProjectInfo

        rows = (
            self._catalog(
                "projects",
                (ws.ref.key,),
                lambda: browser_api.list_projects(workspace_id=ws.ref.key, session=self.session),
            )
            if ws
            else self._catalog(
                "projects", (None,), lambda: browser_api.list_all_projects(session=self.session)
            )
        )
        return [
            (
                ProjectInfo.from_view(
                    project_to_dict(x),
                    ref=self._make_ref(ProjectRef, x.name, x.project_id, ws.ref.key if ws else ""),
                ),
                x,
            )
            for x in rows
        ]

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[ProjectInfo]:
        ws = self.client.workspaces.get(workspace) if workspace is not None else None
        return self._page(
            [x[0] for x in self._all(ws)],
            limit=limit,
            cursor=cursor,
            query=(ws.ref.key if ws else None,),
        )

    @operation
    def get(
        self, ref: str | ProjectRef, *, workspace: str | WorkspaceRef | None = None
    ) -> ProjectInfo:
        ws = self.client.workspaces.get(workspace) if workspace is not None else None
        return exact(
            [x[0] for x in self._all(ws)],
            ref,
            ProjectRef,
            self.client,
            ws.ref.key if ws else None,
        )

    @operation
    def detail(
        self,
        ref: str | ProjectRef,
        *,
        workspace: str | WorkspaceRef | None = None,
    ) -> ProjectDetail:
        from inspire.platform.web import browser_api
        from inspire.services.projects import project_detail_view

        if isinstance(ref, ProjectRef):
            ws = self.client.workspaces.get(workspace) if workspace is not None else None
            self.client._validate_ref(ref, ProjectRef, ws.ref.key if ws else None)
        else:
            ref = self.get(ref, workspace=workspace).ref
        data = browser_api.get_project_detail(ref.key, session=self.session)
        try:
            usage = browser_api.get_project_budget_usage(ref.key, session=self.session)
        except Exception:
            usage = None
        return ProjectDetail.from_view(project_detail_view(data, usage), ref=ref)

    @operation
    def owners(self) -> tuple[ProjectOwner, ...]:
        from inspire.platform.web import browser_api
        from inspire.services.projects import owner_views

        rows = browser_api.list_project_owners(session=self.session)
        return tuple(
            ProjectOwner.from_view(
                view,
                ref=self._make_ref(ProjectOwnerRef, view["name"], row.get("id") or row.get("user_id"))
                if row.get("id") or row.get("user_id")
                else None,
            )
            for row in rows
            for view in owner_views([row])
        )


class ComputeGroups(Service):
    def _all(self, ws):
        from inspire.platform.web.browser_api.availability import list_compute_groups

        rows = self._catalog(
            "compute_groups",
            (ws.ref.key,),
            lambda: list_compute_groups(workspace_id=ws.ref.key, session=self.session),
        )
        return [
            (
                Resource(
                    str(x.get("name") or x.get("logic_compute_group_name") or ""),
                    self._make_ref(
                        ComputeGroupRef,
                        str(x.get("name") or x.get("logic_compute_group_name") or ""),
                        x.get("id") or x.get("logic_compute_group_id"),
                        ws.ref.key,
                    ),
                ),
                x,
            )
            for x in rows
        ]

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Resource[ComputeGroupRef]]:
        ws = self.client.workspaces.get(workspace)
        return self._page(
            [x[0] for x in self._all(ws)], limit=limit, cursor=cursor, query=(ws.ref.key,)
        )

    @operation
    def get(
        self, ref: str | ComputeGroupRef, *, workspace: str | WorkspaceRef | None = None
    ) -> Resource[ComputeGroupRef]:
        if workspace is None:
            if not isinstance(ref, ComputeGroupRef):
                raise ValidationError("workspace is required when selecting by name.")
            self.client._validate_ref(ref, ComputeGroupRef)
            workspace = WorkspaceRef("", ref.account, ref.base_url, ref.workspace_id, ref.workspace_id)
        ws = self.client.workspaces.get(workspace)
        return exact(
            [x[0] for x in self._all(ws)], ref, ComputeGroupRef, self.client, ws.ref.key
        )


class Images(Service):
    SOURCES = ("official", "public", "project", "private")

    def _all(self, ws, source=None, keyword=None, *, require_complete=False):
        from inspire.platform.web import browser_api
        from inspire.services import images

        sources = self.SOURCES if source in (None, "all") else (source.lower(),)
        rows, failed = [], []
        for key in sources:
            def load_source(source: str = key) -> list[CustomImageInfo]:
                return browser_api.list_images_by_source(
                    source=source, workspace_id=ws.ref.key, session=self.session
                )

            try:
                rows.extend(
                    self._catalog(
                        "images",
                        (ws.ref.key, key),
                        load_source,
                    )
                )
            except Exception as error:
                if len(sources) == 1:
                    raise
                failed.append(error)
        if failed and (require_complete or not rows):
            raise ResolutionIncompleteError("An image catalog could not be read.") from failed[0]
        rows = images.dedupe_images_by_id(rows)
        return [
            Image(
                images.image_label(x),
                self._make_ref(ImageRef, images.image_label(x), x.image_id, ws.ref.key),
                images.image_visibility(x) or source or "",
                x.url,
                x.status,
                x.framework,
                images.image_visibility(x),
            )
            for x in rows
            if not keyword or keyword.strip().casefold() in images.image_label(x).casefold()
        ]

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        *,
        source: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Image]:
        ws = self.client.workspaces.get(workspace)
        return self._page(
            self._all(ws, source, keyword),
            limit=limit,
            cursor=cursor,
            query=(ws.ref.key, source, keyword),
        )

    @operation
    def get(
        self, ref: str | ImageRef | ImageSelector, *, workspace: str | WorkspaceRef | None = None
    ) -> Image:
        if workspace is None:
            if not isinstance(ref, ImageRef):
                raise ValidationError("workspace is required when selecting by name.")
            self.client._validate_ref(ref, ImageRef)
            workspace = WorkspaceRef(
                "", ref.account, ref.base_url, ref.workspace_id, ref.workspace_id
            )
        ws = self.client.workspaces.get(workspace)
        if isinstance(ref, ImageRef):
            from inspire.platform.web import browser_api
            from inspire.services import images

            self.client._validate_ref(ref, ImageRef, ws.ref.key)
            row = browser_api.get_image_detail(image_id=ref.key, session=self.session)
            if not row:
                raise ResourceNotFoundError("Image no longer exists or is not visible.")
            return Image(
                images.image_label(row),
                ref,
                images.image_visibility(row),
                row.url,
                row.status,
                row.framework,
                images.image_visibility(row),
            )
        if isinstance(ref, str) and "/" in ref:
            return Image(ref, self._make_ref(ImageRef, ref, ref, ws.ref.key), "url", ref)
        source = ref.source if isinstance(ref, ImageSelector) else None
        name = ref.name if isinstance(ref, ImageSelector) else ref
        rows = self._all(ws, source, require_complete=True)
        if isinstance(name, str) and ":" not in name:
            rows = [
                row
                for row in rows
                if row.name.split(":", 1)[0].casefold() == name.strip().casefold()
            ]
            if len(rows) > 1:
                raise AmbiguousResourceError("Multiple resources match; select a reference.", rows)
            if rows:
                return rows[0]
        return exact(rows, name, ImageRef, self.client, ws.ref.key)

    @operation
    def detail(
        self,
        ref: str | ImageRef | ImageSelector,
        *,
        workspace: str | WorkspaceRef | None = None,
    ) -> ImageDetail:
        from inspire.platform.web import browser_api
        from inspire.services.images import image_summary

        resolved = self._write_ref(ref, workspace)
        row = browser_api.get_image_detail(image_id=resolved.key, session=self.session)
        return ImageDetail.from_view(image_summary(row), ref=resolved)

    def _write_ref(self, ref, workspace=None):
        if isinstance(ref, ImageRef):
            ws = self.client.workspaces.get(workspace).ref.key if workspace is not None else None
            self.client._validate_ref(ref, ImageRef, ws)
            return ref
        if workspace is None:
            raise ValidationError("workspace is required when selecting an image by name.")
        return self.get(ref, workspace=workspace).ref

    @operation
    @image_mutation
    def register(
        self,
        name: str,
        *,
        workspace: str | WorkspaceRef,
        version: str | None = None,
        description: str | None = None,
        visibility: str | None = None,
        operation_id: str | None = None,
    ) -> ImageRegisterHandle:
        from uuid import uuid4
        from inspire.platform.web import browser_api
        from inspire.services.image_writes import (
            parse_visibility_value,
            IMAGE_ADD_METHOD_LOCAL_PUSH,
        )
        from .exceptions import SubmissionUncertainError

        identifier = uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        ws = self.client.workspaces.get(workspace)
        session = self.session
        visibility_value = parse_visibility_value(visibility or "private")
        assert visibility_value is not None
        with self.client._transport.single_send(identifier, create=True):
            result = browser_api.create_image(
                name=name,
                version=version or "v1",
                workspace_id=ws.ref.key,
                description=description or "",
                visibility=visibility_value,
                add_method=IMAGE_ADD_METHOD_LOCAL_PUSH,
                session=session,
            )
        data = result.get("image") or {}
        key = data.get("image_id") or result.get("image_id")
        if not key:
            raise SubmissionUncertainError(identifier)
        label = f"{name}:{version or 'v1'}"
        return ImageRegisterHandle(
            label,
            self._make_ref(ImageRef, label, key, ws.ref.key),
            identifier,
            data.get("address") or result.get("address") or "",
        )

    def wait_ready(
        self,
        ref: str | ImageRef | ImageSelector,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        workspace: str | WorkspaceRef | None = None,
    ) -> CustomImageInfo:
        from inspire.platform.web import browser_api
        from .compute_jobs import duration
        from .exceptions import WaitTimeoutError

        duration(timeout)
        duration(poll_interval)
        with self.client._transport.scope(timeout=timeout):
            resolved = self._write_ref(ref, workspace)
            try:
                return browser_api.wait_for_image_ready(
                    image_id=resolved.key,
                    session=self.session,
                    timeout=timeout,
                    poll_interval=poll_interval,
                )
            except TimeoutError as exc:
                raise WaitTimeoutError(str(exc)) from exc
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

    @operation
    @image_mutation
    def delete(
        self, ref: str | ImageRef | ImageSelector, *, workspace: str | WorkspaceRef | None = None
    ) -> None:
        from inspire.platform.web import browser_api

        resolved = self._write_ref(ref, workspace)
        session = self.session
        with self.client._transport.single_send():
            browser_api.delete_image(image_id=resolved.key, session=session)

    @operation
    @image_mutation
    def set_visibility(
        self,
        ref: str | ImageRef | ImageSelector,
        *,
        visibility: str,
        workspace: str | WorkspaceRef | None = None,
    ) -> None:
        from inspire.platform.web import browser_api
        from inspire.services.image_writes import parse_visibility_value

        resolved = self._write_ref(ref, workspace)
        value = parse_visibility_value(visibility)
        session = self.session
        with self.client._transport.single_send():
            browser_api.update_image(image_id=resolved.key, visibility=value, session=session)
