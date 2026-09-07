"""Explicit secret destinations, portable serialization and process scope."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

from click.testing import CliRunner
import pytest

from inspire.cli.main import main
from inspire.platform.web.browser_api import api_keys
from inspire.cli.commands.account import key_export

key_cli = importlib.import_module("inspire.cli.commands.account.api_key")
SECRET = "fixture-secret-only"


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(key_cli, "get_web_session", lambda: object())
    monkeypatch.setattr(
        api_keys, "list_api_keys", lambda **kw: [api_keys.APIKeyInfo("internal", "demo", "0")]
    )
    get = Mock(return_value=SECRET)
    monkeypatch.setattr(api_keys, "get_api_key_plaintext", get)
    return get


@pytest.mark.parametrize(
    "fmt,expected",
    [
        ("raw", SECRET + "\n"),
        ("dotenv", "INF_API_KEY=" + SECRET + "\n"),
        ("sh", "export INF_API_KEY=" + SECRET + "\n"),
        ("powershell", "$env:INF_API_KEY = '" + SECRET + "'\n"),
    ],
)
def test_stdout_is_explicit_and_exact(key, fmt, expected):
    result = CliRunner().invoke(
        main, ["account", "api-key", "export", "demo", "--stdout", "--format", fmt]
    )
    assert result.exit_code == 0
    assert result.stdout == expected and result.stderr == ""


@pytest.mark.parametrize(
    "args", [[], ["--stdout", "--output", "unused"], ["--stdout", "--env-name", "bad;name"]]
)
def test_invalid_destination_or_name_does_not_fetch(key, args):
    result = CliRunner().invoke(main, ["account", "api-key", "export", "demo", *args])
    assert result.exit_code != 0
    key.assert_not_called()


def test_json_never_implicitly_reveals_secret(key):
    result = CliRunner().invoke(
        main, ["--json", "account", "api-key", "export", "demo", "--stdout"]
    )
    assert result.exit_code != 0 and SECRET not in result.output
    assert json.loads(result.output)["success"] is False
    key.assert_not_called()


def test_dotenv_file_is_private_and_does_not_merge(key, tmp_path):
    target = tmp_path / ".env"
    args = [
        "--json",
        "account",
        "api-key",
        "export",
        "demo",
        "--format",
        "dotenv",
        "--env-name",
        "APP_API_KEY",
        "--output",
        str(target),
    ]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == b"APP_API_KEY=fixture-secret-only\n"
    assert SECRET not in result.output
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600
    second = CliRunner().invoke(main, args)
    assert second.exit_code != 0
    key.assert_called_once()


@pytest.mark.parametrize(
    "value", ["${EXPAND}", "has'quote", 'a"b', "a#comment", "a\\escape", "~", "prefix:~"]
)
def test_dotenv_rejects_ambiguous_parser_escaping(value):
    with pytest.raises(ValueError):
        key_export.render_key(value, "dotenv", "INF_API_KEY")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_sh_export_round_trips_without_command_execution(tmp_path):
    value = "key'$(touch should-not-exist);`echo nope`"
    script = key_export.render_key(value, "sh", "INF_API_KEY")
    result = subprocess.run(
        ["sh", "-c", script + 'test "$INF_API_KEY" = "$EXPECTED"'],
        env={**os.environ, "EXPECTED": value},
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows PowerShell")
def test_powershell_export_round_trips_literals(tmp_path):
    value = "key'$(New-Item should-not-exist);`echo nope`"
    script = key_export.render_key(value, "powershell", "INF_API_KEY")
    result = subprocess.run(
        [
            key_export.windows_acl_tool(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script + "if ($env:INF_API_KEY -cne $env:EXPECTED) { exit 1 }",
        ],
        env={**os.environ, "EXPECTED": value},
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == 0
    assert not list(tmp_path.iterdir())


def test_run_injects_only_into_child_and_propagates_exit(key, monkeypatch):
    monkeypatch.setenv("INF_API_KEY", "parent-value")
    result = CliRunner().invoke(
        main,
        [
            "account",
            "api-key",
            "run",
            "demo",
            "--",
            sys.executable,
            "-c",
            "import os,sys; sys.exit(7 if os.environ.get('INF_API_KEY') == 'fixture-secret-only' else 9)",
        ],
    )
    assert result.exit_code == 7
    assert SECRET not in result.output
    assert os.environ["INF_API_KEY"] == "parent-value"


def test_run_rejects_json_before_fetch(key):
    result = CliRunner().invoke(
        main, ["--json", "account", "api-key", "run", "demo", "--", "client"]
    )
    assert result.exit_code != 0
    key.assert_not_called()


def test_windows_acl_is_applied_to_empty_file_before_write(monkeypatch, tmp_path):
    monkeypatch.setattr(key_export.sys, "platform", "win32")
    observed = []

    def protect(path):
        observed.append(Path(path).read_bytes())

    monkeypatch.setattr(key_export, "restrict_windows_file", protect)
    target = tmp_path / "key"
    key_export.export_private_key(SECRET, target)
    assert observed == [b""]
    assert target.read_text() == SECRET


def test_windows_acl_failure_leaves_no_file_or_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(key_export.sys, "platform", "win32")

    def fail(path):
        assert Path(path).read_bytes() == b""
        raise ValueError("ACL unavailable")

    monkeypatch.setattr(key_export, "restrict_windows_file", fail)
    with pytest.raises(ValueError):
        key_export.export_private_key(SECRET, tmp_path / ".env")
    assert not list(tmp_path.iterdir())


def test_acl_subprocess_receives_path_but_no_key(monkeypatch, tmp_path):
    monkeypatch.setattr(key_export, "windows_acl_tool", lambda: "powershell.exe")
    call = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(key_export.subprocess, "run", call)
    key_export.restrict_windows_file(str(tmp_path / "empty-file"))
    assert SECRET not in repr(call.call_args)
    assert call.call_args.kwargs["env"]["INSPIRE_KEY_EXPORT_PATH"] == str(tmp_path / "empty-file")


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL diagnostics")
@pytest.mark.parametrize("keep_open", [False, True])
def test_native_windows_acl_on_empty_file(tmp_path, keep_open):
    # This isolated subprocess only sees an empty fixture. Its diagnostic is
    # safe to expose on failure; production export still suppresses stderr.
    target = tmp_path / "empty"
    target.write_bytes(b"")
    stream = target.open("r+") if keep_open else None
    try:
        result = subprocess.run(
            [
                key_export.windows_acl_tool(),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                key_export._WINDOWS_ACL_SCRIPT,
            ],
            env={**os.environ, "INSPIRE_KEY_EXPORT_PATH": str(target)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert target.read_bytes() == b""
    finally:
        if stream is not None:
            stream.close()
