"""Credential serialization and private file publication across platforms."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


def render_key(value: str, format: str, env_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("Use a valid environment variable name.")
    if format == "raw":
        return value + "\n"
    if format == "dotenv":
        # Dotenv parsers disagree on quote escaping and ${...} expansion.
        # Plain safe tokens round-trip in shell, python-dotenv and Docker.
        if not re.fullmatch(r"[A-Za-z0-9_./+=:@%-]+", value):
            raise ValueError(
                "This key needs parser-specific dotenv escaping; use raw or a shell format."
            )
        return f"{env_name}={value}\n"
    if format == "sh":
        return f"export {env_name}={shlex.quote(value)}\n"
    if format == "powershell":
        return f"$env:{env_name} = '" + value.replace("'", "''") + "'\n"
    raise ValueError("Unknown key export format.")


# Only the empty temporary file's path enters PowerShell. No credential is
# passed in argv, the script, environment variables, stdout or stderr.
_WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
# Python may inherit PS7's module path when launching Windows PowerShell.
# Load the security cmdlets from this engine's own installation explicitly.
Import-Module "$PSHOME\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
$path = $env:INSPIRE_KEY_EXPORT_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl
$actual = Get-Acl -LiteralPath $path
$rules = @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
if (!$actual.AreAccessRulesProtected -or $rules.Count -ne 1 -or
    $rules[0].IdentityReference.Value -ne $sid.Value -or
    $rules[0].AccessControlType -ne 'Allow' -or
    $rules[0].FileSystemRights -ne 'FullControl') {
    throw 'Private file ACL verification failed'
}
"""


def windows_acl_tool() -> str:
    tool = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not tool:
        raise ValueError(
            "Private file export requires PowerShell on Windows. Alternatively use --stdout or api-key run."
        )
    return tool


def restrict_windows_file(path: str) -> None:
    env = os.environ.copy()
    env["INSPIRE_KEY_EXPORT_PATH"] = os.path.abspath(path)
    try:
        result = subprocess.run(
            [
                windows_acl_tool(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_ACL_SCRIPT,
            ],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Could not establish private Windows file permissions.") from None
    if result.returncode:
        raise ValueError("Could not verify private Windows file permissions.")


def export_private_key(content: str, output: Path) -> None:
    """Publish a complete private file atomically, never replacing a path."""
    fd, temporary = tempfile.mkstemp(prefix=".inspire-key-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            if sys.platform == "win32":
                restrict_windows_file(temporary)
            elif os.fstat(stream.fileno()).st_mode & 0o777 != 0o600:
                raise ValueError(
                    "The destination filesystem did not enforce private 0600 permissions."
                )
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        os.unlink(temporary)
