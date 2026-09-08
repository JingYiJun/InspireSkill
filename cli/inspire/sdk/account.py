"""Account metadata and explicitly requested API key secrets."""

from __future__ import annotations

from uuid import uuid4

from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api
from inspire.platform.web.browser_api import api_keys
from inspire.platform.web.session import SessionExpiredError
from inspire.services import account_check, account_context
from .exceptions import AuthenticationError
from .models import Page, WorkspaceRef
from .models_resources import (
    AccountInfo,
    AccountCheck,
    AccountContext,
    Permission,
    APIKeyInfo,
    APIKeyRef,
)
from .resources import Service, operation, exact


class AccountInformation(Service):
    @operation
    def current(self) -> AccountInfo:
        user = browser_api.get_current_user(session=self.session)
        return AccountInfo(
            self.client.account,
            self.client._config.username,
            self.client.base_url,
            str(user.get("id") or user.get("user_id") or ""),
            str(user.get("name") or user.get("user_name") or ""),
        )

    @operation
    def check(self) -> AccountCheck:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False, account=self.client.account
        )
        issues = []
        placeholder = account_check.find_placeholder_host_issues(cfg, sources)
        if placeholder:
            issues.append(account_check.format_placeholder_issue_message(placeholder))
        try:
            account_check.validate_required_credentials(cfg)
        except ConfigError as error:
            issues.append(str(error))
        if issues:
            return AccountCheck(False, None, None, tuple(issues))
        try:
            session = self.session
            user = browser_api.get_current_user(session=session, refresh=True)
        except (SessionExpiredError, ValueError, AuthenticationError) as error:
            return AccountCheck(False, None, None, (str(error),))
        # Return identity fields only: authentication material is never metadata.
        identity = {
            k: user[k] for k in ("id", "user_id", "name", "user_name", "username") if k in user
        }
        return AccountCheck(True, identity, session.created_at, ())

    @operation
    def context(self, limit: int | None = None) -> AccountContext:
        data = account_context.collect_context(
            self.client._config, session=self.session, account=self.client.account
        )
        return AccountContext.from_view(account_context.bound_context(data, limit))

    @operation
    def permissions(self, workspace: str | WorkspaceRef | None = None) -> tuple[Permission, ...]:
        all_workspaces = workspace is None or (
            isinstance(workspace, str) and workspace.strip().casefold() == "all"
        )
        workspaces = (
            self.client.workspaces._all()
            if all_workspaces
            else [self.client.workspaces.get(workspace)]
        )
        return tuple(
            Permission(ws.name, permission)
            for ws in workspaces
            for permission in sorted(
                set(browser_api.get_user_permissions(workspace_id=ws.ref.key, session=self.session))
            )
        )


class APIKeys(Service):
    def _all(self):
        return [
            APIKeyInfo.from_view(
                {"name": key.name, "created_at": key.created_at},
                ref=self.ref(APIKeyRef, key.name, key.key_id),
            )
            for key in api_keys.list_api_keys(session=self.session)
        ]

    @operation
    def list(self, *, limit: int = 20, cursor: str | None = None) -> Page[APIKeyInfo]:
        return self.page(self._all(), limit=limit, cursor=cursor)

    @operation
    def get(self, name_or_ref: str | APIKeyRef) -> APIKeyInfo:
        return exact(self._all(), name_or_ref, APIKeyRef, self.client)

    @operation
    def create(self, name: str) -> APIKeyInfo:
        session = self.session
        with self.client._transport.single_send(uuid4().hex, create=True):
            api_keys.create_api_key(name, session=session)
        # GenerateAPIKey has no metadata result. Never invent an identity or re-list.
        return APIKeyInfo.from_view({"name": name})

    @operation
    def delete(self, name_or_ref: str | APIKeyRef) -> APIKeyInfo:
        if isinstance(name_or_ref, APIKeyRef):
            self.client._validate_ref(name_or_ref, APIKeyRef)
            key = APIKeyInfo.from_view({"name": name_or_ref.name}, ref=name_or_ref)
        else:
            key = self.get(name_or_ref)
        assert key.ref is not None
        session = self.session
        with self.client._transport.single_send():
            api_keys.delete_api_key(key.ref.key, session=session)
        return key

    @operation
    def plaintext(self, name_or_ref: str | APIKeyRef) -> str:
        if isinstance(name_or_ref, APIKeyRef):
            self.client._validate_ref(name_or_ref, APIKeyRef)
            ref = name_or_ref
        else:
            key = self.get(name_or_ref)
            assert key.ref is not None
            ref = key.ref
        return api_keys.get_api_key_plaintext(ref.key, session=self.session)
