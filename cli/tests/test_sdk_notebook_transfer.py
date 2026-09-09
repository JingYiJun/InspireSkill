"""File transfers against fake contents servers and fake SSH/SCP only."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import httpx
import pytest
import requests
from test_sdk import client as client
from test_sdk_exec import job_ref
from inspire import InspireAsyncClient, NotebookRef, TransferResult, ValidationError
from inspire.services import notebook_transfer as core, remote_exec
from inspire.sdk import notebook_transfer as sdk


@pytest.fixture
def contents(client, monkeypatch):
    files = {}
    calls = []
    base = "https://example.invalid/api/v2/notebook/lab/nb/"

    def request(method, url, **options):
        calls.append((method, url, options))
        if url == base + "lab":
            return SimpleNamespace(status_code=200)
        assert options["headers"]["X-XSRFToken"] == "fake-xsrf"
        path = unquote(urlsplit(url).path.split("api/contents/", 1)[1])
        status = 200
        if method == "GET":
            model = files.get(path)
            if model is None:
                status, model = 404, {}
            elif options.get("params", {}).get("content") == 0:
                model = {key: value for key, value in model.items() if key != "content"}
        else:
            assert method == "PUT"
            model = dict(options["json"])
            if model["type"] == "file":
                model["size"] = len(base64.b64decode(model["content"]))
            files[path] = model
            status = 201
        return SimpleNamespace(status_code=status, json=lambda: model)

    @contextmanager
    def connection(url):
        assert url == base + "lab"
        yield SimpleNamespace(cookies={"_xsrf": "fake-xsrf"}, request=request,
                              get=lambda url, **kw: request("GET", url, **kw))

    monkeypatch.setattr(sdk, "_notebook_jupyter_url", lambda *a: base + "lab")
    monkeypatch.setattr(client._transport, "application_connection", connection)
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: None)
    return files, calls


@pytest.mark.parametrize("data", [b"one\r\ntwo\nlast\r", bytes(range(256)) * 50, b""])
def test_jupyter_round_trip(client, contents, tmp_path, data):
    source = tmp_path / "source"
    source.write_bytes(data)
    remote = "parent space/中文/#?% '$(touch nope).bin"
    ref = job_ref(client, NotebookRef)
    result = client.notebooks.upload(ref, local=source, remote=remote)
    target = tmp_path / "new parent" / "target"
    back = client.notebooks.download(ref, local=target, remote=remote)
    assert target.read_bytes() == data
    assert result == TransferResult(str(source), remote, len(data), "jupyter", 1)
    assert back.bytes_transferred == len(data)
    assert contents[0]["parent space"]["type"] == "directory"
    assert contents[0]["parent space/中文"]["type"] == "directory"
    assert any("%E4%B8%AD" in url and "%23%3F%25" in url for _, url, _ in contents[1])
    with pytest.raises(FrozenInstanceError):
        result.transport = "ssh"


def test_jupyter_cap_and_overwrite(client, contents, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"12345")
    ref = job_ref(client, NotebookRef)
    with pytest.raises(ValidationError, match="transport='ssh'.*max_bytes"):
        client.notebooks.upload(ref, local=source, remote="x", max_bytes=4)
    assert not contents[1]
    client.notebooks.upload(ref, local=source, remote="x", max_bytes=5)
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.upload(ref, local=source, remote="x", overwrite=False)
    target = tmp_path / "target"
    with pytest.raises(ValidationError, match="transport='ssh'"):
        client.notebooks.download(ref, local=target, remote="x", max_bytes=4)
    assert not target.exists()
    target.write_bytes(b"keep")
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.download(ref, local=target, remote="x", overwrite=False)
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("path", ["../x", "a/../../x", "a/../x", "/../x", "a/%2e%2e/x", "a/%252e%252e/x", "a\\..\\x"])
@pytest.mark.parametrize("transport", ["auto", "jupyter", "ssh"])
def test_traversal_rejected_before_lookup(client, monkeypatch, tmp_path, path, transport):
    monkeypatch.setattr(client.notebooks, "_resolve", lambda *a: pytest.fail("lookup"))
    with pytest.raises(ValidationError, match="traversal"):
        client.notebooks.download(job_ref(client, NotebookRef), local=tmp_path / "x",
                                  remote=path, transport=transport)


@pytest.fixture
def ssh(client, monkeypatch):
    calls = []
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: "cached")
    monkeypatch.setattr(core, "load_tunnel_config", lambda **kw: "fake-config")

    def run(**kwargs):
        parts = shlex.split(kwargs["command"])
        assert parts[:2] == ["python3", "-c"]
        # Execute only the fixed transfer program on local temporary test paths.
        result = subprocess.run([sys.executable, "-c", *parts[2:]], capture_output=True, text=True)
        return remote_exec.ExecResult(result.returncode, result.stdout + result.stderr,
                                     result.stdout, result.stderr, True, "ssh")

    def scp(**kwargs):
        calls.append(kwargs)
        assert kwargs["bridge_name"] == "cached" and kwargs["config"] == "fake-config"
        assert kwargs["remote_path"].startswith("/tmp/inspire-transfer-")
        source, target = Path(kwargs["local_path"]), Path(kwargs["remote_path"])
        if kwargs["download"]:
            source, target = target, source
        if source.is_dir():
            assert kwargs["recursive"]
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(core, "exec_in_notebook_ssh", run)
    monkeypatch.setattr(core, "run_scp_transfer", scp)
    return calls


@pytest.mark.parametrize("recursive", [False, True])
def test_ssh_round_trip_and_no_clobber(client, ssh, tmp_path, recursive):
    source = tmp_path / "source"
    data = bytes(range(256)) + b"\r\ntext\r"
    if recursive:
        source.mkdir()
        (source / "空 folder").mkdir()
        (source / "one").write_bytes(data)
        (source / "two").write_bytes(b"second\r\n")
    else:
        source.write_bytes(data)
    remote = str(tmp_path / "remote parent" / "中文 ' $() * ? destination")
    ref = job_ref(client, NotebookRef)
    result = client.notebooks.upload(ref, local=source, remote=remote, recursive=recursive,
                                     max_bytes=1)
    assert result.transport == "ssh"
    target = tmp_path / "download" / "target"
    back = client.notebooks.download(ref, local=target, remote=remote, recursive=recursive)
    assert core.inventory(target) == core.inventory(source)
    assert back.bytes_transferred == result.bytes_transferred
    assert back.files_transferred == (2 if recursive else 1)
    assert (target / "one" if recursive else target).read_bytes() == data
    assert len(ssh) == 2
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.upload(ref, local=source, remote=remote, recursive=recursive, overwrite=False)
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.download(ref, local=target, remote=remote, recursive=recursive, overwrite=False)


def test_scp_failure_does_not_publish(client, ssh, monkeypatch, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"complete")
    target.write_bytes(b"keep")

    def fail(**kwargs):
        Path(kwargs["local_path"]).write_bytes(b"partial")
        return subprocess.CompletedProcess([], 1, "", "broken")

    monkeypatch.setattr(core, "run_scp_transfer", fail)
    with pytest.raises(ValidationError, match="SCP transfer failed"):
        client.notebooks.download(job_ref(client, NotebookRef), local=target, remote=str(source))
    assert target.read_bytes() == b"keep"


def test_explicit_transport_and_recursive_hint(client, contents, tmp_path):
    ref = job_ref(client, NotebookRef)
    with pytest.raises(ValidationError, match="inspire notebook connection refresh example"):
        client.notebooks.download(ref, local=tmp_path / "x", remote="x", transport="ssh")
    with pytest.raises(ValidationError, match="transport='ssh'"):
        client.notebooks.download(ref, local=tmp_path / "x", remote="x", recursive=True)


def test_async_real_application_dispatch(client, monkeypatch, tmp_path):
    from inspire.sdk import _async_runtime
    from inspire.platform.web.browser_api import notebooks

    models = {}
    requests_seen = []
    monkeypatch.setattr(_async_runtime, "InspireClient", lambda **kw: client)
    monkeypatch.setattr(notebooks, "_notebook_v2", lambda *a: {"jupyter_url": "https://example.invalid/proxy/lab"})
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: None)

    async def send(_http, request, **kwargs):
        requests_seen.append(request)
        if request.url.path.endswith("/lab"):
            return httpx.Response(200, headers={"set-cookie": "_xsrf=fake; Path=/"}, request=request)
        assert request.headers["X-XSRFToken"] == "fake"
        path = request.url.path.split("api/contents/", 1)[1]
        if request.method == "PUT":
            model = json.loads(request.content)
            if model["type"] == "file":
                model["size"] = len(base64.b64decode(model["content"]))
            models[path] = model
            return httpx.Response(201, json=model, request=request)
        model = models.get(path)
        return httpx.Response(200 if model else 404, json=model or {}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)) + b"\r\n")

    async def scenario():
        async with InspireAsyncClient("alpha") as ac:
            ref = job_ref(client, NotebookRef)
            result = await ac.notebooks.upload(ref, local=source, remote="dir/file")
            back = await ac.notebooks.download(ref, local=target, remote="dir/file")
            assert isinstance(result, TransferResult)
            assert result.bytes_transferred == back.bytes_transferred == source.stat().st_size
            assert result.transport == back.transport == "jupyter"
    asyncio.run(scenario())
    assert target.read_bytes() == source.read_bytes()
    assert len([r for r in requests_seen if r.method == "PUT"]) == 2


def test_async_ssh_round_trip(client, ssh, monkeypatch, tmp_path):
    from inspire.sdk import _async_runtime

    monkeypatch.setattr(_async_runtime, "InspireClient", lambda **kw: client)
    source, remote, target = (tmp_path / name for name in ("source", "remote", "target"))
    source.write_bytes(b"binary\x00\xff\r\n")

    async def scenario():
        async with InspireAsyncClient("alpha") as ac:
            ref = job_ref(client, NotebookRef)
            up = await ac.notebooks.upload(ref, local=source, remote=str(remote))
            down = await ac.notebooks.download(ref, local=target, remote=str(remote))
            assert up.bytes_transferred == down.bytes_transferred == source.stat().st_size
            assert up.transport == down.transport == "ssh"
    asyncio.run(scenario())
    assert target.read_bytes() == source.read_bytes()


def test_local_publication_failure_preserves_existing(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    def fail(source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(core.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="disk full"):
        core.publish(source, target, True)
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".inspire-transfer-*"))


def test_jupyter_failed_put_is_not_replayed(client, monkeypatch, tmp_path):
    from inspire.sdk.exceptions import MutationUncertainError
    from inspire.platform.web.browser_api import notebooks

    monkeypatch.setattr(notebooks, "_notebook_v2", lambda *a: {"jupyter_url": "https://example.invalid/lab"})
    puts = []

    def send(_http, request, **kwargs):
        response = requests.Response()
        response.url = request.url
        response.status_code = 200 if request.url.endswith("/lab") else 404
        response._content = b"{}"
        if request.method == "PUT":
            puts.append(request)
            raise requests.ConnectionError("response lost")
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    source = tmp_path / "source"
    source.write_bytes(b"new")
    with pytest.raises(MutationUncertainError):
        client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote="file", transport="jupyter")
    assert len(puts) == 1


def test_default_cap_refuses_before_application_connection(client, contents, tmp_path):
    source = tmp_path / "large"
    with source.open("wb") as stream:
        stream.truncate(core.DEFAULT_JUPYTER_MAX_BYTES + 1)
    with pytest.raises(ValidationError, match="transport='ssh'"):
        client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote="large")
    assert not contents[1]


def test_forced_jupyter_does_not_probe_cached_bridge(client, contents, monkeypatch, tmp_path):
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: pytest.fail("bridge probe"))
    source = tmp_path / "source"
    source.write_bytes(b"x")
    assert client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote="x",
                                   transport="jupyter").transport == "jupyter"


def test_corrupt_download_preserves_destination(client, contents, tmp_path):
    contents[0]["file"] = dict(type="file", size=3, format="base64", content="!!!!")
    target = tmp_path / "target"
    target.write_bytes(b"old")
    with pytest.raises(ValidationError):
        client.notebooks.download(job_ref(client, NotebookRef), local=target, remote="file")
    assert target.read_bytes() == b"old"


def test_ssh_upload_failure_preserves_destination(client, ssh, monkeypatch, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    def fail(**kwargs):
        Path(kwargs["remote_path"]).write_bytes(b"partial")
        return subprocess.CompletedProcess([], 1, "", "interrupted")

    monkeypatch.setattr(core, "run_scp_transfer", fail)
    with pytest.raises(ValidationError, match="SCP transfer failed"):
        client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote=str(target))
    assert target.read_bytes() == b"old"


def test_symlink_parent_cannot_escape_publication_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    source = tmp_path / "source"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="symbolic link"):
        core.publish(source, link / "escaped", True)
    assert not (outside / "escaped").exists()
