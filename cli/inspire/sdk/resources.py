"""Name resolution and resource discovery; no Click or output dependencies."""

from __future__ import annotations

import base64
import json
from functools import wraps
from typing import Callable, TypeVar, cast, Any

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


class Service:
    def __init__(self, client):
        self.client = client

    @property
    def session(self):
        return self.client._transport.session

    def ref(self, cls, name, key, workspace_id=""):
        if not key:
            raise ResolutionIncompleteError("Platform omitted a resource identity.")
        return cls(
            name=name,
            account=self.client.account,
            base_url=self.client.base_url,
            key=str(key),
            workspace_id=workspace_id,
        )

    def cursor_offset(self, cursor, query):
        query = json.loads(
            json.dumps(
                [self.client.account, self.client.base_url, type(self).__name__, query], default=str
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

    def encode_cursor(self, offset, query):
        return base64.urlsafe_b64encode(
            json.dumps({"query": query, "offset": offset}).encode()
        ).decode()

    def page(self, items, *, limit=20, cursor=None, query=()):
        positive(limit)
        offset, query = self.cursor_offset(cursor, query)
        end = offset + limit
        next_cursor = self.encode_cursor(end, query) if end < len(items) else None
        return Page(tuple(items[offset:end]), next_cursor, len(items))


class Workspaces(Service):
    def _all(self):
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

        rows = try_enumerate_workspaces(self.session, base_url=self.client.base_url)
        return [
            Resource(x["name"], self.ref(WorkspaceRef, x["name"], x["id"], x["id"])) for x in rows
        ]

    @operation
    def list(self, *, limit: int = 20, cursor: str | None = None) -> Page[Resource[WorkspaceRef]]:
        return self.page(self._all(), limit=limit, cursor=cursor)

    @operation
    def get(self, selector: str | WorkspaceRef) -> Resource[WorkspaceRef]:
        return exact(self._all(), selector, WorkspaceRef, self.client)


class Projects(Service):
    def _all(self, ws):
        from inspire.platform.web.browser_api.projects import list_projects

        return [
            (Resource(x.name, self.ref(ProjectRef, x.name, x.project_id, ws.ref.key)), x)
            for x in list_projects(workspace_id=ws.ref.key, session=self.session)
        ]

    @operation
    def list(
        self, *, workspace: str | WorkspaceRef, limit: int = 20, cursor: str | None = None
    ) -> Page[Resource[ProjectRef]]:
        ws = self.client.workspaces.get(workspace)
        return self.page(
            [x[0] for x in self._all(ws)], limit=limit, cursor=cursor, query=(ws.ref.key,)
        )

    @operation
    def get(
        self, selector: str | ProjectRef, *, workspace: str | WorkspaceRef
    ) -> Resource[ProjectRef]:
        ws = self.client.workspaces.get(workspace)
        return exact([x[0] for x in self._all(ws)], selector, ProjectRef, self.client, ws.ref.key)


class ComputeGroups(Service):
    def _all(self, ws):
        from inspire.platform.web.browser_api.availability import list_compute_groups

        rows = list_compute_groups(workspace_id=ws.ref.key, session=self.session)
        return [
            (
                Resource(
                    str(x.get("name") or x.get("logic_compute_group_name") or ""),
                    self.ref(
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
        self, *, workspace: str | WorkspaceRef, limit: int = 20, cursor: str | None = None
    ) -> Page[Resource[ComputeGroupRef]]:
        ws = self.client.workspaces.get(workspace)
        return self.page(
            [x[0] for x in self._all(ws)], limit=limit, cursor=cursor, query=(ws.ref.key,)
        )

    @operation
    def get(
        self, selector: str | ComputeGroupRef, *, workspace: str | WorkspaceRef
    ) -> Resource[ComputeGroupRef]:
        ws = self.client.workspaces.get(workspace)
        return exact(
            [x[0] for x in self._all(ws)], selector, ComputeGroupRef, self.client, ws.ref.key
        )


class Images(Service):
    SOURCES = ("official", "public", "project", "private")

    def _all(self, ws, source=None):
        from inspire.platform.web.browser_api.images import list_images_by_source

        if source is not None and source not in self.SOURCES:
            raise ValidationError("Unknown image catalog source.")
        images = {}
        for catalog in (source,) if source else self.SOURCES:
            try:
                rows = list_images_by_source(catalog, workspace_id=ws.ref.key, session=self.session)
            except InspireError:
                raise
            except Exception:
                raise ResolutionIncompleteError("An image catalog could not be read.") from None
            for x in rows:
                label = f"{x.name}:{x.version}" if x.version else x.name
                images[x.image_id] = Image(
                    label,
                    self.ref(ImageRef, label, x.image_id, ws.ref.key),
                    catalog,
                    x.url,
                )
        return list(images.values())

    @operation
    def list(
        self,
        *,
        workspace: str | WorkspaceRef,
        source: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Image]:
        ws = self.client.workspaces.get(workspace)
        return self.page(
            self._all(ws, source), limit=limit, cursor=cursor, query=(ws.ref.key, source)
        )

    @operation
    def get(
        self, selector: str | ImageRef | ImageSelector, *, workspace: str | WorkspaceRef
    ) -> Image:
        ws = self.client.workspaces.get(workspace)
        source = selector.source if isinstance(selector, ImageSelector) else None
        name = selector.name if isinstance(selector, ImageSelector) else selector
        return exact(self._all(ws, source), name, ImageRef, self.client, ws.ref.key)
