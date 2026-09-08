"""Browser-free command execution shared by SDK workloads."""

from __future__ import annotations

import codecs
import re
import select
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Any, Sequence

from inspire.platform.web.pty_socket import (
    WebSocketClient,
    JobShellAuthError,
    build_remote_cmd_headers,
    normalize_job_instances,
    select_job_instance,
)
from inspire.platform.web.session import WebSession
from inspire.platform.web.session.models import SessionExpiredError
from inspire.platform.web.browser_api.jupyter_terminal import (
    build_jupyter_exec_command,
    new_completion_marker,
    parse_jupyter_exec_output,
    run_command_capture_in_notebook,
)
from inspire.bridge.tunnel.config import load_tunnel_config
from inspire.bridge.tunnel.ssh_exec import run_ssh_command, run_ssh_command_streaming
from inspire.bridge import tunnel
from inspire.services.notebook_targets import read_target_cache


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    output: str
    stdout: str
    stderr: str
    completed: bool
    transport: str
    instance: str = ""


def build_remote_command(
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    remote_env_exports: str = "",
) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string.")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be a dictionary of string values.")
    exports = remote_env_exports
    for key, value in (env or {}).items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid environment key: {key!r}")
        if not isinstance(value, str):
            raise ValueError(f"Environment value for {key!r} must be a string.")
        exports += f"export {key}={shlex.quote(value)} && "
    if cwd is not None:
        if not isinstance(cwd, str):
            raise ValueError("cwd must be a string.")
        # Preserve the CLI's double-quoted shape while treating a caller's path literally.
        quoted = re.sub(r'([\\"$`])', r"\\\1", cwd)
        exports += f'cd "{quoted}" && '
    return exports + command


def exec_over_pty_websocket(
    *,
    session: WebSession,
    url: str,
    command: str,
    timeout: float,
    marker: str | None = None,
    on_output: Callable[[str], None] | None = None,
) -> ExecResult:
    marker = marker or new_completion_marker()
    deadline = time.monotonic() + timeout
    prompt_deadline = time.monotonic() + min(3.0, timeout / 4)
    output = ""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    sent = False
    ws = WebSocketClient(
        url, build_remote_cmd_headers(session, base_url=session.base_url), timeout=timeout
    )
    try:
        try:
            ws.connect()
        except TimeoutError:
            return ExecResult(124, "", "", "", False, "pty")
        except JobShellAuthError as error:
            raise SessionExpiredError(str(error)) from error
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if not sent and now >= prompt_deadline:
                ws.send_text(build_jupyter_exec_command(command, marker=marker).rstrip("\r") + "\r")
                sent = True
            ready, _, _ = select.select([ws.fileno()], [], [], min(0.25, deadline - now))
            if not ready and not ws.has_pending_data():
                continue
            ws.set_read_timeout(max(0.001, deadline - time.monotonic()))
            try:
                opcode, payload = ws.recv_frame()
            except (EOFError, TimeoutError):
                break
            if opcode == 0x8:
                break
            if opcode == 0x9:
                ws.send_pong(payload)
                continue
            if opcode not in (0x1, 0x2):
                continue
            chunk = decoder.decode(payload)
            output += chunk
            if on_output is not None and chunk:
                on_output(chunk)
            if not sent and re.search(r"[$#]\s*$", output):
                ws.send_text(build_jupyter_exec_command(command, marker=marker).rstrip("\r") + "\r")
                sent = True
            if re.search(re.escape(marker) + r":exit:\d+\s", output):
                break
    finally:
        ws.close()
    output += decoder.decode(b"", final=True)
    result = parse_jupyter_exec_output(output, marker=marker)
    return ExecResult(result.returncode, result.output, result.output, "", result.completed, "pty")


def exec_in_notebook_jupyter(
    *,
    session: WebSession,
    notebook_id: str,
    command: str,
    timeout: float,
    marker: str | None = None,
    on_output: Callable[[str], None] | None = None,
) -> ExecResult:
    from requests.exceptions import Timeout

    chunks: list[str] = []
    callback_failed = False

    def capture(chunk: str) -> None:
        nonlocal callback_failed
        chunks.append(chunk)
        if on_output is not None:
            try:
                on_output(chunk)
            except BaseException:
                callback_failed = True
                raise

    try:
        result = run_command_capture_in_notebook(
            session=session,
            notebook_id=notebook_id,
            command=command,
            timeout=timeout,
            marker=marker,
            on_output=capture if on_output is not None else None,
        )
    except (Timeout, TimeoutError):
        if callback_failed:
            raise
        output = "".join(chunks)
        return ExecResult(124, output, output, "", False, "jupyter")
    return ExecResult(
        result.returncode, result.output, result.output, "", result.completed, "jupyter"
    )


def exec_in_notebook_ssh(
    *,
    bridge_name: str,
    account: str,
    command: str,
    timeout: float,
    on_output: Callable[[str], None] | None = None,
) -> ExecResult:
    config = load_tunnel_config(account=account)
    out: list[str] = []
    err: list[str] = []

    def capture(chunk: str) -> None:
        out.append(chunk)
        if on_output is not None:
            on_output(chunk)

    def capture_error(chunk: str) -> None:
        err.append(chunk)
        if on_output is not None:
            on_output(chunk)

    try:
        if on_output is None:
            result = run_ssh_command(
                command,
                bridge_name=bridge_name,
                config=config,
                timeout=timeout,
                capture_output=True,
                check=False,
            )
            stdout, stderr = result.stdout or "", result.stderr or ""
            code = result.returncode
        else:
            code = run_ssh_command_streaming(
                command,
                bridge_name=bridge_name,
                config=config,
                timeout=timeout,
                output_callback=capture,
                stderr_callback=capture_error,
            )
            stdout, stderr = "".join(out), "".join(err)
    except subprocess.TimeoutExpired as error:

        def decoded(value: str | bytes | None) -> str:
            return (
                value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            )

        stdout = "".join(out) if on_output is not None else decoded(error.stdout)
        stderr = "".join(err) if on_output is not None else decoded(error.stderr)
        return ExecResult(124, stdout + stderr, stdout, stderr, False, "ssh")
    return ExecResult(code, stdout + stderr, stdout, stderr, True, "ssh")


def cached_notebook_bridge(*, notebook_id: str, workspace_id: str, account: str) -> str | None:
    """Find only an existing bridge for the exact account/workspace/notebook identity."""
    try:
        config = load_tunnel_config(account=account)
        entries = read_target_cache().get("targets", {})
        names = [
            row.get("bridge_name")
            for row in entries.values()
            if isinstance(row, dict)
            and row.get("account") == account
            and row.get("notebook_id") == notebook_id
            and row.get("workspace_id") == workspace_id
        ]
        bridges = list(config.list_bridges())
        bridges.sort(key=lambda bridge: bridge.name not in names)
        for bridge in bridges:
            if bridge.notebook_id != notebook_id or bridge.workspace_id != workspace_id:
                continue
            if tunnel.is_tunnel_available(
                bridge_name=bridge.name,
                config=config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            ):
                return bridge.name
    except (OSError, ValueError, RuntimeError):
        return None
    return None


def select_exec_instance(
    workload: str, rows: Sequence[dict[str, Any]], instance: str | None = None
) -> str:
    """Select a single addressable running instance using the workload's public labels."""
    from inspire.services.job_events import job_instance_views, select_job_instance_views
    from inspire.services.hpc_instances import hpc_instance_views, select_hpc_instance_views
    from inspire.services.ray_instances import ray_instance_views, select_ray_instance_views
    from inspire.services.serving_instances import (
        serving_instance_views,
        select_serving_instance_views,
    )

    if instance is not None and (not isinstance(instance, str) or not instance.strip()):
        raise ValueError("instance must be a non-empty label or instance name.")
    if workload == "job":
        normalized = normalize_job_instances(list(rows))
        if instance is None or any(i.name == instance for i in normalized):
            return select_job_instance(normalized, instance_name=instance, prompt=False).name
        rank_text = instance.removeprefix("rank=")
        if rank_text.isdigit():
            return select_job_instance(normalized, rank=int(rank_text), prompt=False).name
    running = [
        row
        for row in rows
        if "run" in str(row.get("status") or row.get("instance_status") or "").lower()
    ]
    selectors: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {
        "job": (job_instance_views, select_job_instance_views),
        "hpc": (hpc_instance_views, select_hpc_instance_views),
        "ray": (ray_instance_views, select_ray_instance_views),
        "serving": (serving_instance_views, select_serving_instance_views),
    }
    project, choose = selectors[workload]
    # Construct labels before filtering so positional ranks remain stable.
    views = project(rows)
    handles = {str(row.get("name") or row.get("pod_name") or "") for row in running}
    views = [view for view in views if view.handle in handles]
    if not views:
        raise ValueError(f"No running instances found for {workload}.")
    if instance:
        chosen = [view for view in views if view.handle == instance]
        if not chosen:
            chosen = choose(views, [instance])
    elif workload == "serving":
        chosen = views[:1]
    else:
        default = "launcher" if workload == "hpc" else "head"
        chosen = choose(views, [default])
    if len(chosen) != 1:
        candidates = ", ".join(f"{v.label} ({v.handle})" for v in chosen or views)
        raise ValueError(
            f"Multiple running instances match; pass instance. Candidates: {candidates}"
        )
    return chosen[0].handle
