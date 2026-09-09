"""Resumable HTTP workflows shared by the blocking and native async drivers.

A workflow yields calls, receiving either their result or their original
exception. Branches, exception boundaries and credential guards run once in
this description; interpreters supply the I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Generator, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
Program = Generator["Call", Any, T]


@dataclass
class Call:
    function: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    http: bool = False


def call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Call:
    return Call(function, args, kwargs)


def http_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Call:
    return Call(function, args, kwargs, http=True)


def workflow(function: Callable[P, Program[T]]) -> Callable[P, T]:
    @wraps(function)
    def execute(*args: P.args, **kwargs: P.kwargs) -> T:
        program = function(*args, **kwargs)
        try:
            action = next(program)
            while True:
                try:
                    result = perform_sync(action)
                except Exception as error:
                    action = program.throw(error)
                else:
                    action = program.send(result)
        except StopIteration as done:
            return done.value
        finally:
            program.close()

    execute.__workflow__ = function  # type: ignore[attr-defined]
    return execute


def program_for(action: Call) -> Program[Any] | None:
    function = action.function
    program = getattr(function, "__workflow__", None)
    if program is None:
        return None
    owner = getattr(function, "__self__", None)
    args = (owner, *action.args) if owner is not None else action.args
    return program(*args, **action.kwargs)


def enter_context(context: Any) -> Any:
    return context.__enter__()


def exit_context(context: Any, *error: Any) -> Any:
    return context.__exit__(*error)


def perform_sync(action: Call) -> Any:
    if not action.http:
        return action.function(*action.args, **action.kwargs)
    from inspire.platform.web.runtime import active_transport

    owner = active_transport.get()
    if owner is not None:
        owner.check_deadline()
    try:
        return action.function(*action.args, **action.kwargs)
    finally:
        if owner is not None:
            owner.check_deadline()
