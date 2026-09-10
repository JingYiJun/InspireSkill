"""Compatibility alias; implementation lives in inspire.services.processes."""

import sys
from inspire.services import processes as _implementation
from inspire.services.processes import *  # noqa: F403

# Keep historical monkeypatch targets bound to the implementation globals.
sys.modules[__name__] = _implementation
