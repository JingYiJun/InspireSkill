"""Per-client catalog snapshots. Exceptions and incomplete catalogs are never stored."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
import math
import time
from typing import Any, TypeVar, cast

from .exceptions import ValidationError

T = TypeVar("T")
CatalogKey = tuple[Any, ...]


class CatalogCache:
    def __init__(self, ttl: float = 60) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or ttl < 0
        ):
            raise ValidationError("catalog_ttl must be finite non-negative seconds.")
        self.ttl = float(ttl)
        self._entries: dict[CatalogKey, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0

    def _get(self, key: CatalogKey, load: Callable[[], T]) -> T:
        entry = self._entries.get(key)
        if self.ttl > 0 and entry is not None and time.monotonic() < entry[0]:
            self._hits += 1
            return cast(T, deepcopy(entry[1]))
        self._entries.pop(key, None)
        self._misses += 1
        value = load()
        if self.ttl > 0:
            self._entries[key] = (time.monotonic() + self.ttl, deepcopy(value))
        return value

    def clear(self) -> None:
        """Discard all snapshots; lifetime hit/miss counters are preserved."""
        self._entries.clear()

    def _invalidate(self, kind: str, account: str, base_url: str, *scope: Any) -> None:
        """Discard a catalog kind, optionally restricted by a scope prefix."""
        prefix = (kind, account, base_url, *scope)
        for key in list(self._entries):
            if key[: len(prefix)] == prefix:
                del self._entries[key]

    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        for key, (expiry, _) in list(self._entries.items()):
            if self.ttl == 0 or expiry <= now:
                del self._entries[key]
        return {"hits": self._hits, "misses": self._misses, "entries": len(self._entries)}
