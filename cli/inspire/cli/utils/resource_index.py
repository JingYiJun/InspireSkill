"""Compatibility alias; implementation lives in inspire.services.resource_index."""

import sys
from inspire.services import resource_index as _implementation
from inspire.services.resource_index import *  # noqa: F403

# Keep historical monkeypatch targets bound to the implementation globals.
sys.modules[__name__] = _implementation
