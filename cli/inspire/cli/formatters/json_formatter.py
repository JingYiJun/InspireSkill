"""Compatibility alias; implementation lives in inspire.services.json_formatter."""

import sys
from inspire.services import json_formatter as _implementation
from inspire.services.json_formatter import *  # noqa: F403

# Keep historical monkeypatch targets bound to the implementation globals.
sys.modules[__name__] = _implementation
