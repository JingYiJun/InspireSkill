"""SDK file transfer orchestration using the same bridge preference as exec."""
from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath
from typing import Any, TYPE_CHECKING

from inspire.platform.web.browser_api.jupyter_terminal import _notebook_jupyter_url
from inspire.platform.web.jupyter_urls import jupyter_server_base
from inspire.platform.web.flow import call, perform_sync, blocking_io
from inspire.services import remote_exec, notebook_transfer as core
from .exceptions import ValidationError, TransportError
if TYPE_CHECKING:
    from .notebooks import Notebooks
from .compute_jobs import duration


def transfer(
    service: Notebooks, ref: Any, *, local: str | Path, remote: str,
    workspace: Any, transport: str, recursive: bool, overwrite: bool,
    timeout: float, max_bytes: int, download: bool,
) -> core.TransferResult:
    duration(timeout)
    if transport not in ("auto", "jupyter", "ssh"):
        raise ValidationError("transport must be auto, jupyter, or ssh.")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValidationError("max_bytes must be a positive integer.")
    remote = core.remote_path(remote)
    path = Path(local).absolute()
    _validate_local(path, download, overwrite, recursive)
    resolved = service._resolve(ref, workspace)
    bridge = None
    if transport != "jupyter":
        bridge = perform_sync(call(remote_exec.cached_notebook_bridge,
            notebook_id=resolved.key, workspace_id=resolved.workspace_id,
            account=service.client.account,
        ))
    if bridge is not None:
        return perform_sync(call(core.transfer_ssh,
            local=str(path), remote=remote, download=download, recursive=recursive,
            overwrite=overwrite, bridge_name=bridge, account=service.client.account,
            timeout=min(timeout, service.client._transport.remaining()),
        ))
    if transport == "ssh":
        raise ValidationError(
            "No reachable cached SSH bridge. Run "
            f"`inspire notebook connection refresh {resolved.name}` first."
        )
    if recursive:
        raise ValidationError("Recursive transfers require transport='ssh' and a cached bridge.")
    return jupyter_transfer(service, notebook_id=resolved.key, local=path, remote=remote,
                            download=download, overwrite=overwrite, timeout=timeout,
                            max_bytes=max_bytes)


def jupyter_transfer(
    service: Notebooks, *, notebook_id: str, local: Path, remote: str,
    download: bool, overwrite: bool, timeout: float, max_bytes: int,
) -> core.TransferResult:
    if not download:
        data = _read_upload(local, max_bytes)
    owner = service.client._transport
    lab = _notebook_jupyter_url(service.session, notebook_id)
    if not lab:
        raise ValidationError("Notebook has no Jupyter access URL.")
    base = jupyter_server_base(lab)
    with owner.application_connection(lab) as http:
        entrance = http.get(lab, timeout=timeout, allow_redirects=True)
        if entrance.status_code != 200:
            raise TransportError("Cannot open Jupyter entrance.")

        def send(method: str, path: str, **options: Any) -> Any:
            response = http.request(
                method, core.contents_url(base, path), timeout=timeout,
                headers={"X-XSRFToken": str(http.cookies.get("_xsrf") or "")}, **options,
            )
            if response.status_code not in (200, 201, 204, 404):
                raise TransportError(f"Jupyter contents request failed ({response.status_code}).")
            return response

        def metadata(path: str) -> dict[str, Any] | None:
            response = send("GET", path, params={"content": 0}, allow_not_found=True)
            return None if response.status_code == 404 else response.json()

        existing = metadata(remote)
        if download:
            if existing is None or existing.get("type") != "file":
                raise ValidationError("Remote source must be a file; use transport='ssh' for directories.")
            size = existing.get("size")
            if type(size) is not int or size < 0:
                raise ValidationError("Jupyter omitted file size; use transport='ssh'.")
            core.check_size(size, max_bytes)
            response = send("GET", remote, params={"format": "base64", "content": 1})
            if response.status_code != 200:
                raise TransportError("Jupyter source disappeared during download.")
            model = response.json()
            content = model.get("content")
            if model.get("format") != "base64" or not isinstance(content, str):
                raise TransportError("Jupyter did not return base64 file content.")
            encoded = "".join(content.split())
            padding = len(encoded) - len(encoded.rstrip("="))
            core.check_size((len(encoded) // 4) * 3 - padding, max_bytes)
            data = base64.b64decode(encoded, validate=True)
            core.check_size(len(data), max_bytes)
            _publish_download(data, local, overwrite)
        else:
            if existing is not None:
                if not overwrite:
                    raise ValidationError(f"Destination already exists: {remote}")
                if existing.get("type") != "file":
                    raise ValidationError("Remote destination is not a file.")
            for parent in reversed(PurePosixPath(remote).parents):
                if str(parent) in (".", "/"):
                    continue
                entry = metadata(str(parent))
                if entry is not None:
                    if entry.get("type") != "directory":
                        raise ValidationError(f"Remote parent is not a directory: {parent}")
                else:
                    with owner.single_send():
                        response = send("PUT", str(parent), json={"type": "directory"})
                        if response.status_code not in (200, 201):
                            raise TransportError("Jupyter did not create the parent directory.")
            with owner.single_send():
                response = send("PUT", remote, json={
                    "type": "file", "format": "base64",
                    "content": base64.b64encode(data).decode("ascii"),
                })
                if response.status_code not in (200, 201):
                    raise TransportError("Jupyter did not confirm the upload.")
    return core.TransferResult(str(local), remote, len(data), "jupyter")


@blocking_io
def _validate_local(path: Path, download: bool, overwrite: bool, recursive: bool) -> None:
    if download:
        if not overwrite and (path.exists() or path.is_symlink()):
            raise ValidationError(f"Destination already exists: {path}")
    else:
        core.inventory(path)
        if path.is_dir() and not recursive:
            raise ValidationError("Directories require recursive=True and transport='ssh'.")


@blocking_io
def _read_upload(local: Path, max_bytes: int) -> bytes:
    core.check_size(local.stat().st_size, max_bytes)
    with local.open("rb") as source:
        data = source.read(max_bytes + 1)
    core.check_size(len(data), max_bytes)
    return data


@blocking_io
def _publish_download(data: bytes, local: Path, overwrite: bool) -> None:
    with core.temporary_directory() as directory:
        staged = Path(directory) / "payload"
        staged.write_bytes(data)
        core.publish(staged, local, overwrite)
