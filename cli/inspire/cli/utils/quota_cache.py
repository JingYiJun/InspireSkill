"""Compatibility alias; implementation lives in inspire.services.quota_cache."""

import sys
from inspire.services import quota_cache as _implementation
from inspire.services.quota_cache import *  # noqa: F403
from inspire.services.quota_cache import group_supports_workload as group_supports_workload

# Keep historical monkeypatch targets bound to the implementation globals.
sys.modules[__name__] = _implementation
