"""Synchronous Python entry point; construction never authenticates or connects."""

from __future__ import annotations

import math

from .exceptions import ConfigurationError, ValidationError
from .transport import Transport
from .cache import CatalogCache
from .resources import Workspaces, Projects, ComputeGroups, Images


class InspireClient:
    def __init__(
        self,
        account: str | None = None,
        *,
        allow_browser: bool = False,
        timeout: float = 30,
        operation_timeout: float = 120,
        catalog_ttl: float = 60,
    ):
        from inspire.accounts import current_account, account_exists, validate_name
        from inspire.config import Config

        self.cache = CatalogCache(catalog_ttl)
        self._catalog_context: dict[str, bool] | None = None
        for value in (timeout, operation_timeout):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (math.isfinite(value) and value > 0)
            ):
                raise ValidationError("Timeouts must be finite positive seconds.")
        try:
            selected = validate_name(account if account is not None else current_account() or "")
            if not account_exists(selected):
                raise ValueError()
            config, _ = Config.from_files_and_env(require_credentials=False, account=selected)
        except Exception:
            raise ConfigurationError("Select an initialized Inspire account.") from None
        self._account = selected
        self._base_url = config.base_url.rstrip("/")
        self.operation_timeout = operation_timeout
        self._config = config
        self._transport = Transport(
            selected,
            self.base_url,
            username=config.username,
            allow_browser=allow_browser,
            timeout=timeout,
        )
        self.workspaces, self.projects = Workspaces(self), Projects(self)
        self.compute_groups, self.images = ComputeGroups(self), Images(self)
        from .jobs import Jobs

        self.jobs = Jobs(self)
        from .hpc import HPC

        self.hpc = HPC(self)
        from .ray import Ray

        self.ray = Ray(self)
        from .servings import Servings

        self.servings = Servings(self)
        from .tensorboards import Tensorboards

        self.tensorboards = Tensorboards(self)
        from .notebooks import Notebooks

        self.notebooks = Notebooks(self)
        from .account import AccountInformation

        self.account_info = AccountInformation(self)
        from .account import APIKeys

        self.api_keys = APIKeys(self)
        from .datasets import Datasets

        self.datasets = Datasets(self)
        from .model_registry import Models

        self.models = Models(self)
        from .resource_monitor import Resources

        self.resources = Resources(self)

    @property
    def account(self) -> str:
        return self._account

    @property
    def base_url(self) -> str:
        return self._base_url

    def _validate_ref(self, ref, cls, workspace_id=None):
        if type(ref) is not cls or ref.account != self.account or ref.base_url != self.base_url:
            raise ValidationError("Reference belongs to another resource type, account or origin.")
        if (
            not isinstance(ref.key, str)
            or not ref.key
            or (workspace_id is not None and ref.workspace_id and ref.workspace_id != workspace_id)
        ):
            raise ValidationError("Reference does not match the requested workspace.")

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> InspireClient:
        self._transport.check()
        return self

    def __exit__(self, *args):
        self.close()
