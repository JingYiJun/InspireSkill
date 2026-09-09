"""Bounded, account-local SDK catalog storage; independent of CLI caches."""

from __future__ import annotations

from dataclasses import fields
import json
import math
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from inspire.accounts.cache_lock import exclusive_cache_lock
from inspire.accounts.storage import account_dir
from inspire.platform.web.browser_api.images import CustomImageInfo
from inspire.platform.web.browser_api.projects import ProjectInfo
from inspire.services.account_config import atomic_write_text

KINDS = frozenset(
    {
        "workspaces",
        "projects",
        "compute_groups",
        "images",
        "prices",
        "priority_levels",
        "fair_scheduling",
        "current_user",
    }
)
MAX_ENTRIES = 256
MAX_BYTES = 8 * 1024 * 1024
_TYPES = {cls.__name__: cls for cls in (ProjectInfo, CustomImageInfo)}


def _encode(value: Any) -> Any:
    # Tagged containers preserve tuple fields and integer dictionary keys.
    # Only these two known directory models can be reconstructed; no pickle
    # or imports selected by disk contents.
    if type(value) in (str, int, float, bool, type(None)):
        return value
    if type(value) in (list, tuple):
        return [type(value).__name__, [_encode(item) for item in value]]
    if type(value) is dict:
        return ["dict", [[_encode(k), _encode(v)] for k, v in value.items()]]
    if type(value) in _TYPES.values():
        return [
            type(value).__name__,
            {f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        ]
    raise TypeError("Unsupported catalog value")


def _decode(value: Any) -> Any:
    if type(value) in (str, int, float, bool, type(None)):
        return value
    tag, data = value
    if tag == "list":
        return [_decode(item) for item in data]
    if tag == "tuple":
        return tuple(_decode(item) for item in data)
    if tag == "dict":
        return {_decode(k): _decode(v) for k, v in data}
    if tag in _TYPES:
        return _TYPES[tag](**{k: _decode(v) for k, v in data.items()})
    raise ValueError("Unknown catalog value")


def _validate_value(kind: str, value: Any) -> None:
    if kind in {"workspaces", "projects", "compute_groups", "images", "prices"}:
        if not isinstance(value, list):
            raise ValueError("Invalid catalog rows")
        for row in value:
            if kind == "projects":
                valid = isinstance(row, ProjectInfo) and bool(row.project_id)
            elif kind == "images":
                valid = isinstance(row, CustomImageInfo) and bool(row.image_id)
            else:
                identity = {
                    "workspaces": ("id",),
                    "compute_groups": ("id", "logic_compute_group_id"),
                    "prices": ("quota_id", "spec_id"),
                }[kind]
                valid = isinstance(row, dict) and any(row.get(field) for field in identity)
            if not valid:
                raise ValueError("Catalog omitted a resource identity")
    elif kind == "fair_scheduling":
        if type(value) is not bool:
            raise ValueError("Invalid scheduling flag")
    elif not isinstance(value, dict) or (
        kind == "current_user" and not (value.get("id") or value.get("user_id"))
    ):
        raise ValueError("Invalid catalog mapping")


def key_string(key: tuple[Any, ...]) -> str:
    return json.dumps(_encode(key), ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class CatalogStore:
    def __init__(self, account: str, base_url: str) -> None:
        self.account = account
        self.base_url = base_url
        self.path: Path = account_dir(account) / "sdk-catalog-v1.json"

    def _read(self) -> dict[str, Any]:
        """Called under the stable sibling lock, including repair and pruning."""
        try:
            if self.path.stat().st_size > MAX_BYTES:
                raise ValueError("Oversized catalog")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                data["version"] != 1
                or data["account"] != self.account
                or not isinstance(data["generation"], str)
                or not isinstance(data["entries"], dict)
            ):
                raise ValueError("Invalid catalog envelope")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            data = {"version": 1, "account": self.account, "generation": uuid4().hex, "entries": {}}
            self._write(data)
        now = time.time()
        changed = False
        for key, entry in list(data["entries"].items()):
            try:
                decoded_key = _decode(json.loads(key))
                if (
                    not isinstance(decoded_key, tuple)
                    or len(decoded_key) < 3
                    or decoded_key[0] not in KINDS
                    or decoded_key[1] != self.account
                    or not isinstance(decoded_key[2], str)
                    or not isinstance(entry["token"], str)
                    or not all(
                        type(entry[k]) in (int, float) and math.isfinite(entry[k])
                        for k in ("created", "expires")
                    )
                    or not entry["created"] <= now < entry["expires"]
                ):
                    raise ValueError("Invalid or expired entry")
                _validate_value(decoded_key[0], _decode(entry["value"]))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                del data["entries"][key]
                changed = True
        if changed:
            self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        entries = data["entries"]
        while len(entries) > MAX_ENTRIES:
            del entries[next(iter(entries))]
        while True:
            content = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            if len(content) <= MAX_BYTES:
                break
            del entries[next(iter(entries))]
        # The fixed temporary file is protected by the same lock and reused
        # after crashes, so temporary files and lock files cannot accumulate.
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(tmp, 0o600)
        atomic_write_text(self.path, content)

    def read(self, key: tuple[Any, ...]) -> tuple[str, dict[str, Any] | None]:
        with exclusive_cache_lock(self.path, timeout=5):
            data = self._read()
            return data["generation"], data["entries"].get(key_string(key))

    def put(
        self, key: tuple[Any, ...], value: Any, ttl: float, generation: str
    ) -> dict[str, Any] | None:
        _validate_value(key[0], value)
        encoded = _encode(value)
        with exclusive_cache_lock(self.path, timeout=5):
            data = self._read()
            # A mutation/clear during enumeration must not resurrect old data.
            if data["generation"] != generation:
                return None
            now = time.time()
            entry = {"created": now, "expires": now + ttl, "token": uuid4().hex, "value": encoded}
            key_text = key_string(key)
            data["entries"].pop(key_text, None)
            data["entries"][key_text] = entry
            self._write(data)
            return data["entries"].get(key_text)

    def invalidate(self, prefix: tuple[Any, ...] | None = None) -> None:
        with exclusive_cache_lock(self.path, timeout=5):
            data = self._read()
            for key_text in list(data["entries"]):
                key = _decode(json.loads(key_text))
                if (prefix is None and key[2] == self.base_url) or (
                    prefix is not None and key[: len(prefix)] == prefix
                ):
                    del data["entries"][key_text]
            data["generation"] = uuid4().hex
            self._write(data)

    @staticmethod
    def value(entry: dict[str, Any]) -> Any:
        return _decode(entry["value"])
