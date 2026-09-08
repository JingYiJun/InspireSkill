"""Compatibility alias for shared account TOML serialization."""
from inspire.services.account_config import toml_dumps as _toml_dumps

__all__ = ["_toml_dumps"]
