"""SDK behavior contracts; all account state is isolated and all network calls are fake."""

from __future__ import annotations
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspire.sdk import (
    InspireClient,
    Quota,
    JobRef,
    Resource,
    WorkspaceRef,
    Job,
    ValidationError,
    AmbiguousResourceError,
    ClientClosedError,
    ClientThreadError,
    SubmissionUncertainError,
    AuthenticationError,
    WaitTimeoutError,
)
from inspire.sdk.transport import Transport
from inspire.platform.web.session.models import WebSession, SessionExpiredError, TransientAPIError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".inspire"
    for name in ("alpha", "beta"):
        d = root / "accounts" / name
        d.mkdir(parents=True)
        (d / "config.toml").write_text('[auth]\nusername="test"\npassword="unused"\n')
    (root / "current").write_text("alpha")
    c = InspireClient()
    c._transport._session = WebSession(
        storage_state={"cookies": [{"name": "x", "value": "fake"}]},
        created_at=time.time(),
        workspace_id="ws-test",
        account="alpha",
        login_username="test",
        base_url=c.base_url,
    )
    yield c
    c.close()


def test_imports_are_lazy():
    script = 'import sys; from inspire import InspireClient; import inspire.platform.web.browser_api.jobs; assert not any(x.startswith("playwright") for x in sys.modules)'
    subprocess.run([sys.executable, "-c", script], check=True)


def test_account_fixed_and_scope_restored(client):
    from inspire.accounts import set_current_account, current_account

    set_current_account("beta")
    with client._transport.scope():
        assert current_account() == "alpha"
    assert current_account() == "beta"
    assert client.account == "alpha"


def test_thread_and_process_guards(client, monkeypatch):
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client._transport.check)
        with pytest.raises(ClientThreadError):
            future.result()
    monkeypatch.setattr("inspire.sdk.transport.os.getpid", lambda: -1)
    with pytest.raises(ClientThreadError):
        client._transport.check()
    monkeypatch.undo()


def test_close_is_owned_and_idempotent(client):
    counts = []
    other = Transport("beta", client.base_url, username="test")
    client._transport._http = SimpleNamespace(close=lambda: counts.append("alpha"))
    other._http = SimpleNamespace(close=lambda: counts.append("beta"))
    client.close()
    client.close()
    assert counts == ["alpha"]
    with pytest.raises(ClientClosedError):
        client._transport.check()
    other.check()
    other.close()
    assert counts == ["alpha", "beta"]


@pytest.mark.parametrize(
    "error",
    [
        SessionExpiredError("expired"),
        TransientAPIError("busy"),
        ValueError("bad JSON"),
        __import__("requests").exceptions.ReadTimeout("timeout"),
    ],
)
def test_create_never_replays(client, monkeypatch, error):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise error

    monkeypatch.setattr(client._transport, "_once", fail)
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("write refreshed"))
    client._transport.allow_browser = True
    client._transport.operation_id = "diagnostic-id"
    with pytest.raises(SubmissionUncertainError) as caught:
        with client._transport.scope():
            client._transport.request("POST", "/api/v2/train?Action=CreateJobConsole", body={})
    assert len(calls) == 1
    assert caught.value.operation_id == "diagnostic-id"
    assert caught.value.retryable is False


def test_envelope_read_retries_but_create_does_not(client, monkeypatch):
    replies = iter(
        [
            {"ResponseMetadata": {"Error": {"Code": "Throttling", "Message": "fake"}}},
            {"Result": {"ok": True}},
        ]
    )
    monkeypatch.setattr(client._transport, "_once", lambda *a, **k: next(replies))
    monkeypatch.setattr("inspire.sdk.transport.time.sleep", lambda _: None)
    with client._transport.scope():
        assert client._transport.request("POST", "/api/v2/train?Action=ListJobs")["Result"]["ok"]
    calls = []

    def busy(*a, **k):
        calls.append(1)
        return {"ResponseMetadata": {"Error": {"Code": "InternalError", "Message": "fake"}}}

    monkeypatch.setattr(client._transport, "_once", busy)
    with pytest.raises(SubmissionUncertainError):
        client._transport.request("POST", "/api/v2/train?Action=CreateJobConsole")
    assert calls == [1]


def test_browser_disabled_on_expiry(client, monkeypatch):
    monkeypatch.setattr(
        client._transport, "_once", lambda *a, **k: (_ for _ in ()).throw(SessionExpiredError("x"))
    )
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("browser invoked"))
    with pytest.raises(AuthenticationError):
        client._transport.request("POST", "/api/v2/train?Action=ListJobs")


def test_invalid_ref_and_cursor_rejected(client):
    ref = JobRef("job", "beta", client.base_url, "key", "ws-test")
    with pytest.raises(ValidationError):
        client.jobs.get(ref)
    first = client.jobs.page(list(range(25)), query=("query",))
    assert first.items == tuple(range(20))
    with pytest.raises(ValidationError):
        client.jobs.page(list(range(25)), cursor=first.next_cursor, query=("other",))
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    assert JobRef.from_dict(ref.to_dict()) == ref
    assert "key" not in repr(ref)
    with pytest.raises(ValidationError):
        WorkspaceRef.from_dict(ref.to_dict())


def test_resolution_checks_beyond_first_page(client, monkeypatch):
    from inspire.platform.web.browser_api.jobs import JobInfo

    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    template = dict(
        status="job_running",
        command="",
        created_at="",
        finished_at=None,
        created_by_name="",
        created_by_id="",
        project_id="",
        project_name="",
        compute_group_name="",
        gpu_type="",
        gpu_count=1,
        instance_count=1,
        priority=4,
        workspace_id="ws-test",
    )
    rows = [
        JobInfo(job_id=str(i), name="same" if i in (0, 100) else str(i), **template)
        for i in range(101)
    ]
    calls = []

    def listing(**kw):
        calls.append(kw["page_num"])
        offset = (kw["page_num"] - 1) * 100
        return rows[offset : offset + 100], len(rows)

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_jobs", listing)
    with pytest.raises(AmbiguousResourceError):
        client.jobs.get("same", workspace="ws")
    assert calls == [1, 2]


def test_images_do_not_choose_first_source(client, monkeypatch):
    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.images.list_images_by_source",
        lambda source, **kw: [
            SimpleNamespace(name="image", version="v1", image_id=source, url="r/" + source)
        ],
    )
    with pytest.raises(AmbiguousResourceError):
        client.images.get("image:v1", workspace="ws")


def test_wait_binds_ref_unknown_is_not_success(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    states = iter(["UNKNOWN", "RUNNING", "SUCCEEDED"])
    calls = []

    def get(selector):
        calls.append(selector)
        status = next(states)
        return Job("job", ref, status, status)

    monkeypatch.setattr(client.jobs, "get", get)
    monkeypatch.setattr("inspire.sdk.jobs.time.sleep", lambda _: None)
    assert client.jobs.wait(ref).status == "SUCCEEDED"
    assert calls == [ref, ref, ref]


def test_wait_budget_does_not_stop(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr(client.jobs, "get", lambda _: Job("job", ref, "RUNNING", "job_running"))
    monkeypatch.setattr(client.jobs, "stop", lambda *a: pytest.fail("stopped"))
    with pytest.raises(WaitTimeoutError):
        client.jobs.wait(ref, timeout=0.005, poll_interval=0.01)


def test_running_job_not_deleted(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr(
        client.jobs, "get", lambda *a, **k: Job("job", ref, "RUNNING", "job_running")
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.jobs.delete_job", lambda *a, **k: pytest.fail("deleted")
    )
    with pytest.raises(ValidationError):
        client.jobs.delete(ref)


def test_log_window_and_global_budget(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    job = Job(
        "job",
        ref,
        "SUCCEEDED",
        "job_succeeded",
        created_at="1700000000000",
        finished_at="1700003600000",
    )
    monkeypatch.setattr(client.jobs, "get", lambda *a, **k: job)
    monkeypatch.setattr(client.jobs, "instances", lambda _: ("worker-0", "worker-1"))
    captured = []

    def logs(**kwargs):
        captured.append(kwargs)
        return [{"timestamp_ms": "1700003600000", "pod_name": "worker-1", "message": "x" * 100}], 50

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_train_job_logs", logs)
    result = client.jobs.logs(ref, limit=1, max_chars=20)
    assert len(result.text) == 20 and result.truncated
    assert captured[0]["pod_names"] == ["worker-0", "worker-1"]
    assert captured[0]["end_timestamp_ms"] == 1700003600000
    with pytest.raises(TypeError):
        client.jobs.logs(ref, tail=1)


def test_quota_validation():
    with pytest.raises(ValidationError):
        Quota(True, 20, 200)
    with pytest.raises(ValidationError):
        Quota(1, 0, 200)


def test_sdk_and_cli_payloads_match(monkeypatch):
    from inspire.config import Config
    from inspire.cli.utils import job_submit
    from inspire.services import job_submission
    from inspire.services.quotas import ResolvedQuota

    quota = ResolvedQuota("q", "g", "group", 1, 20, 200, "GPU", {"gpu_info": {"gpu_type": "GPU"}})
    monkeypatch.setattr(job_submit, "resolve_image_url", lambda *a, **k: "registry/image:v1")
    kwargs = dict(
        config=Config("test", "unused"),
        name="job",
        command="python train.py",
        quota=quota,
        framework="pytorch",
        project_id="p",
        workspace_id="w",
        priority=4,
        nodes=2,
        max_time_hours=1,
        shm_size=64,
    )
    a = job_submit.build_training_job_plan(image="display:v1", **kwargs)
    b = job_submission.build_training_job_plan(image="registry/image:v1", **kwargs)
    assert a.create_kwargs == b.create_kwargs


def test_list_fetches_only_needed_pages(client, monkeypatch):
    from inspire.platform.web.browser_api.jobs import JobInfo
    from dataclasses import fields

    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    template = {f.name: "" for f in fields(JobInfo)}
    calls = []

    def listing(**kw):
        calls.append(kw["page_num"])
        start = (kw["page_num"] - 1) * 100
        return [
            JobInfo(**{**template, "job_id": str(i), "name": str(i), "status": "job_running"})
            for i in range(start, start + 100)
        ], 10000

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_jobs", listing)
    first = client.jobs.list(workspace="ws", limit=3)
    second = client.jobs.list(workspace="ws", limit=3, cursor=first.next_cursor)
    assert [x.name for x in first.items + second.items] == [str(i) for i in range(6)]
    assert calls == [1, 1]
    assert first.total is None
    with pytest.raises(ValidationError):
        client.jobs.list(workspace="ws", status="RUNNING", cursor=first.next_cursor)


@pytest.fixture
def planned(client, monkeypatch):
    from inspire.sdk import (
        ProjectRef,
        ComputeGroupRef,
        ImageRef,
        Image,
        JobCreateSpec,
        QuotaRef,
        QuotaOption,
    )
    from inspire.services.quotas import ResolvedQuota

    ws = Resource(
        "workspace", client.workspaces.ref(WorkspaceRef, "workspace", "ws-test", "ws-test")
    )
    project = Resource("project", client.projects.ref(ProjectRef, "project", "p", "ws-test"))
    group = Resource("group", client.compute_groups.ref(ComputeGroupRef, "group", "g", "ws-test"))
    img = Image(
        "image:v1",
        client.images.ref(ImageRef, "image:v1", "i", "ws-test"),
        "private",
        "registry/image:v1",
    )
    quota = Quota(1, 20, 200)
    option = QuotaOption(
        "1,20,200", client.jobs.ref(QuotaRef, "1,20,200", "q", "ws-test"), quota, group.ref, "GPU"
    )
    resolved = ResolvedQuota(
        "q", "g", "group", 1, 20, 200, "GPU", {"gpu_info": {"gpu_type": "GPU"}}
    )
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(
        client.projects, "_all", lambda _: [(project, SimpleNamespace(priority_name="HIGH"))]
    )
    monkeypatch.setattr(
        client.compute_groups,
        "_all",
        lambda _: [(group, {"support_job_type_list": '["distributed_training"]'})],
    )
    monkeypatch.setattr(client.images, "get", lambda *a, **kw: img)
    monkeypatch.setattr(client.jobs, "_quota_rows", lambda *a: [(option, resolved)])
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.workspaces.is_fair_scheduling_workspace", lambda *a: False
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.availability.get_quota_priority_levels",
        lambda *a, **kw: {},
    )
    return JobCreateSpec(
        "job", ws.ref, project.ref, group.ref, quota, img.ref, "python train.py", priority=4
    )


def test_create_plan_and_single_dispatch(client, planned, monkeypatch):
    calls = []

    def create(**kw):
        calls.append(kw["payload"])
        return {"job_id": "created"}

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.create_training_job", create)
    monkeypatch.setattr(client.jobs, "get", lambda *a, **kw: pytest.fail("post-create read"))
    preview = client.jobs.plan(planned)
    assert preview.quota == Quota(1, 20, 200) and calls == []
    handle = client.jobs.create(planned)
    assert handle.ref.key == "created" and len(calls) == 1
    assert "python train.py" not in repr(planned)
    assert "python train.py" not in preview.summary


def test_create_missing_acknowledgement_is_uncertain(client, planned, monkeypatch):
    calls = []

    def once(**kw):
        calls.append(1)
        return {}

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.create_training_job", once)
    identifier = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(SubmissionUncertainError) as exc:
        client.jobs.create(planned, operation_id=identifier)
    assert calls == [1] and exc.value.operation_id == identifier


def test_plan_rejects_unsupported_group(client, planned, monkeypatch):
    group = client.compute_groups._all(None)[0][0]
    monkeypatch.setattr(
        client.compute_groups,
        "_all",
        lambda _: [(group, {"support_job_type_list": '["interactive_modeling"]'})],
    )
    with pytest.raises(ValidationError, match="does not support"):
        client.jobs.plan(planned)


def test_unknown_get_is_not_assumed_replayable(client, monkeypatch):
    from inspire.sdk import MutationUncertainError

    calls = []

    def fail(*a, **kw):
        calls.append(1)
        raise TransientAPIError("busy")

    monkeypatch.setattr(client._transport, "_once", fail)
    with pytest.raises(MutationUncertainError):
        client._transport.request("GET", "/api/v2/train?Action=GetUnknownSideEffect")
    assert calls == [1]


def test_business_rejection_is_definite(client, monkeypatch):
    monkeypatch.setattr(
        client._transport,
        "_once",
        lambda *a, **kw: {
            "ResponseMetadata": {"Error": {"Code": "InvalidParameter", "Message": "secret body"}}
        },
    )
    with pytest.raises(ValidationError) as error:
        client._transport.request("POST", "/api/v2/train?Action=CreateJobConsole")
    assert "secret body" not in str(error.value)


def test_refresh_cooldown_has_structured_deadline(client, monkeypatch):
    from inspire.sdk import AuthenticationCooldownError
    from inspire.platform.web.session.models import AuthenticationError as PlatformAuthError
    from contextlib import nullcontext

    deadline = time.time() + 60

    def fail(**kw):
        error = PlatformAuthError("private login detail")
        error.retry_at = deadline
        raise error

    monkeypatch.setattr("inspire.platform.web.session.models.WebSession.load", lambda **kw: None)
    monkeypatch.setattr(
        "inspire.platform.web.session.refresh_lock.exclusive_session_refresh",
        lambda *a, **kw: nullcontext(),
    )
    monkeypatch.setattr("inspire.platform.web.session.auth.get_web_session", fail)
    client._transport.allow_browser = True
    with pytest.raises(AuthenticationCooldownError) as caught:
        client._transport._refresh()
    assert caught.value.retry_at == deadline
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("status", [401, 302, 429, 500, 503])
def test_http_create_errors_do_not_replay(client, monkeypatch, status):
    calls = []

    def request(*a, **kw):
        calls.append(kw)
        return SimpleNamespace(status_code=status)

    client._transport._http = SimpleNamespace(request=request, close=lambda: None)
    monkeypatch.setattr("inspire.platform.web.session.requests._configure", lambda *a: None)
    with pytest.raises(SubmissionUncertainError):
        client._transport.request("POST", "/api/v2/train?Action=CreateJobConsole")
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False


def test_partial_image_catalog_cannot_resolve_unique_name(client, monkeypatch):
    from inspire.sdk import ResolutionIncompleteError

    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)

    def listing(source, **kw):
        if source == "private":
            raise RuntimeError("unreadable")
        return [SimpleNamespace(name="image", version="v1", image_id="i", url="r/i")]

    monkeypatch.setattr("inspire.platform.web.browser_api.images.list_images_by_source", listing)
    with pytest.raises(ResolutionIncompleteError):
        client.images.get("image:v1", workspace=ws.ref)
