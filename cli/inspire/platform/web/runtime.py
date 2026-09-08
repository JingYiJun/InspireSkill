"""Optional per-operation transport binding. Legacy CLI behavior is unchanged."""

from contextvars import ContextVar
from typing import Any

active_transport: ContextVar[Any] = ContextVar("inspire_transport", default=None)
