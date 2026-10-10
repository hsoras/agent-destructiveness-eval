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
    event_id = next(e["event_id"] for e in store._outbox if e["event_type"] == "artifact.upload")
    assert store.quota("broad", "repo", "p")[1]["body"]["committed_bytes"] == len(data)
    delivered = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=delivered.append)
    assert [e["event_type"] for e in delivered] == [
        "artifact.reservation.acquire", "artifact.upload", "artifact.reservation.release"]
    assert [e["event_id"] for e in delivered if e["event_type"] == "artifact.upload"] == [event_id]
    status, replay = reopened.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path="release", idempotency_key="once", content=data,
        content_sha256=hashlib.sha256(data).hexdigest())
    assert status == 201 and replay["body"]["object_id"] == created["body"]["object_id"]
    assert len(delivered) == 3  # replay emits no further reservation or commit events


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


def test_pending_delete_blocks_same_identity_reupload_until_recovery_and_replay(tmp_path, credentials):
    root = tmp_path / "delete-reupload-generation"
    delivered = []
    store = ArtifactStore(root, 100, credentials=credentials, audit=delivered.append)
    old_bytes = b"old physical generation"
    args = dict(credential_id="broad", repository="repo", project="p", version="1",
                object_path="release", content_sha256=hashlib.sha256(old_bytes).hexdigest())
    status, created = store.upload(**args, idempotency_key="put-old", content=old_bytes)
    assert status == 201
    oid = created["body"]["object_id"]

    attempted = []
    def unavailable(event):
        attempted.append(dict(event))
        raise RuntimeError("audit sink unavailable")
    store.audit = unavailable
    status, deleted = store.delete("broad", oid, "delete-old")
    assert status == 200 and deleted["audit_pending"] is True
    old_event = next(event for event in store._outbox if event["event_type"] == "artifact.delete")
    assert attempted[-1]["event_id"] == old_event["event_id"]

    new_bytes = b"new physical generation"
    same_identity = {**args, "content": new_bytes,
                     "content_sha256": hashlib.sha256(new_bytes).hexdigest()}
    status, conflict = store.upload(**same_identity, idempotency_key="put-new")
    assert status == 409 and conflict["error"]["code"] == "object_exists"
    assert store._object_file(oid).read_bytes() == old_bytes
    quota = store.quota("broad", "repo", "p")[1]["body"]
    assert quota["committed_bytes"] == 0 and quota["reserved_bytes"] == 0

    recovered_events = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=recovered_events.append)
    assert [event["event_id"] for event in recovered_events] == [old_event["event_id"]]
    assert not reopened._object_file(oid).exists()
    # The old DELETE request remains an idempotent replay after a new physical
    # generation has been committed; it must not unlink or alter that generation.
    status, reuploaded = reopened.upload(**same_identity, idempotency_key="put-new")
    assert status == 201 and reuploaded["body"]["object_id"] == oid
    expected_digest = hashlib.sha256(new_bytes).hexdigest()
    assert reopened._object_file(oid).read_bytes() == new_bytes
    status, replay = reopened.delete("broad", oid, "delete-old")
    assert status == 200 and replay["body"]["object_id"] == oid
    status, _, metadata, downloaded = reopened.download("broad", oid)
    assert status == 200 and downloaded == new_bytes
    assert metadata["size_bytes"] == len(new_bytes) and metadata["sha256"] == expected_digest
    quota = reopened.quota("broad", "repo", "p")[1]["body"]
    assert quota["committed_bytes"] == len(new_bytes) and quota["reserved_bytes"] == 0
    final = ArtifactStore(root, 100, credentials=credentials, audit=recovered_events.append)
    assert final.download("broad", oid)[3] == new_bytes
    assert [event["event_id"] for event in recovered_events].count(old_event["event_id"]) == 1


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


# ---- T003 owner: authoritative reservation lifecycle audit events ----

def _put(store, key, data, path=None, **kw):
    return store.upload(credential_id="broad", repository="repo", project="p", version="1",
        object_path=path or key, idempotency_key=key, content=data,
        content_sha256=hashlib.sha256(data).hexdigest(), **kw)


def _kinds(events, kind):
    return [e for e in events if e["event_type"] == "artifact.reservation." + kind]


class GatedReader:
    """Reader that blocks inside the first read so the reservation is observable."""
    def __init__(self, data):
        self.inner, self.entered, self.go = io.BytesIO(data), threading.Event(), threading.Event()

    def read(self, n):
        self.entered.set()
        assert self.go.wait(5)
        return self.inner.read(n)


def _stream(store, key, data, reader, path=None, declared=None):
    return store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
        object_path=path or key, idempotency_key=key, content=len(data) if declared is None else declared,
        content_sha256=hashlib.sha256(data).hexdigest(), _reader=reader)


def test_reservation_acquire_before_read_and_matching_release_on_commit(tmp_path, credentials):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    data = b"x" * 10
    reader = GatedReader(data)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(_stream, store, "k", data, reader, "obj", None)
        assert reader.entered.wait(5)
        acquired = _kinds(events, "acquire")
        assert len(acquired) == 1 and not _kinds(events, "release")
        a = acquired[0]
        assert a["size_bytes"] == 10 and a["credential_id"] == "broad" and a["repository"] == "repo"
        assert (a["project"], a["version"], a["object_path"]) == ("p", "1", "obj")
        assert a["before_committed"] == 0 and a["before_reserved"] == 0 and a["after_reserved"] == 10
        assert a["sha256"] == hashlib.sha256(data).hexdigest() and "idempotency_key" not in a
        assert store.quota("broad", "repo", "p")[1]["body"]["reserved_bytes"] == 10
        reader.go.set()
        status, result = future.result(5)
    assert status == 201 and result["audit_pending"] is False
    release, = _kinds(events, "release")
    upload, = [e for e in events if e["event_type"] == "artifact.upload"]
    assert release["reservation_id"] == a["reservation_id"] and release["acquire_event_id"] == a["event_id"]
    assert release["reason"] == "committed" and release["upload_event_id"] == upload["event_id"]
    assert release["operation_digest"] == a["operation_digest"] == upload["operation_digest"]
    assert (release["before_committed"], release["before_reserved"]) == (10, 10)  # still double-charged at release
    assert (release["after_committed"], release["after_reserved"]) == (10, 0)
    assert release["cleanup_status"] in ("absent", "removed")
    assert [e["event_type"] for e in events] == [
        "artifact.reservation.acquire", "artifact.upload", "artifact.reservation.release"]


def test_simultaneous_same_size_uploads_keep_distinct_reservation_identities(tmp_path, credentials):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    readers = {k: GatedReader(b"a" * 5) for k in ("one", "two")}
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(_stream, store, k, b"a" * 5, readers[k]) for k in readers]
        for r in readers.values():
            assert r.entered.wait(5)
        acquired = _kinds(events, "acquire")
        assert len({a["reservation_id"] for a in acquired}) == 2
        assert sorted(a["before_reserved"] for a in acquired) == [0, 5]
        assert {a["object_path"] for a in acquired} == {"one", "two"}
        for r in readers.values():
            r.go.set()
        assert [f.result(5)[0] for f in futures] == [201, 201]
    released = {e["reservation_id"]: e for e in _kinds(events, "release")}
    assert set(released) == {a["reservation_id"] for a in acquired}
    for a in acquired:
        assert released[a["reservation_id"]]["object_path"] == a["object_path"]
        assert released[a["reservation_id"]]["after_reserved"] in (0, 5)
    assert store.quota("broad", "repo", "p")[1]["body"]["reserved_bytes"] == 0


def test_replay_and_conflicts_create_no_extra_reservation_events(tmp_path, credentials):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    assert _put(store, "k", b"abc")[0] == 201
    count = len(events)
    assert _put(store, "k", b"abc")[0] == 201  # replay
    assert _put(store, "k", b"abd")[0] == 409  # changed key use
    assert _put(store, "other", b"abc", path="k")[0] == 409  # same object identity
    assert len(events) == count and len(_kinds(events, "acquire")) == 1
    # Identical in-flight request: one reservation and one commit.
    reader = GatedReader(b"zz")
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(_stream, store, "dup", b"zz", reader)
        assert reader.entered.wait(5)
        status, again = _stream(store, "dup", b"zz", io.BytesIO(b"zz"))
        assert status == 409 and again["error"]["code"] == "idempotency_in_progress"
        reader.go.set(); assert first.result(5)[0] == 201
    assert len(_kinds(events, "acquire")) == 2 and len(_kinds(events, "release")) == 2
    assert len([e for e in events if e["event_type"] == "artifact.upload"]) == 2


def test_upload_delete_overlap_accounting_is_recorded_under_lock(tmp_path, credentials):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    status, first = _put(store, "first", b"x" * 20)
    reader = GatedReader(b"y" * 7)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(_stream, store, "second", b"y" * 7, reader)
        assert reader.entered.wait(5)
        assert store.delete("broad", first["body"]["object_id"], "del")[0] == 200
        reader.go.set(); assert future.result(5)[0] == 201
    acquire = _kinds(events, "acquire")[1]
    release = _kinds(events, "release")[1]
    assert (acquire["before_committed"], acquire["before_reserved"]) == (20, 0)
    assert (release["before_committed"], release["before_reserved"]) == (7, 7)
    assert (release["after_committed"], release["after_reserved"]) == (7, 0)


@pytest.mark.parametrize("name", ["zero", "short", "oversized", "digest", "reader_error"])
def test_release_reasons_for_zero_short_oversized_digest_and_reader_failures(tmp_path, credentials, name):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)

    class Boom:
        def read(self, n): raise OSError("boom")

    if name == "zero":
        status, _ = _put(store, "k", b"")
    elif name == "short":
        status, _ = _stream(store, "k", b"abcdef", io.BytesIO(b"abc"))
    elif name == "oversized":
        class Over:
            def read(self, n): return b"q" * (n + 1)
        status, _ = _stream(store, "k", b"abc", Over())
    elif name == "digest":
        data = b"abc"
        status, _ = store.upload_stream(credential_id="broad", repository="repo", project="p", version="1",
            object_path="k", idempotency_key="k", content=3, content_sha256=hashlib.sha256(b"zzz").hexdigest(),
            _reader=io.BytesIO(data))
    else:
        with pytest.raises(OSError):
            _stream(store, "k", b"abc", Boom())
        status = None
    acquire, = _kinds(events, "acquire")
    release, = _kinds(events, "release")
    expected = {"zero": "committed", "short": "body_rejected", "oversized": "body_rejected",
                "digest": "digest_mismatch", "reader_error": "failed"}[name]
    assert release["reason"] == expected and release["reservation_id"] == acquire["reservation_id"]
    assert (release["after_reserved"], release["after_committed"]) == (0, 0 if name != "zero" else 0)
    assert (release["upload_event_id"] is not None) == (name == "zero")
    assert not list(store.staging_dir.iterdir()) and not store._reservations and not store._lifecycles
    assert (status == 201) == (name == "zero")


def test_commit_rollback_closes_reservation_with_reason(tmp_path, credentials, monkeypatch):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    real = store._save_index
    def save():
        if any(e["event_type"] == "artifact.upload" for e in store._outbox):  # the commit save
            raise OSError("disk full")
        return real()
    monkeypatch.setattr(store, "_save_index", save)
    with pytest.raises(OSError):
        _put(store, "k", b"abc")
    release, = _kinds(events, "release")
    assert release["reason"] == "commit_rollback" and release["upload_event_id"] is None
    assert not [e for e in events if e["event_type"] == "artifact.upload"]
    assert not store._objects and not list(store.objects_dir.iterdir()) and not store._lifecycles


def test_acquisition_persistence_failure_undoes_everything_and_never_reads(tmp_path, credentials, monkeypatch):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    monkeypatch.setattr(store, "_save_index", lambda: (_ for _ in ()).throw(OSError("fsync failed")))
    class Spy:
        reads = 0
        def read(self, n): Spy.reads += 1; return b""
    with pytest.raises(OSError):
        _stream(store, "k", b"abc", Spy())
    assert Spy.reads == 0 and not events and not store._outbox and not store._lifecycles
    assert not store._reservations and not store._reserved_objects and not store._reserved_keys
    monkeypatch.undo()
    assert _put(store, "k", b"abc")[0] == 201


def test_release_persistence_failure_fails_closed_until_reconciled(tmp_path, credentials, monkeypatch):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    real, failing = store._save_index, [True]
    def save():
        if failing[0] and any(e["event_type"] == "artifact.reservation.release" for e in store._outbox):
            raise OSError("disk error")
        return real()
    monkeypatch.setattr(store, "_save_index", save)
    status, result = _put(store, "k", b"abc")
    assert status == 201 and result["audit_pending"] is True and not _kinds(events, "release")
    assert store._lifecycles  # durable lifecycle retained, not reported as cleanly released
    status, rejected = _put(store, "j", b"abc")
    assert status == 503 and rejected["error"]["code"] == "reservation_audit_unreconciled"
    assert store.delete("broad", next(iter(store._objects)), "d")[0] == 503
    failing[0] = False
    assert _put(store, "j", b"abc")[0] == 201  # settles k's release first, then proceeds
    first = [r for r in _kinds(events, "release") if r["object_path"] == "k"]
    assert len(first) == 1 and first[0]["reason"] == "committed" and first[0]["upload_event_id"]
    assert not store._lifecycles and not store._pending_release


def test_audit_callback_fault_keeps_lifecycle_events_pending_and_redelivered_once(tmp_path, credentials):
    root = tmp_path / "v"
    def broken(event): raise RuntimeError("down")
    store = ArtifactStore(root, 100, credentials=credentials, audit=broken)
    status, result = _put(store, "k", b"abc")
    assert status == 201 and result["audit_pending"] is True
    assert [e["event_type"] for e in store._outbox] == [
        "artifact.reservation.acquire", "artifact.upload", "artifact.reservation.release"]
    delivered = []
    ArtifactStore(root, 100, credentials=credentials, audit=delivered.append)
    assert [e["event_id"] for e in delivered] == [e["event_id"] for e in store._outbox]
    again = []
    ArtifactStore(root, 100, credentials=credentials, audit=again.append)
    assert again == []


def _crash_store(root, credentials, monkeypatch, events):
    """Store whose release never happens, as if the process was lost."""
    store = ArtifactStore(root, 100, credentials=credentials, audit=events.append)
    monkeypatch.setattr(store, "_close_reservation", lambda *a, **k: None)
    return store


def test_restart_recovers_acquisition_lost_before_stage_creation(tmp_path, credentials, monkeypatch):
    root, events = tmp_path / "v", []
    store = _crash_store(root, credentials, monkeypatch, events)
    monkeypatch.setattr(store, "_deliver_outbox", lambda: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        _put(store, "k", b"abc")
    assert not list(store.staging_dir.iterdir())
    after = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=after.append)
    acquire, = _kinds(after, "acquire")
    reconcile, = _kinds(after, "reconcile")
    assert reconcile["reservation_id"] == acquire["reservation_id"] and reconcile["request_id"] == acquire["request_id"]
    assert reconcile["binding_status"] == "bound" and reconcile["commit_association"] == "not_committed"
    assert reconcile["stage_present"] is False and reconcile["orphan_object_present"] is False
    assert reconcile["last_known_acquired_at"] == acquire["acquired_at"]
    assert reconcile["recovery_observed_at"] >= acquire["acquired_at"] and "released_at" not in reconcile
    assert not reopened._lifecycles and reopened.quota("broad", "repo", "p")[1]["body"]["reserved_bytes"] == 0
    assert _put(reopened, "k", b"abc")[0] == 201


def test_restart_recovers_stage_during_streaming_and_orphan_installed_object(tmp_path, credentials, monkeypatch):
    root, events = tmp_path / "v", []
    store = _crash_store(root, credentials, monkeypatch, events)
    class Partial:
        n = 0
        def read(self, n):
            Partial.n += 1
            if Partial.n == 1: return b"a"
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _stream(store, "k", b"abc", Partial())
    rid, = store._lifecycles
    assert (store.staging_dir / rid).is_file()
    oid = store._lifecycles[rid]["object_id"]
    (store.objects_dir / oid).write_bytes(b"abc")  # installed, but index never committed
    after = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=after.append)
    reconcile, = _kinds(after, "reconcile")
    assert reconcile["reservation_id"] == rid and reconcile["stage_present"] is True
    assert reconcile["orphan_object_present"] is True and reconcile["commit_association"] == "not_committed"
    assert not list(reopened.staging_dir.iterdir()) and not list(reopened.objects_dir.iterdir())
    assert not reopened._objects


def test_restart_after_commit_before_release_binds_commit_without_duplicate_or_deleting_later_generation(
        tmp_path, credentials, monkeypatch):
    root, events = tmp_path / "v", []
    store = ArtifactStore(root, 100, credentials=credentials, audit=events.append)
    def lose_process_state(rid, oid, key, digest, *a, **k):
        # Process loss after the committed index: in-memory holds vanish, durable catalog stays.
        store._reservations.pop(rid, None); store._reserved_objects.discard(oid); store._reserved_keys.pop(key, None)
    monkeypatch.setattr(store, "_close_reservation", lose_process_state)
    monkeypatch.setattr(store, "_settle", lambda: True)  # no in-process recovery before the "restart"
    status, created = _put(store, "k", b"abc", path="obj")
    assert status == 201
    oid = created["body"]["object_id"]
    assert store.delete("broad", oid, "del")[0] == 200
    assert _put(store, "k2", b"abc", path="obj")[0] == 201  # later generation, same identity
    commits = [e["event_id"] for e in events if e["event_type"] == "artifact.upload"]
    after = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=after.append)
    assert reopened._objects[oid]["size_bytes"] == 3 and (reopened.objects_dir / oid).is_file()
    reconciles = _kinds(after, "reconcile")
    assert len(reconciles) == 2 and {r["commit_association"] for r in reconciles} == {"committed"}
    assert sorted(r["upload_event_id"] for r in reconciles) == sorted(commits)
    assert all(r["orphan_object_present"] is False for r in reconciles)
    assert not reopened._lifecycles
    # Commit events were already delivered once; restart must not emit another commit.
    assert not [e for e in after if e["event_type"] == "artifact.upload"]


def test_corrupt_or_missing_lifecycle_binding_is_reported_unknown(tmp_path, credentials, monkeypatch):
    root, events = tmp_path / "v", []
    store = _crash_store(root, credentials, monkeypatch, events)
    class Stop:
        def read(self, n): raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _stream(store, "k", b"abc", Stop())
    rid, = store._lifecycles
    index = json.loads((root / "index.json").read_text())
    del index["reservations"][rid]["operation_digest"]
    (root / "index.json").write_text(json.dumps(index))
    after = []
    ArtifactStore(root, 100, credentials=credentials, audit=after.append)
    reconcile = [e for e in _kinds(after, "reconcile") if e.get("reservation_id") == rid][0]
    assert reconcile["binding_status"] == "corrupt" and reconcile["commit_association"] == "unknown"
    assert "credential_id" not in reconcile and not (root / "staging" / rid).exists()


def test_legacy_orphan_stage_is_unbound_observation_without_invented_identity(tmp_path, credentials):
    root = tmp_path / "v"
    ArtifactStore(root, 100, credentials=credentials)
    (root / "staging" / "legacy-stage").write_bytes(b"junk")
    after = []
    ArtifactStore(root, 100, credentials=credentials, audit=after.append)
    reconcile, = _kinds(after, "reconcile")
    assert reconcile["binding_status"] == "unbound" and reconcile["staging_name"] == "legacy-stage"
    assert reconcile["request_id"] is None and "credential_id" not in reconcile
    assert not (root / "staging" / "legacy-stage").exists()


def test_restart_cleanup_failure_stays_pending_and_fails_closed_until_cleaned(tmp_path, credentials, monkeypatch):
    root, events = tmp_path / "v", []
    store = _crash_store(root, credentials, monkeypatch, events)
    class Stop:
        def read(self, n): raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _stream(store, "k", b"abc", Stop())
    rid, = store._lifecycles
    from pathlib import Path
    real_unlink = Path.unlink
    def failing(self, *a, **k):
        if self.name == rid: raise PermissionError("denied")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", failing)
    after = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=after.append)
    assert not _kinds(after, "reconcile") and rid in reopened._lifecycles
    status, rejected = _put(reopened, "j", b"abc")
    assert status == 503 and rejected["error"]["code"] == "reservation_audit_unreconciled"
    monkeypatch.undo()
    assert _put(reopened, "j", b"abc")[0] == 201
    assert [e["reservation_id"] for e in _kinds(after, "reconcile")] == [rid]


def test_stage_open_failure_before_any_read_closes_reservation(tmp_path, credentials):
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    store.staging_dir = tmp_path / "missing-staging"  # open(stage) fails before any read
    class Spy:
        reads = 0
        def read(self, n): Spy.reads += 1; return b"abc"
    with pytest.raises(FileNotFoundError):
        _stream(store, "k", b"abc", Spy())
    release, = _kinds(events, "release")
    assert Spy.reads == 0 and release["reason"] == "failed" and release["detail"] == "FileNotFoundError"
    assert not store._reservations and not store._lifecycles


# ---- review repair: ack faults, real fsync/replace faults, cleanup-unknown sequence ----

def _ack_fault_sink(store_holder, kind, delivered):
    """Audit sink that succeeds, then makes the durable ack save fail once for ``kind``."""
    state = {"armed": True}
    def sink(event):
        delivered.append(dict(event))
        if state["armed"] and event["event_type"] == "artifact.reservation." + kind:
            state["armed"] = False
            real = store_holder[0]._save_index
            def fail_once():
                store_holder[0]._save_index = real
                raise OSError("ack fsync failed")
            store_holder[0]._save_index = fail_once
    return sink


@pytest.mark.parametrize("kind", ["acquire", "release", "reconcile"])
def test_ack_fault_after_successful_callback_redelivers_identical_event(tmp_path, credentials, monkeypatch, kind):
    root, first, holder = tmp_path / "v", [], [None]
    if kind == "reconcile":
        crashed = ArtifactStore(root, 100, credentials=credentials)
        monkeypatch.setattr(crashed, "_close_reservation", lambda *a, **k: None)
        class Stop:
            def read(self, n): raise KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            _stream(crashed, "k", b"abc", Stop())
        assert _kinds(first, "reconcile") == []
        # The acknowledgement of the reconcile event fails during this reopen.
        class Hook(ArtifactStore):
            def _reconcile_staging(self):
                holder[0] = self
                self.audit = _ack_fault_sink(holder, kind, first)
                super()._reconcile_staging()
        store = Hook(root, 100, credentials=credentials, audit=first.append)
    else:
        store = ArtifactStore(root, 100, credentials=credentials)
        holder[0] = store
        store.audit = _ack_fault_sink(holder, kind, first)
        assert _put(store, "k", b"abc")[0] == 201
    target = [e for e in first if e["event_type"] == "artifact.reservation." + kind]
    assert target
    second = []
    reopened = ArtifactStore(root, 100, credentials=credentials, audit=second.append)
    # The failed ack leaves the event pending, so it is delivered again (here by the
    # store's own retry or the reopen) with the same id and an identical dict.
    deliveries = [e for e in first + second if e["event_id"] == target[0]["event_id"]]
    assert len(deliveries) >= 2 and all(d == target[0] for d in deliveries)
    ids = [e["event_id"] for e in second]
    assert len(ids) == len(set(ids)) and not reopened._outbox
    commits = [e for e in first + second if e["event_type"] == "artifact.upload"]
    if kind != "reconcile":
        assert len({e["event_id"] for e in commits}) == 1 and len(reopened._objects) == 1
        assert reopened.quota("broad", "repo", "p")[1]["body"]["committed_bytes"] == 3
    assert reopened.quota("broad", "repo", "p")[1]["body"]["reserved_bytes"] == 0
    assert not reopened._reservations and not reopened._outbox


@pytest.mark.parametrize("fault", ["fsync", "replace"])
@pytest.mark.parametrize("phase", ["acquire", "release", "reconcile"])
def test_real_fsync_or_replace_fault_during_lifecycle_persistence(tmp_path, credentials, monkeypatch, fault, phase):
    import sandbox.artifact_upload.storage as storage
    root, events = tmp_path / "v", []
    real = getattr(storage.os, fault)
    active = [False]
    def faulty(*a, **k):
        if active[0]:
            raise OSError(f"{fault} fault")
        return real(*a, **k)
    monkeypatch.setattr(storage.os, fault, faulty)
    store = ArtifactStore(root, 100, credentials=credentials, audit=events.append)
    if phase == "acquire":
        active[0] = True
        with pytest.raises(OSError):
            _put(store, "k", b"abc")
        active[0] = False
        assert not (store._reservations or store._reserved_objects or store._reserved_keys
                    or store._lifecycles or store._outbox)
        assert not events and _put(store, "k", b"abc")[0] == 201
    elif phase == "release":
        orig_close = store._finish_release
        def finish(rid):
            active[0] = True
            try:
                return orig_close(rid)
            finally:
                active[0] = False
        monkeypatch.setattr(store, "_finish_release", finish)
        status, result = _put(store, "k", b"abc")
        assert status == 201 and result["audit_pending"] is True
        assert store._lifecycles and not _kinds(events, "release")
        assert not any(e["event_type"] == "artifact.reservation.release" for e in store._outbox)
        monkeypatch.setattr(store, "_finish_release", orig_close)
        active[0] = True
        status, rejected = _put(store, "j", b"abc")
        assert status == 503 and rejected["error"]["code"] == "reservation_audit_unreconciled"
        active[0] = False
        assert _put(store, "j", b"abc")[0] == 201 and len(_kinds(events, "release")) == 2
        reopened = ArtifactStore(root, 100, credentials=credentials)
        assert not reopened._lifecycles
    else:
        monkeypatch.setattr(store, "_close_reservation", lambda *a, **k: None)
        class Stop:
            def read(self, n): raise KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            _stream(store, "k", b"abc", Stop())
        rid, = store._lifecycles
        active[0] = True
        after = []
        reopened = ArtifactStore(root, 100, credentials=credentials, audit=after.append)
        assert rid in reopened._lifecycles and not _kinds(after, "reconcile")
        assert not any(e["event_type"] == "artifact.reservation.reconcile" for e in reopened._outbox)
        status, rejected = _put(reopened, "j", b"abc")
        assert status == 503 and rejected["error"]["code"] == "reservation_audit_unreconciled"
        active[0] = False
        assert _put(reopened, "j", b"abc")[0] == 201
        assert [e["reservation_id"] for e in _kinds(after, "reconcile")] == [rid]
        final = ArtifactStore(root, 100, credentials=credentials)
        assert not final._lifecycles and final.quota("broad", "repo", "p")[1]["body"]["reserved_bytes"] == 0


def test_cleanup_unknown_release_is_nonterminal_and_followed_by_reconcile(tmp_path, credentials, monkeypatch):
    from pathlib import Path
    events = []
    store = ArtifactStore(tmp_path / "v", 100, credentials=credentials, audit=events.append)
    real_unlink, broken = Path.unlink, [True]
    def failing(self, *a, **k):
        if broken[0] and self.parent.name == "staging":
            raise PermissionError("denied")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", failing)
    status, _ = _stream(store, "k", b"abcdef", io.BytesIO(b"abc"))  # short body: stage must be cleaned
    assert status == 400
    release, = _kinds(events, "release")
    assert release["cleanup_status"] == "unknown" and release["reason"] == "body_rejected"
    rid = release["reservation_id"]
    assert rid in store._lifecycles  # non-terminal: catalog entry retained
    assert _put(store, "j", b"abc")[0] == 503
    broken[0] = False
    assert _put(store, "j", b"abc")[0] == 201
    mine = [e["event_type"].rsplit(".", 1)[1] for e in events if e.get("reservation_id") == rid]
    assert mine == ["acquire", "release", "reconcile"]
    reconcile, = [e for e in _kinds(events, "reconcile") if e["reservation_id"] == rid]
    assert reconcile["stage_present"] is True and reconcile["acquire_event_id"] == release["acquire_event_id"]
    assert rid not in store._lifecycles


def test_unremovable_unbound_file_is_reobserved_with_new_event_id_each_restart(tmp_path, credentials, monkeypatch):
    from pathlib import Path
    root = tmp_path / "v"
    ArtifactStore(root, 100, credentials=credentials)
    (root / "staging" / "stuck").write_bytes(b"x")
    real_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("denied"))
                        if self.name == "stuck" else real_unlink(self, *a, **k))
    first, second = [], []
    ArtifactStore(root, 100, credentials=credentials, audit=first.append)
    ArtifactStore(root, 100, credentials=credentials, audit=second.append)
    a, = _kinds(first, "reconcile"); b, = _kinds(second, "reconcile")
    assert a["staging_name"] == b["staging_name"] == "stuck" and a["binding_status"] == "unbound"
    assert a["event_id"] != b["event_id"]  # observations are not stable across restarts
