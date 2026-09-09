"""Generate explicit signatures: uv run python scripts/generate_sdk_async.py."""
from __future__ import annotations

import ast
import builtins
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_origin
from unittest.mock import patch

from inspire.sdk.client import InspireClient


def generate() -> str:
    # Local bindings only: no account files or platform access.
    with patch("inspire.accounts.account_exists", return_value=True), patch(
        "inspire.config.Config.from_files_and_env",
        return_value=(SimpleNamespace(base_url="https://example.invalid", username="fake"), None),
    ):
        client = InspireClient("generator")
    facades = {name: type(value) for name, value in vars(client).items()
               if not name.startswith("_") and not isinstance(value, (str, int, float, bool))}
    client.close()
    modules: dict[str, str] = {}

    def module(name: str) -> str:
        if name not in modules:
            modules[name] = f"_m{len(modules)}"
        return modules[name]

    def wrapper(cls, facade: str, name: str, *, output: bool = False) -> str:
        fn = inspect.unwrap(getattr(cls, name))
        signature = inspect.signature(fn)
        substitutions = {}
        for base in getattr(cls, "__orig_bases__", ()):
            origin = get_origin(base)
            for variable, concrete in zip(getattr(origin, "__parameters__", ()), get_args(base)):
                substitutions[variable.__name__] = (
                    f"{module(concrete.__module__)}.{concrete.__name__}"
                )

        class Qualify(ast.NodeTransformer):
            def visit_Name(self, node):
                if node.id == "Literal":
                    return ast.Name(id="Literal", ctx=ast.Load())
                if node.id == "Iterator":
                    return ast.Name(id="AsyncIterator", ctx=ast.Load())
                if node.id in substitutions:
                    return ast.parse(substitutions[node.id], mode="eval").body
                if node.id in vars(builtins):
                    return node
                return ast.Attribute(value=ast.Name(id=module(fn.__module__), ctx=ast.Load()),
                                     attr=node.id, ctx=ast.Load())

        def annotation(value) -> str:
            assert isinstance(value, str), (name, value)
            return ast.unparse(Qualify().visit(ast.parse(value, mode="eval").body))

        params = []
        calls = []
        keyword = False
        for param in signature.parameters.values():
            if param.name == "self":
                params.append("self")
                continue
            if param.kind == param.KEYWORD_ONLY and not keyword:
                params.append("*")
                keyword = True
            prefix = "**" if param.kind == param.VAR_KEYWORD else ""
            item = prefix + param.name + ": " + annotation(param.annotation)
            if param.default is not param.empty:
                item += " = " + repr(param.default)
            params.append(item)
            calls.append(prefix + param.name if prefix else f"{param.name}={param.name}")
        returns = "AsyncIterator[str]" if output else annotation(signature.return_annotation)
        is_iterator = returns.startswith("AsyncIterator[")
        public_name = "exec_stream" if output else name
        lines = [f"    async def {public_name}(\n" +
                 "".join(f"        {param},\n" for param in params) +
                 f"    ) -> {returns}:\n"]
        invocation = f"self._client.{'_stream' if is_iterator else '_call'}({facade!r}, {name!r}"
        if output:
            invocation += ", output=True"
        invocation += "".join(f",\n            {arg}" for arg in calls) + ")"
        if is_iterator:
            lines.append(f"        async with aclosing({invocation}) as stream:\n")
            lines.append("            async for item in stream:\n                yield item\n")
        else:
            lines.append(f"        return await {invocation}\n")
        return "".join(lines)

    body = []
    for facade, cls in facades.items():
        body.append(f"class Async{cls.__name__}(AsyncFacade):\n")
        for name, _ in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_"):
                continue
            body.append(wrapper(cls, facade, name))
            if name == "exec":
                body.append(wrapper(cls, facade, name, output=True))
        body.append("\n")
    body.append("class InspireAsyncClient(AsyncRuntime):\n")
    body.append('    """Async SDK with native I/O on the caller event loop."""\n')
    body.append("    accounts = Accounts\n\n")
    body.append("    def __init__(\n        self,\n        account: str | None = None,\n        *,\n")
    sig = inspect.signature(InspireClient)
    options = ["account"]
    for param in list(sig.parameters.values())[1:]:
        body.append(f"        {param.name}: {param.annotation} = {param.default!r},\n")
        options.append(param.name)
    body.append("        concurrency: int | None = None,\n    ) -> None:\n")
    body.append("        super().__init__({\n" + "".join(
        f"            {name!r}: {name},\n" for name in options) + "        }, concurrency)\n")
    for facade, cls in facades.items():
        body.append(f"        self.{facade} = Async{cls.__name__}(self)\n")
    body.append("""
    @classmethod
    def from_credentials(
        cls, username: str, password: str, *, base_url: str | None = None,
        proxy: str | None = None, account: str | None = None, **client_kwargs: Any,
    ) -> InspireAsyncClient:
        return cls(account, username=username, password=password, base_url=base_url,
                   proxy=proxy, **client_kwargs)

    @property
    def account(self) -> str:
        if self._account is None:
            raise RuntimeError("Enter the async context or await a call before reading account.")
        return self._account

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            raise RuntimeError("Enter the async context or await a call before reading base_url.")
        return self._base_url

    async def login(self, *, force: bool = False) -> AccountInfo:
        return await self._call("", "login", force=force)

    async def init(self, *, force: bool = False) -> InitResult:
        return await self._call("", "init", force=force)

    async def __aenter__(self) -> InspireAsyncClient:
        await self._start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
""")
    return (
        '"""Generated by scripts/generate_sdk_async.py; do not edit wrappers by hand."""\n'
        'from __future__ import annotations\n\n'
        'from contextlib import aclosing\n'
        'from typing import Any, AsyncIterator, Literal\n'
        'from .accounts import Accounts, InitResult\n'
        'from .models_resources import AccountInfo\n'
        'from ._async_runtime import AsyncFacade, AsyncRuntime\n'
        + "".join(f"import {name} as {alias}\n" for name, alias in modules.items()
                  if alias + "." in "".join(body))
        + "\n\n" + "\n".join(body)
    )


if __name__ == "__main__":
    Path("inspire/sdk/async_client.py").write_text(generate())
