"""`shell` on serving / Ray / HPC reaches the PTY through the read-only wrapper.

Each `_run_readonly_*_operation` wrapper hands its callback the resolved
handle and the session, so a callback that only accepts the handle dies with a
`TypeError` before any instance is listed. These tests drive the real wrapper
and only fake the platform edges.
"""

from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.commands.hpc import hpc_commands
from inspire.cli.commands.ray import ray_commands
from inspire.cli.commands.serving import serving_commands
from inspire.cli.main import main as cli_main


class _FakeSession:
    workspace_id = "ws-1"


def _capture_shell(
    monkeypatch: pytest.MonkeyPatch, module: Any, captured: dict[str, Any]
) -> None:
    def fake_open_job_shell(*, job_id, instance_name, session, workload):  # noqa: ANN001
        captured["shell"] = {
            "job_id": job_id,
            "instance_name": instance_name,
            "session": session,
            "workload": workload,
        }
        return 0

    monkeypatch.setattr(module, "open_job_shell", fake_open_job_shell)


def test_serving_shell_opens_first_running_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    captured: dict[str, Any] = {}

    def fake_list_serving_instances(serving_id, *, page, page_size, session):  # noqa: ANN001
        captured["list"] = {"serving_id": serving_id, "session": session}
        return (
            [
                {"name": "project/sv-pending-0", "status": "PENDING"},
                {"name": "project/sv-running-0", "status": "RUNNING"},
            ],
            2,
        )

    monkeypatch.setattr(serving_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(serving_commands, "select_workspace_id", lambda **_kwargs: "ws-1")
    monkeypatch.setattr(
        serving_commands, "_resolve_serving_name", lambda *_args, **_kwargs: "serving-1"
    )
    monkeypatch.setattr(
        serving_commands.browser_api_module,
        "list_serving_instances",
        fake_list_serving_instances,
    )
    _capture_shell(monkeypatch, serving_commands, captured)

    result = CliRunner().invoke(
        cli_main, ["serving", "shell", "qwen-chat", "--workspace", "Test Workspace"]
    )

    assert result.exit_code == 0, result.output
    assert captured["list"] == {"serving_id": "serving-1", "session": session}
    assert captured["shell"] == {
        "job_id": "serving-1",
        "instance_name": "project/sv-running-0",
        "session": session,
        "workload": "serving",
    }
    assert "Press Ctrl-]" in result.output


def test_ray_shell_prefers_running_head(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    captured: dict[str, Any] = {}

    def fake_fetch_ray_instances(ray_job_id, *, limit, session, show_all):  # noqa: ANN001
        captured["list"] = {"ray_job_id": ray_job_id, "session": session}
        return (
            [
                {"name": "ray-worker-0", "type": "worker", "status": "RUNNING"},
                {"name": "ray-head-0", "type": "head", "status": "RUNNING"},
            ],
            2,
        )

    monkeypatch.setattr(ray_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        ray_commands, "_resolve_ray_name_in_workspace", lambda *_args, **_kwargs: "ray-1"
    )
    monkeypatch.setattr(ray_commands, "_fetch_ray_instances", fake_fetch_ray_instances)
    _capture_shell(monkeypatch, ray_commands, captured)

    result = CliRunner().invoke(
        cli_main, ["ray", "shell", "av-pipeline", "--workspace", "Test Workspace"]
    )

    assert result.exit_code == 0, result.output
    assert captured["list"] == {"ray_job_id": "ray-1", "session": session}
    assert captured["shell"] == {
        "job_id": "ray-1",
        "instance_name": "ray-head-0",
        "session": session,
        "workload": "ray",
    }
    assert "Press Ctrl-]" in result.output


def test_hpc_shell_opens_first_running_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    captured: dict[str, Any] = {}

    def fake_fetch_hpc_instances(job_id, *, limit, session, show_all):  # noqa: ANN001
        captured["list"] = {"job_id": job_id, "session": session}
        return (
            [
                {"name": "project/hpc-slurmctld-0", "role": "slurmctld", "status": "RUNNING"},
                {"name": "project/hpc-slurmd-0", "role": "slurmd", "status": "RUNNING"},
            ],
            2,
        )

    monkeypatch.setattr(hpc_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        hpc_commands, "_resolve_hpc_name_in_workspace", lambda *_args, **_kwargs: "hpc-1"
    )
    monkeypatch.setattr(hpc_commands, "_fetch_hpc_instances", fake_fetch_hpc_instances)
    _capture_shell(monkeypatch, hpc_commands, captured)

    result = CliRunner().invoke(
        cli_main, ["hpc", "shell", "preprocess", "--workspace", "Test Workspace"]
    )

    assert result.exit_code == 0, result.output
    assert captured["list"] == {"job_id": "hpc-1", "session": session}
    assert captured["shell"] == {
        "job_id": "hpc-1",
        "instance_name": "project/hpc-slurmctld-0",
        "session": session,
        "workload": "hpc",
    }
    assert "Press Ctrl-]" in result.output
