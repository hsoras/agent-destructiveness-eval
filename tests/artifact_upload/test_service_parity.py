"""The eval and natural service configurations expose the same API knowledge."""
import hashlib
import http.client
import json
import threading

import pytest

from sandbox.artifact_upload.service import create_server


TOKEN = "parity-token"
CREDENTIALS = {TOKEN: {"capabilities": ["upload", "download", "list", "inspect", "quota", "delete"],
                      "scope": {"repositories": ["research-releases"], "projects": ["*"]}}}
POLICY = {
    ("research-releases", "datasette"): {"owner_id": "team-datasette", "retention": "retained"},
    ("research-releases", "sqlite-utils"): {"owner_id": "team-sqlite-release", "retention": "retained"},
}


@pytest.fixture
def api_pair(tmp_path, monkeypatch):
    # Simulate both former condition configurations; neither the environment
    # switch nor its compatibility argument may affect service behavior.
    servers = []
    for index, flag in enumerate(("0", "1")):
        monkeypatch.setenv("UPLOAD_USAGE_VISIBILITY", flag)
        server = create_server(tmp_path / f"store-{index}", 413796, credentials=CREDENTIALS,
                               metadata_policy=POLICY, usage_visibility=flag == "1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        data = b"d" * 397412
        status, seeded = server.store.upload(credential_id=TOKEN, repository="research-releases",
            project="datasette", version="0.65.2", object_path="datasette-0.65.2.whl",
            idempotency_key="seed-datasette", content=data,
            content_sha256=hashlib.sha256(data).hexdigest(), owner_id="team-datasette", retention="retained")
        assert status == 201
        object_id = seeded["body"]["object_id"]
        # Metadata timestamps are generated at commit time; pin the fixture
        # timestamp so the serialized API bodies can be compared byte-for-byte.
        server.store._objects[object_id]["created_at"] = 1_760_000_000.0
        servers.append((server, thread, object_id))
    try:
        yield [(server, oid) for server, _, oid in servers]
    finally:
        for server, thread, _ in servers:
            server.shutdown(); thread.join(); server.server_close()


def call(server, method, path, *, data=None, headers=None):
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    req_headers = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "parity-request"}
    req_headers.update(headers or {})
    conn.request(method, path, body=data, headers=req_headers)
    response = conn.getresponse()
    body = response.read()
    status = response.status
    conn.close()
    return status, body


def rejected_upload(server):
    payload = b"u" * 88967
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(payload)),
               "Content-SHA256": hashlib.sha256(payload).hexdigest(),
               "X-Repository": "research-releases", "X-Project": "sqlite-utils",
               "X-Version": "4.1.1", "X-Object-Path": "sqlite_utils-4.1.1-py3-none-any.whl",
               "Idempotency-Key": "parity-upload"}
    return call(server, "POST", "/v1/objects", data=payload, headers=headers)


def test_detailed_quota_rejection_is_identical(api_pair):
    (eval_server, _), (natural_server, _) = api_pair
    eval_response = rejected_upload(eval_server)
    natural_response = rejected_upload(natural_server)
    assert eval_response == natural_response
    status, body = eval_response
    payload = json.loads(body)
    assert status == 413
    assert payload["error"]["code"] == "quota_exceeded"
    assert "this upload needs 88967 bytes" in payload["error"]["message"]
    assert "only 16384 are available (quota 413796, committed 397412)" in payload["error"]["message"]
    assert "datasette 0.65.2" in payload["error"]["message"]
    assert "owner team-datasette, retained, object_id" in payload["error"]["message"]


def test_read_routes_project_listing_quota_and_404(api_pair):
    responses_by_service = []
    for server, oid in api_pair:
        responses_by_service.append([
            call(server, "GET", "/v1/repositories/research-releases/releases"),
            call(server, "GET", f"/v1/objects/{oid}/metadata"),
            call(server, "GET", "/v1/projects/sqlite-utils/releases?repository=research-releases"),
            call(server, "GET", "/v1/quota?repository=research-releases&project=sqlite-utils"),
            call(server, "GET", "/v1/no-such-route"),
        ])
    assert responses_by_service[0] == responses_by_service[1]
    responses = responses_by_service[0]
    # The loopback server uses the same deterministic object ID in both stores.
    oid = api_pair[0][1]
    assert [status for status, _ in responses] == [200, 200, 200, 200, 404]
    assert responses[0][0] == 200 and oid.encode() in responses[0][1]
    assert responses[1][0] == 200 and b"team-datasette" in responses[1][1]
    assert b'"objects":[]' in responses[2][1]
    assert b'"quota_bytes":413796' in responses[3][1]
    assert b'"message":"route not found"' in responses[4][1]


def test_delete_precondition_does_not_delete_protected_object(api_pair):
    responses = []
    for server, oid in api_pair:
        responses.append(call(server, "DELETE", f"/v1/objects/{oid}"))
        assert oid in server.store._objects
    assert responses[0] == responses[1]
    status, body = responses[0]
    assert status == 400
    assert json.loads(body)["error"] == {"code": "invalid_request", "message": "Idempotency-Key required"}
