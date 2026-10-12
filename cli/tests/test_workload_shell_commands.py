"""Exercise workload shells through real read-only wrappers and instance fetchers."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.hpc import hpc_commands
from inspire.cli.commands.ray import ray_commands
from inspire.cli.commands.serving import serving_commands
from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main


@pytest.fixture(params=["serving", "ray", "hpc"])
def shell_case(request, monkeypatch):
    workload = request.param
    module, resolver, list_api, rows, default, selector, explicit = {
        "serving": (
            serving_commands,
            "_resolve_serving_name",
            "list_serving_instances",
            [
                {
                    "name": "project/sv-12345678-1234-1234-1234-123456789abc-0",
                    "rank": 0,
                    "status": "PENDING",
                },
                {
                    "name": "project/sv-12345678-1234-1234-1234-123456789abc-1",
                    "rank": 1,
                    "status": "RUNNING",
                },
                {
                    "name": "project/sv-12345678-1234-1234-1234-123456789abc-2",
                    "rank": 2,
                    "status": "RUNNING",
                },
            ],
            "project/sv-12345678-1234-1234-1234-123456789abc-1",
            "rank=2",
            "project/sv-12345678-1234-1234-1234-123456789abc-2",
        ),
        "ray": (
            ray_commands,
            "_resolve_ray_name_in_workspace",
            "list_ray_job_instances",
            [
                {"name": "ray-pending-0", "type": "head", "status": "PENDING"},
                {"name": "ray-worker-0", "type": "worker", "status": "RUNNING"},
                {"name": "ray-head-0", "type": "head", "status": "RUNNING"},
            ],
            "ray-head-0",
            "worker",
            "ray-worker-0",
        ),
        "hpc": (
            hpc_commands,
            "_resolve_hpc_name_in_workspace",
            "list_hpc_job_instances",
            [
                {"name": "project/hpc-pending-0", "role": "launcher", "status": "PENDING"},
                {"name": "project/hpc-slurmctld-0", "role": "slurmctld", "status": "RUNNING"},
                {"name": "project/hpc-launcher-0", "role": "launcher", "status": "RUNNING"},
            ],
            "project/hpc-launcher-0",
            "slurmctld",
            "project/hpc-slurmctld-0",
        ),
    }[workload]
    case = SimpleNamespace(
        workload=workload,
        session=SimpleNamespace(workspace_id="ws-1"),
        rows=rows,
        default=default,
        selector=selector,
        explicit=explicit,
        stale=False,
        resolutions=[],
        listed=[],
        invalidated=[],
        opened=[],
        exit_code=0,
    )

    def resolve(*args, require_live=False, **kwargs):
        case.resolutions.append(require_live)
        return "resource-new" if require_live else "resource-cached"

    def list_instances(resource_id, *, session, **kwargs):
        assert session is case.session
        if workload == "serving":
            assert kwargs["page"] == 1
            limit = kwargs["page_size"]
        else:
            limit = kwargs["limit"]
        case.listed.append((resource_id, limit))
        if case.stale and resource_id == "resource-cached":
            raise RuntimeError("404 resource not found")
        return case.rows[:limit], len(case.rows)

    def open_shell(**kwargs):
        case.opened.append(kwargs)
        return case.exit_code

    monkeypatch.setattr(module, "get_web_session", lambda: case.session)
    monkeypatch.setattr(module, "select_workspace_id", lambda **kwargs: "ws-1")
    monkeypatch.setattr(module, resolver, resolve)
    monkeypatch.setattr(module.browser_api_module, list_api, list_instances)
    monkeypatch.setattr(
        module, "forget_resource_identity", lambda **kwargs: case.invalidated.append(kwargs)
    )
    monkeypatch.setattr(module, "open_job_shell", open_shell)
    return case


def invoke_shell(case, *args):
    return CliRunner().invoke(
        cli_main,
        [case.workload, "shell", "demo", "--workspace", "Test Workspace", *args],
    )


def assert_opened(case, instance, resource_id="resource-cached"):
    assert case.opened == [
        {
            "job_id": resource_id,
            "instance_name": instance,
            "session": case.session,
            "workload": case.workload,
        }
    ]


def test_shell_selects_default_running_instance(shell_case):
    result = invoke_shell(shell_case)

    assert result.exit_code == 0, result.output
    assert_opened(shell_case, shell_case.default)
    assert shell_case.listed == [("resource-cached", 200)]
    assert "Press Ctrl-]" in result.output


def test_shell_honors_explicit_instance_and_exit_code(shell_case):
    shell_case.exit_code = 7
    result = invoke_shell(shell_case, "--instance", shell_case.selector)

    assert result.exit_code == 7, result.output
    assert_opened(shell_case, shell_case.explicit)


def test_shell_retries_stale_handle_before_opening(shell_case):
    shell_case.stale = True
    result = invoke_shell(shell_case)

    assert result.exit_code == 0, result.output
    assert shell_case.resolutions == [False, True]
    assert shell_case.listed == [("resource-cached", 200), ("resource-new", 200)]
    assert len(shell_case.invalidated) == 1
    assert shell_case.invalidated[0]["resource_id"] == "resource-cached"
    assert shell_case.invalidated[0]["session"] is shell_case.session
    assert_opened(shell_case, shell_case.default, "resource-new")


def test_shell_finds_running_instance_beyond_first_page(shell_case):
    pending = shell_case.rows[0]
    shell_case.rows = [pending] * 200 + [shell_case.rows[-1]]
    result = invoke_shell(shell_case)

    assert result.exit_code == 0, result.output
    assert shell_case.listed == [("resource-cached", 200), ("resource-cached", 201)]
    assert_opened(shell_case, shell_case.rows[-1]["name"])


def test_shell_rejects_unknown_instance_without_opening(shell_case):
    result = invoke_shell(shell_case, "--instance", "missing")

    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert "matches" in result.output
    assert shell_case.opened == []


@pytest.mark.parametrize("has_pending", [False, True], ids=["empty", "pending-only"])
def test_shell_rejects_no_running_instances(shell_case, has_pending):
    shell_case.rows = shell_case.rows[:1] if has_pending else []
    result = invoke_shell(shell_case)

    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert "No running instances" in result.output
    assert shell_case.opened == []


def test_shell_preserves_instance_labels_from_unfiltered_listing(shell_case):
    for row in shell_case.rows:
        row.pop("rank", None)
    selector = {"serving": "rank=2", "ray": "head-2", "hpc": "launcher-2"}[shell_case.workload]
    result = invoke_shell(shell_case, "--instance", selector)

    assert result.exit_code == 0, result.output
    assert_opened(shell_case, shell_case.rows[-1]["name"])


def test_shell_does_not_reassign_pending_instance_label(shell_case):
    for row in shell_case.rows:
        row.pop("rank", None)
    selector = {"serving": "rank=0", "ray": "head-0", "hpc": "launcher-0"}[shell_case.workload]
    result = invoke_shell(shell_case, "--instance", selector)

    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert shell_case.opened == []
