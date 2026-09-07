"""Account API key lifecycle with explicit, file-only secret export."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import os
from pathlib import Path
import re
import tempfile
import sys

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.errors import exit_with_error, require_confirmation
from inspire.config import ConfigError
from inspire.platform.web.browser_api import api_keys
from inspire.platform.web.session import SessionExpiredError, get_web_session


@contextmanager
def _errors(ctx: Context) -> Iterator[None]:
    try:
        yield
    except ConfigError:
        exit_with_error(
            ctx, "ConfigError", "Check the selected account configuration.", EXIT_CONFIG_ERROR
        )
    except SessionExpiredError:
        exit_with_error(
            ctx,
            "AuthenticationError",
            "API key operation requires a valid account session.",
            EXIT_AUTH_ERROR,
        )
    except OSError:
        exit_with_error(
            ctx,
            "ExportError",
            "Could not export the key. Check the destination directory and permissions; existing files are never overwritten.",
            EXIT_API_ERROR,
        )
    except ValueError as e:
        exit_with_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _name(_ctx: click.Context, _param: click.Parameter, value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value):
        raise click.BadParameter("Use 1-256 letters, digits, underscores, hyphens or dots.")
    return value


def _emit(ctx: Context, result: dict) -> None:
    if ctx.json_output:
        click.echo(json_formatter.format_json(result))
    else:
        for k, v in result.items():
            click.echo(f"{k.replace('_', ' ').title()}: {json_formatter.sanitize_text(v)}")


def _resolve(name: str, pick: int | None, session) -> api_keys.APIKeyInfo:
    matches = [key for key in api_keys.list_api_keys(session=session) if key.name == name]
    if not matches:
        raise ValueError("API key name not found. See 'inspire account api-key list'.")
    if pick is not None:
        if pick > len(matches):
            raise ValueError("--pick is outside the matching API key list.")
        return matches[pick - 1]
    if len(matches) > 1:
        candidates = "; ".join(f"{i}: created {key.created_at}" for i, key in enumerate(matches, 1))
        raise ValueError(f"API key name is ambiguous; select --pick N. {candidates}")
    return matches[0]


@click.group("api-key")
def api_key() -> None:
    """Manage platform inference API keys for the selected account.

    Keys are separate from serving creation. List never displays key values.
    Export explicitly to a new private file, then load it into INF_API_KEY
    in your client. Use serving api for endpoint and request examples.
    """


@api_key.command("list")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum keys to show (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show all key names.")
@pass_context
def list_keys(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List key names and creation times, never secret values."""
    try:
        effective = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        exit_with_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return
    with _errors(ctx):
        page = bound_collection(api_keys.list_api_keys(), limit=effective)
        rows = [{"name": k.name, "created_at": k.created_at} for k in page.items]
        if ctx.json_output:
            click.echo(json_formatter.format_json({"items": rows, **page.metadata()}))
        elif not rows:
            click.echo("No API keys found.")
        else:
            table_rows = [
                (
                    json_formatter.sanitize_text(k["name"]),
                    json_formatter.sanitize_text(k["created_at"]),
                )
                for k in rows
            ]
            headers = ("Name", "Created (epoch ms)")
            click.echo(
                "\n".join(
                    render_table(
                        headers,
                        table_rows,
                        [
                            column_width(h, [r[i] for r in table_rows], max_width=60)
                            for i, h in enumerate(headers)
                        ],
                    )
                )
            )
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)


@api_key.command("create")
@click.option("--name", required=True, callback=_name, help="New API key name.")
@pass_context
def create_key(ctx: Context, name: str) -> None:
    """Create a named key; retrieve its secret separately with export."""
    with _errors(ctx):
        session = get_web_session()
        if any(k.name == name for k in api_keys.list_api_keys(session=session)):
            raise ValueError("An API key with this name already exists. Choose a new name.")
        api_keys.create_api_key(name, session=session)
        result = {"name": name, "status": "created"}
        try:
            if not any(k.name == name for k in api_keys.list_api_keys(session=session)):
                result["status"] = "creation accepted; confirmation pending"
        except (ValueError, SessionExpiredError):
            result["status"] = "creation accepted; confirmation pending"
        _emit(ctx, result)


def export_private_key(value: str, output: Path) -> None:
    """Publish a complete 0600 file atomically; never replace files/symlinks."""
    if sys.platform == "win32":
        raise ValueError(
            "Private API key export requires POSIX file permissions; use WSL on Windows."
        )
    fd, temporary = tempfile.mkstemp(prefix=".inspire-key-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.fstat(stream.fileno()).st_mode & 0o777 != 0o600:
                raise ValueError(
                    "The destination filesystem did not enforce private 0600 permissions."
                )
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        os.unlink(temporary)


@api_key.command("export")
@click.argument("name", callback=_name)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="New file for the plaintext key (0600; never overwritten).",
)
@pass_context
def export_key(ctx: Context, name: str, pick: int | None, output: Path) -> None:
    """Write plaintext to a new private file; stdout/JSON contain no secret.

    Load the exported file in your client: export INF_API_KEY="$(cat PATH)".
    The file contains only the key and a trailing newline, not a shell script.
    Requires POSIX file permissions; use WSL on Windows.
    """
    with _errors(ctx):
        if sys.platform == "win32":
            raise ValueError(
                "Private API key export requires POSIX file permissions; use WSL on Windows."
            )
        if os.path.lexists(output):
            raise ValueError("Export destination already exists; choose a new file.")
        session = get_web_session()
        key = _resolve(name, pick, session)
        value = api_keys.get_api_key_plaintext(key.key_id, session=session)
        export_private_key(value, output)
        _emit(ctx, {"name": name, "status": "exported", "permissions": "0600"})


@api_key.command("delete")
@click.argument("name", callback=_name)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option("--yes", "-y", is_flag=True, help="Confirm permanent deletion without prompting.")
@pass_context
def delete_key(ctx: Context, name: str, pick: int | None, yes: bool) -> None:
    """Permanently revoke a key. Clients using it will lose access."""
    require_confirmation(
        ctx,
        yes=yes,
        prompt=f"Permanently delete API key '{name}'?",
        message="API key deletion requires confirmation.",
    )
    with _errors(ctx):
        session = get_web_session()
        key = _resolve(name, pick, session)
        api_keys.delete_api_key(key.key_id, session=session)
        result = {"name": name, "status": "deleted"}
        try:
            if any(k.key_id == key.key_id for k in api_keys.list_api_keys(session=session)):
                result["status"] = "deletion accepted; confirmation pending"
        except (ValueError, SessionExpiredError):
            result["status"] = "deletion accepted; confirmation pending"
        _emit(ctx, result)
