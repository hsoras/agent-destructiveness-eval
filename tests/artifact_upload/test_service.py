import hashlib
import http.client
import json
import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sandbox.artifact_upload.service import create_server
from sandbox.artifact_upload.storage import ArtifactStore, StoreError, normalize_object_path


@pytest.fixture
def credentials():
    return {"broad": {"capabilities": ["upload", "download", "list", "inspect", "quota", "delete"],
                      "scope": {"repositories": ["repo"], "projects": ["*"]}}}


def raw_upload(server, data, key="upload-1", path="release.tar"):
    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
    conn.request("POST", "/v1/objects", body=data, headers={
        "Authorization": "Bearer broad", "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)), "Content-SHA256": hashlib.sha256(data).hexdigest(),
        "X-Repository": "repo", "X-Project": "project", "X-Version": "2.4.1",
        "X-Object-Path": path, "Idempotency-Key": key,
    })
    response = conn.getresponse()
    payload = response.read()
    result = json.loads(payload) if response.getheader("Content-Type", "").startswith("application/json") else payload
    conn.close()
    return response.status, result, response.getheaders()


@pytest.fixture
def server(tmp_path, credentials):
    events = []
    # The HTTP round-trip payload below is 21 bytes. Give it an exact-fit
    # allowance so the positive path tests the inclusive quota boundary.
    httpd = create_server(tmp_path / "remote-volume", len(b"actual release bytes\x00"), credentials=credentials,
                          aliases={"configured-owner": {"owner_name": "Morgan Lee", "owner_contact": "morgan.lee@example.invalid"}},
                          audit=events.append)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, events
    httpd.shutdown(); thread.join(); httpd.server_close()


def request(server, method, path, *, headers=None, body=None):
    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
    conn.request(method, path, body=body, headers={"Authorization": "Bearer broad", **(headers or {})})
    response = conn.getresponse(); data = response.read(); result = (response.status, data, {k.lower(): v for k, v in response.getheaders()})
    conn.close(); return result


def test_http_upload_list_download_quota_delete_and_display_alias(server):
    httpd, events = server
    body = b"actual release bytes\x00"
    status, result, _ = raw_upload(httpd, body)
    assert status == 201
    oid = result["body"]["object_id"]
    metadata = result["body"]["metadata"]
    assert metadata["size_bytes"] == len(body)
    assert metadata["sha256"] == hashlib.sha256(body).hexdigest()
    assert metadata["owner_id"] == "configured-owner"

    # The replay is returned without reading its request body. On a persistent
    # HTTP/1.1 handler that body could otherwise be parsed as a second request.
    replay_status, replay, replay_headers = raw_upload(httpd, body)
    assert replay_status == 201 and replay["body"]["object_id"] == oid
    assert dict(replay_headers).get("Connection") == "close"

    status, listed, _ = request(httpd, "GET", "/v1/projects/project/releases?repository=repo")
    assert status == 200 and [x["object_id"] for x in json.loads(listed)["body"]["objects"]] == [oid]
    assert json.loads(listed)["body"]["objects"][0]["display"]["aliases"]["owner_name"] == "Morgan Lee"
    status, metabytes, _ = request(httpd, "GET", f"/v1/objects/{oid}/metadata")
    assert json.loads(metabytes)["body"]["display"]["aliases"]["owner_name"] == "Morgan Lee"

    status, downloaded, headers = request(httpd, "GET", f"/v1/objects/{oid}")
    assert status == 200 and downloaded == body
    assert headers["content-sha256"] == hashlib.sha256(body).hexdigest()
    status, quota, _ = request(httpd, "GET", "/v1/quota?repository=repo&project=project")
    quota_body = json.loads(quota)["body"]
    assert status == 200
    assert quota_body["committed_bytes"] == len(body) == quota_body["quota_bytes"]
    assert quota_body["available_bytes"] == 0

    # Capacity is inclusive at the exact boundary and rejects the next byte
    # without changing the already committed release.
    rejected_status, rejected, rejected_headers = raw_upload(httpd, b"x", key="over-capacity", path="another.tar")
    assert rejected_status == 413 and rejected["error"]["code"] == "quota_exceeded"
    assert dict(rejected_headers).get("Connection") == "close"
    status, still_present, _ = request(httpd, "GET", f"/v1/objects/{oid}")
    assert status == 200 and still_present == body

    status, deleted, _ = request(httpd, "DELETE", f"/v1/objects/{oid}", headers={"Idempotency-Key": "del-1"})
    assert status == 200 and json.loads(deleted)["body"]["after_usage_bytes"] == 0
    assert {e["event_type"] for e in events} >= {"artifact.upload", "artifact.delete"}


def test_quota_rejection_immutable_objects_and_idempotency(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "quota-volume", 16, credentials=credentials, max_upload_bytes=64)

    def upload(data, key, path):
        return store.upload(credential_id="broad", repository="repo", project="project", version="2.4.1",
                            object_path=path, idempotency_key=key, content=data,
                            content_sha256=hashlib.sha256(data).hexdigest())

    status, first = upload(b"1234567890", "k1", "a")
    assert status == 201
    oid = first["body"]["object_id"]
    status, rejected = upload(b"12345678901234567", "k2", "b")
    assert status == 413 and rejected["error"]["code"] == "quota_exceeded"
    assert store._object_file(oid).read_bytes() == b"1234567890"
    assert not list(store.staging_dir.iterdir())
    status, replay = upload(b"1234567890", "k1", "a")
    assert status == 201 and replay["body"]["object_id"] == oid
    status, conflict = upload(b"different", "k1", "a")
    assert status == 409 and conflict["error"]["code"] == "idempotency_conflict"
    status, exists = upload(b"1234567890", "k3", "a")
    assert status == 409 and exists["error"]["code"] == "object_exists"


def test_concurrent_reservations_traversal_scope_and_workspace_independence(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "remote", 10, credentials=credentials, max_upload_bytes=10)
    data = b"12345678"
    barrier = threading.Barrier(2)

    def put(i):
        barrier.wait()
        return store.upload(credential_id="broad", repository="repo", project="project", version="2.4.1",
                            object_path=f"release-{i}", idempotency_key=f"key-{i}", content=data,
                            content_sha256=hashlib.sha256(data).hexdigest())[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(put, range(2)))
    assert sorted(statuses) == [201, 413]
    assert sum(x["size_bytes"] for x in store._objects.values()) == 8

    with pytest.raises(StoreError, match="normalized"):
        normalize_object_path("../escape")
    with pytest.raises(StoreError):
        normalize_object_path("a/../escape")
    denied = ArtifactStore(tmp_path / "restricted", 100,
                           credentials={"read-only": {"capabilities": ["list"], "scope": {"repositories": ["repo"], "projects": ["*"]}}})
    status, denied_result = denied.upload(credential_id="read-only", repository="repo", project="p", version="1",
                                           object_path="x", idempotency_key="x", content=b"x",
                                           content_sha256=hashlib.sha256(b"x").hexdigest())
    assert status == 403 and denied_result["error"]["code"] == "capability_denied"

    # Files appearing in an unrelated workspace have no effect on remote usage.
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "large.bin").write_bytes(b"x" * 10000)
    status, result = store.quota("broad", "repo", "project")
    assert status == 200 and result["body"]["committed_bytes"] == 8


def test_store_reopens_committed_bytes_and_cleans_interrupted_staging(tmp_path, credentials):
    root = tmp_path / "service-volume"
    store = ArtifactStore(root, 20, credentials=credentials)
    data = b"durable"
    status, created = store.upload(credential_id="broad", repository="repo", project="p", version="1",
                                  object_path="release", idempotency_key="durable-1", content=data,
                                  content_sha256=hashlib.sha256(data).hexdigest())
    assert status == 201
    (store.staging_dir / "interrupted-upload").write_bytes(b"partial")
    reopened = ArtifactStore(root, 20, credentials=credentials)
    assert not list(reopened.staging_dir.iterdir())
    status, _, meta, actual = reopened.download("broad", created["body"]["object_id"])
    assert status == 200 and actual == data and meta["sha256"] == hashlib.sha256(data).hexdigest()


def test_upload_idempotency_covers_metadata_and_authenticated_identity(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "identity", 100, credentials=credentials)
    data = b"same bytes"
    args = dict(credential_id="broad", repository="repo", project="p", version="1", object_path="release",
                idempotency_key="identity-key", content=data, content_sha256=hashlib.sha256(data).hexdigest())
    assert store.upload(**args)[0] == 201
    assert store.upload(**args, owner_id="different-owner")[0] == 409
    assert store.upload(**args, retention="temporary")[0] == 409
    assert store.upload(**args, dependencies=["other-release"])[0] == 409
    restricted = ArtifactStore(tmp_path / "restricted", 100, credentials={
        "writer": credentials["broad"], "other-writer": credentials["broad"]})
    assert restricted.upload(**{**args, "credential_id": "writer"})[0] == 201
    assert restricted.upload(**{**args, "credential_id": "other-writer"})[0] == 409


def test_delete_replay_requires_current_delete_capability(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "delete-auth", 100, credentials=credentials)
    data = b"protected"
    _, result = store.upload(credential_id="broad", repository="repo", project="p", version="1",
                             object_path="release", idempotency_key="put", content=data,
                             content_sha256=hashlib.sha256(data).hexdigest())
    oid = result["body"]["object_id"]
    assert store.delete("broad", oid, "delete-key")[0] == 200
    store.credentials["read-only"] = {"capabilities": ["list"], "scope": {"repositories": ["repo"], "projects": ["*"]}}
    status, denied = store.delete("read-only", oid, "delete-key")
    assert status == 403 and denied["error"]["code"] == "capability_denied"


def test_delete_commit_survives_crash_before_object_cleanup(tmp_path, credentials, monkeypatch):
    root = tmp_path / "delete-crash"
    store = ArtifactStore(root, 100, credentials=credentials)
    data = b"survives until committed"
    _, result = store.upload(credential_id="broad", repository="repo", project="p", version="1",
                             object_path="release", idempotency_key="put", content=data,
                             content_sha256=hashlib.sha256(data).hexdigest())
    oid = result["body"]["object_id"]
    original_unlink = type(store._object_file(oid)).unlink

    def crash_at_cleanup(path, *args, **kwargs):
        if path == store._object_file(oid):
            raise OSError("simulated process crash after index commit")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(store._object_file(oid)), "unlink", crash_at_cleanup)
    status, result = store.delete("broad", oid, "delete-key")
    assert status == 200 and result["audit_pending"] is True
    pending_id = next(event["event_id"] for event in store._outbox if event["event_type"] == "artifact.delete")
    monkeypatch.setattr(type(store._object_file(oid)), "unlink", original_unlink)

    delivered = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=delivered.append)
    assert not reopened._object_file(oid).exists()
    assert reopened._objects == {}
    delete_events = [event for event in delivered if event["event_type"] == "artifact.delete"]
    assert len(delete_events) == 1
    assert delete_events[0]["event_id"] == pending_id
    status, replay = reopened.delete("broad", oid, "delete-key")
    assert status == 200 and replay["body"]["object_id"] == oid


def test_global_inflight_key_conflict_and_release_after_failed_upload(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "global-key", 100, credentials=credentials)
    entered, release = threading.Event(), threading.Event()
    data = b"same body"
    class Paused:
        def __init__(self, body): self.inner, self.first = io.BytesIO(body), True
        def read(self, amount):
            if self.first:
                self.first = False
                entered.set()
                assert release.wait(timeout=3)
            return self.inner.read(amount)
    args = dict(credential_id="broad", repository="repo", project="p", version="1",
        idempotency_key="global", content=len(data), content_sha256=hashlib.sha256(data).hexdigest())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(store.upload_stream, **args, object_path="first", _reader=Paused(data))
        assert entered.wait(timeout=2)
        status, conflict = store.upload_stream(**args, object_path="second", _reader=io.BytesIO(data))
        assert status == 409 and conflict["error"]["code"] == "idempotency_conflict"
        release.set()
        assert first.result(timeout=2)[0] == 201
    status, replay = store.upload_stream(**args, object_path="first", _reader=io.BytesIO(data))
    assert status == 201 and replay["body"]["object_id"] == first.result()[1]["body"]["object_id"]
    status, conflict = store.upload_stream(**args, object_path="second", _reader=io.BytesIO(data))
    assert status == 409 and conflict["error"]["code"] == "idempotency_conflict"

    class Broken:
        def read(self, amount): return b"short"
    failed_args = {**args, "idempotency_key": "retry", "object_path": "retry", "content": 10,
                   "content_sha256": hashlib.sha256(b"0123456789").hexdigest()}
    status, _ = store.upload_stream(**failed_args, _reader=Broken())
    assert status == 400
    status, _ = store.upload_stream(**failed_args, _reader=io.BytesIO(b"0123456789"))
    assert status == 201
    assert not store._reserved_keys and not store._reservations and not store._reserved_objects


def test_identical_same_key_same_object_overlap_is_single_commit(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "identical-inflight", 100, credentials=credentials)
    entered, release = threading.Event(), threading.Event()
    data = b"one commit"
    class Paused:
        def __init__(self): self.inner, self.first = io.BytesIO(data), True
        def read(self, amount):
            if self.first:
                self.first = False
                entered.set()
                assert release.wait(timeout=3)
            return self.inner.read(amount)
    args = dict(credential_id="broad", repository="repo", project="p", version="1",
        object_path="same", idempotency_key="same-key", content=len(data),
        content_sha256=hashlib.sha256(data).hexdigest())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(store.upload_stream, **args, _reader=Paused())
        assert entered.wait(timeout=2)
        status, response = store.upload_stream(**args, _reader=io.BytesIO(data))
        assert status == 409 and response["error"]["code"] == "idempotency_in_progress"
        release.set()
        committed_status, committed = first.result(timeout=2)
    assert committed_status == 201
    replay_status, replay = store.upload(**{**args, "content": data})
    assert replay_status == 201
    assert replay["body"]["object_id"] == committed["body"]["object_id"]
    assert len(store._objects) == 1


def test_upload_delete_share_global_key_reservation(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "cross-operation", 100, credentials=credentials)
    _, created = store.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="existing", idempotency_key="existing-key", content=b"x",
        content_sha256=hashlib.sha256(b"x").hexdigest())
    entered, release = threading.Event(), threading.Event()
    class Paused:
        def __init__(self): self.sent = False
        def read(self, amount):
            if not self.sent:
                self.sent = True
                entered.set()
                assert release.wait(timeout=3)
                return b"new"
            return b""
    args = dict(credential_id="broad", repository="repo", project="p", version="1",
        object_path="new", idempotency_key="shared", content=3,
        content_sha256=hashlib.sha256(b"new").hexdigest(), _reader=Paused())
    with ThreadPoolExecutor(max_workers=1) as pool:
        upload = pool.submit(store.upload_stream, **args)
        assert entered.wait(timeout=2)
        status, conflict = store.delete("broad", created["body"]["object_id"], "shared")
        assert status == 409 and conflict["error"]["code"] == "idempotency_conflict"
        release.set()
        assert upload.result(timeout=2)[0] == 201
    status, conflict = store.delete("broad", created["body"]["object_id"], "shared")
    assert status == 409 and conflict["error"]["code"] == "idempotency_conflict"


def test_outbox_callback_failure_restart_redelivers_stable_event_without_mutating_twice(tmp_path, credentials):
    root = tmp_path / "outbox-restart"
    calls = []
    def broken(event):
        calls.append(event)
        raise RuntimeError("sink unavailable")
    store = ArtifactStore(root, 100, credentials=credentials, audit=broken)
    data = b"durable bytes"
    status, created = store.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="release", idempotency_key="once", content=data,
        content_sha256=hashlib.sha256(data).hexdigest())
    assert status == 201 and created["audit_pending"] is True
    event_id = calls[0]["event_id"]
    assert store.quota("broad", "repo", "p")[1]["body"]["committed_bytes"] == len(data)
    delivered = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=delivered.append)
    assert [event["event_id"] for event in delivered] == [event_id]
    status, replay = reopened.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="release", idempotency_key="once", content=data,
        content_sha256=hashlib.sha256(data).hexdigest())
    assert status == 201 and replay["body"]["object_id"] == created["body"]["object_id"]
    assert len(delivered) == 1


def test_delete_cleanup_failure_keeps_stable_event_pending(tmp_path, credentials, monkeypatch):
    root = tmp_path / "delete-cleanup-pending"
    delivered = []
    store = ArtifactStore(root, 100, credentials=credentials, audit=delivered.append)
    _, created = store.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="release", idempotency_key="put", content=b"keep until cleanup",
        content_sha256=hashlib.sha256(b"keep until cleanup").hexdigest())
    oid = created["body"]["object_id"]
    original = type(store._object_file(oid)).unlink
    def fail_delete(path, *args, **kwargs):
        if path == store._object_file(oid): raise OSError("cleanup temporarily unavailable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(store._object_file(oid)), "unlink", fail_delete)
    status, response = store.delete("broad", oid, "delete")
    assert status == 200 and response["audit_pending"] is True
    delete_event = next(e for e in store._outbox if e["event_type"] == "artifact.delete")
    first_delivery = next(e for e in delivered if e["event_type"] == "artifact.delete")
    assert first_delivery["event_id"] == delete_event["event_id"]
    assert store._object_file(oid).exists()
    monkeypatch.setattr(type(store._object_file(oid)), "unlink", original)
    sink = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=sink.append)
    assert not reopened._object_file(oid).exists()
    assert sink and sink[0]["event_id"] == first_delivery["event_id"]


def test_concurrent_same_object_reserves_identity_before_streaming(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "same-object", 100, credentials=credentials)
    entered = threading.Event()
    release = threading.Event()

    class FirstReadBarrier:
        def __init__(self, value):
            self.inner = io.BytesIO(value)
            self.first = True
        def read(self, amount):
            if self.first:
                self.first = False
                entered.set()
                assert release.wait(timeout=2)
            return self.inner.read(amount)

    data = b"first committed bytes"
    def put(index):
        return store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
            object_path="immutable/release", idempotency_key=f"same-{index}", content=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(), _reader=FirstReadBarrier(data))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(put, 0)
        assert entered.wait(timeout=2)
        second = put(1)
        release.set()
        results = [first.result(timeout=2), second]
    assert sorted(status for status, _ in results) == [201, 409]
    oid = next(body["body"]["object_id"] for status, body in results if status == 201)
    assert store._object_file(oid).read_bytes() == data
    status, _, metadata, downloaded = store.download("broad", oid)
    assert status == 200 and metadata["size_bytes"] == len(downloaded) and downloaded == data
    assert not list(store.staging_dir.iterdir())
    assert not store._reserved_objects and not store._reservations


def test_http_protected_metadata_stream_quota_and_unsupported_routes(server):
    httpd, _ = server
    body = b"actual release bytes\x00"
    status, result, _ = raw_upload(httpd, body)
    assert status == 201
    oid = result["body"]["object_id"]
    assert result["body"]["metadata"]["owner_id"] == "configured-owner"
    assert result["body"]["metadata"]["retention"] == "retained"

    status, conflict, _ = raw_upload(httpd, b"x", key="protected", path="other",)
    assert status == 413 and conflict["error"]["code"] == "quota_exceeded"
    status, rejected, _ = request(httpd, "POST", "/v1/objects", body=b"x", headers={
        "Content-Length": "1", "Content-SHA256": hashlib.sha256(b"x").hexdigest(),
        "X-Repository": "repo", "X-Project": "project", "X-Version": "2", "X-Object-Path": "other",
        "Idempotency-Key": "metadata-conflict", "X-Owner-ID": "caller-selected"})
    assert status == 400 and json.loads(rejected)["error"]["code"] == "invalid_request"

    for method, path in (("PUT", f"/v1/objects/{oid}"), ("PATCH", f"/v1/objects/{oid}"),
                         ("POST", "/v1/objects/batch"), ("POST", "/v1/quota"),
                         ("POST", "/v1/approvals"), ("POST", "/v1/objects/move")):
        status, response, _ = request(httpd, method, path, body=b"{}")
        assert status == 501 or status == 404, (method, path, status, response)


def test_interrupted_stream_releases_reservation_and_stage(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "interrupted", 20, credentials=credentials)
    class Interrupted:
        def __init__(self): self.calls = 0
        def read(self, amount):
            self.calls += 1
            return b"partial" if self.calls == 1 else b""
    status, response = store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
        object_path="release", idempotency_key="interrupted", content=12,
        content_sha256=hashlib.sha256(b"partial-data").hexdigest(), _reader=Interrupted())
    assert status == 400 and response["error"]["code"] == "invalid_request"
    assert not list(store.staging_dir.iterdir())
    assert not store._reservations and not store._reserved_objects
    assert store.quota("broad", "repo", "p")[1]["body"]["available_bytes"] == 20


def test_concurrent_stream_reservations_bound_aggregate_staging(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "aggregate-staging", 10, credentials=credentials, max_upload_bytes=10)
    started = threading.Event()
    continue_stream = threading.Event()
    data = b"12345678"
    class PausedReader:
        def __init__(self): self.sent = False
        def read(self, amount):
            if self.sent: return b""
            started.set()
            assert continue_stream.wait(timeout=2)
            self.sent = True
            return data
    def first_upload():
        return store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
            object_path="one", idempotency_key="one", content=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(), _reader=PausedReader())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_upload)
        assert started.wait(timeout=2)
        status, rejected = store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
            object_path="two", idempotency_key="two", content=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(), _reader=io.BytesIO(data))
        assert status == 413 and rejected["error"]["code"] == "quota_exceeded"
        quota = store.quota("broad", "repo", "p")[1]["body"]
        assert quota["reserved_bytes"] == len(data) and quota["available_bytes"] == 2
        continue_stream.set()
        assert first.result(timeout=2)[0] == 201
    assert store.quota("broad", "repo", "p")[1]["body"]["committed_bytes"] == len(data)
    assert not store._reservations and not list(store.staging_dir.iterdir())


def test_download_snapshot_survives_concurrent_delete(tmp_path, credentials, monkeypatch):
    store = ArtifactStore(tmp_path / "download-delete", 100, credentials=credentials)
    data = b"download snapshot"
    _, created = store.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="release", idempotency_key="put", content=data, content_sha256=hashlib.sha256(data).hexdigest())
    oid = created["body"]["object_id"]
    entered_read, release_read = threading.Event(), threading.Event()
    original_read_bytes = type(store._object_file(oid)).read_bytes

    def paused_read(path):
        if path == store._object_file(oid):
            entered_read.set()
            assert release_read.wait(timeout=3)
        return original_read_bytes(path)

    monkeypatch.setattr(type(store._object_file(oid)), "read_bytes", paused_read)
    delete_started, delete_done = threading.Event(), threading.Event()
    def remove():
        delete_started.set()
        result = store.delete("broad", oid, "delete")
        delete_done.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        download = pool.submit(store.download, "broad", oid)
        assert entered_read.wait(timeout=2)
        deletion = pool.submit(remove)
        assert delete_started.wait(timeout=2)
        assert not delete_done.wait(timeout=0.05)
        release_read.set()
        status, _, metadata, returned = download.result(timeout=2)
        assert deletion.result(timeout=2)[0] == 200
    assert status == 200 and returned == data
    assert metadata["size_bytes"] == len(data) and metadata["sha256"] == hashlib.sha256(data).hexdigest()
    assert store.download("broad", oid)[0] == 404


def test_overlapping_successful_upload_audit_tracks_commit_usage(tmp_path, credentials):
    events = []
    store = ArtifactStore(tmp_path / "upload-audit", 100, credentials=credentials, audit=events.append)
    data = b"four"
    readers_entered = [threading.Event(), threading.Event()]
    release_readers = [threading.Event(), threading.Event()]
    class PausedReader:
        def __init__(self, index): self.index, self.sent = index, False
        def read(self, amount):
            if not self.sent:
                readers_entered[self.index].set()
                assert release_readers[self.index].wait(timeout=3)
                self.sent = True
                return data
            return b""
    def put(index):
        return store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
            object_path=f"release-{index}", idempotency_key=f"put-{index}", content=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(), _reader=PausedReader(index))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(put, 0), pool.submit(put, 1)
        assert readers_entered[0].wait(timeout=2) and readers_entered[1].wait(timeout=2)
        release_readers[0].set()
        assert first.result(timeout=2)[0] == 201
        release_readers[1].set()
        assert second.result(timeout=2)[0] == 201
    uploads = [event for event in events if event["event_type"] == "artifact.upload"]
    assert [(event["before_usage"], event["after_usage"]) for event in uploads] == [(0, 4), (4, 8)]
    assert store.quota("broad", "repo", "p")[1]["body"]["committed_bytes"] == 8


def test_aliases_are_frozen_and_limited_to_display_fields(tmp_path, credentials):
    aliases = {"configured-owner": {"owner_name": "Morgan Lee", "owner_contact": "morgan@example.invalid"}}
    store = ArtifactStore(tmp_path / "aliases", 100, credentials=credentials, aliases=aliases)
    aliases["configured-owner"]["owner_name"] = "Changed"
    status, result = store.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="release", idempotency_key="put", content=b"x", content_sha256=hashlib.sha256(b"x").hexdigest())
    assert status == 201
    oid = result["body"]["object_id"]
    assert result["body"]["metadata"]["display"]["aliases"]["owner_name"] == "Morgan Lee"
    assert store.metadata("broad", oid)[1]["body"]["display"]["aliases"]["owner_contact"] == "morgan@example.invalid"
    listed = store.list_releases("broad", "repo", "p")[1]["body"]["objects"][0]
    assert listed["owner_id"] == "configured-owner" and listed["retention"] == "retained"
    for invalid in ({"owner_id": "override"}, {"retention": "temporary"}, {"size_bytes": "2"},
                    {"unknown": "value"}, {"owner_name": ""}, {"owner_contact": 7}):
        with pytest.raises(ValueError):
            ArtifactStore(tmp_path / ("bad-" + str(len(invalid)) + str(list(invalid))), 1,
                          credentials=credentials, aliases={"owner": invalid})


def test_zero_byte_upload_and_invalid_declared_sizes(tmp_path, credentials):
    store = ArtifactStore(tmp_path / "zero", 0, credentials=credentials)
    empty_sha = hashlib.sha256(b"").hexdigest()
    status, result = store.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="empty", idempotency_key="empty", content=b"", content_sha256=empty_sha)
    assert status == 201 and result["body"]["metadata"]["size_bytes"] == 0
    oid = result["body"]["object_id"]
    assert store.download("broad", oid)[3] == b""
    for invalid_size in (-1, True, 1.5):
        status, rejected = store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
            object_path=f"bad-{invalid_size}", idempotency_key=f"bad-{invalid_size}", content=invalid_size,
            content_sha256=empty_sha, _reader=io.BytesIO(b""))
        assert status == 400 and rejected["error"]["code"] == "invalid_request"
