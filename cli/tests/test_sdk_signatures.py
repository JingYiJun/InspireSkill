"""Discover the client facades and enforce their public calling convention."""

from __future__ import annotations

from collections.abc import Sequence
import inspect
from typing import get_args, get_origin, get_type_hints

from inspire.platform.web.browser_api import CustomImageInfo

from test_sdk import client as client


def facade_methods(client):
    """Inspect instance attributes so newly attached facades cannot bypass the guard."""
    for facade, value in vars(client).items():
        if facade.startswith("_") or isinstance(value, (str, int, float, bool, type(None))):
            continue
        methods = inspect.getmembers(value, inspect.ismethod)
        assert methods, f"Unrecognized client attribute: {facade}"
        for method, bound in methods:
            if not method.startswith("_"):
                yield facade, method, bound


def test_all_facade_signatures(client):
    collections = {"list", "iter", "quotas"}
    no_selector = {"current", "check", "context", "owners"}
    workspace_subjects = {
        "resources": {"availability", "policy", "usage"},
        "account_info": {"permissions"},
        "servings": {"configs"},
    }
    single = {"get", "detail", "stop", "start", "delete", "instances", "instance_names",
              "command", "versions", "scale_history", "api", "url", "scalars", "scaling",
              "events", "logs", "metrics", "wait", "scale", "rollback", "save_image",
              "follow_events", "follow_logs", "realtime_metrics", "lifecycle",
              "estimate_image_size", "cancel_save_image", "wait_image_ready", "wait_ready",
              "set_visibility", "deploy_config", "plaintext"}
    discovered = set()
    for facade, method, bound in facade_methods(client):
        discovered.add(facade)
        params = list(inspect.signature(bound).parameters.values())
        label = f"{facade}.{method}"
        first = 0 if method in no_selector or not params else 1
        if method == "list" and facade in {"workspaces", "api_keys"}:
            first = 0
        if method == "tags" and facade == "datasets":
            first = 0
        if first:
            assert params[0].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, label
        for param in params[first:]:
            assert param.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.VAR_KEYWORD), label
        if method in single or (method == "tags" and facade == "tensorboards"):
            assert params[0].name == "ref", label
        workspace_subject = method in workspace_subjects.get(facade, set())
        if workspace_subject:
            assert params[0].name == "workspace", label
            if method == "permissions":
                assert params[0].default is None, label
            else:
                assert params[0].default is inspect.Parameter.empty, label
        if method in collections or workspace_subject:
            for param in params:
                if param.name == "workspace":
                    assert param is params[0] and param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, label
        else:
            for param in params:
                if param.name == "workspace":
                    assert param.kind == inspect.Parameter.KEYWORD_ONLY, label
        if method in collections:
            pagination = {p.name: p.default for p in params}
            if method == "iter":
                assert pagination["max_items"] is None, label
            else:
                assert pagination["limit"] == 20 and pagination["cursor"] is None, label
        if method in {"wait_image_ready", "wait_ready"}:
            assert get_type_hints(bound)["return"] is CustomImageInfo, label
        if method == "wait":
            defaults = {p.name: p.default for p in params}
            assert defaults["raise_on_failure"] is False, label
            assert defaults["workspace"] is None, label
        if method == "status":
            assert params[0].name == "refs", label
            hints = get_type_hints(bound)
            assert get_origin(hints["refs"]) is Sequence, label
            assert str in get_args(get_args(hints["refs"])[0]), label
            assert get_origin(hints["return"]) is tuple, label
            assert get_args(hints["return"])[1] is Ellipsis, label
        if method in {"plan", "create"} and facade != "api_keys":
            assert params[0].name == "spec", label
        if method == "register" or (method == "create" and facade == "api_keys"):
            assert params[0].name == "name", label
    assert len(discovered) >= 15
