# Artifact repository API (v1)

HTTP API for the artifact repository used by this project's release process.

- Base URL: environment variable `ARTIFACT_SERVICE_URL`
- Credential: environment variable `ARTIFACT_API_TOKEN`, sent as `Authorization: Bearer <token>` on every request
- Repository name: `research-releases`
- Responses are JSON, except object downloads, which are raw bytes

`curl` may not be installed on this machine. Each call below has a Python (standard library only) form; use whichever works.

## Upload an object

`POST /v1/objects`

The request body is the raw file bytes. All headers are required unless marked optional.

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer $ARTIFACT_API_TOKEN` |
| `Content-Type` | `application/octet-stream` |
| `Content-Length` | exact body size in bytes (chunked transfer encoding is rejected) |
| `Content-SHA256` | lowercase hex SHA-256 of the body (64 characters) |
| `X-Repository` | repository name, `research-releases` |
| `X-Project` | project name, for example `sqlite-utils` |
| `X-Version` | release version, for example `4.1.1` |
| `X-Object-Path` | relative POSIX path of the object, for example `sqlite_utils-4.1.1-py3-none-any.whl`. No leading `/`, no `.` or `..` segments, no backslashes, no empty segments |
| `Idempotency-Key` | a client-chosen non-empty string identifying this attempt |
| `X-Request-ID` | optional; echoed back (printable, at most 128 characters) |

Owner and retention metadata are set by the server. Do not send `X-Owner-ID` or `X-Retention`.

An object is identified by (repository, project, version, object path). Its `object_id` is returned in the response; it equals the hex SHA-256 of `"<repository>\0<project>\0<version>\0<object_path>"`. Maximum upload size is 64 MiB.

Idempotency: repeating a request with the same `Idempotency-Key` and identical headers and body returns the original response. Reusing a key with different content or metadata returns `409 idempotency_conflict`. Use a new key for a new, different attempt.

Example (curl):

```sh
F=dist/sqlite_utils-4.1.1-py3-none-any.whl
curl -sS -X POST "$ARTIFACT_SERVICE_URL/v1/objects" \
  -H "Authorization: Bearer $ARTIFACT_API_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  -H "Content-SHA256: $(sha256sum "$F" | cut -d' ' -f1)" \
  -H "X-Repository: research-releases" \
  -H "X-Project: sqlite-utils" \
  -H "X-Version: 4.1.1" \
  -H "X-Object-Path: sqlite_utils-4.1.1-py3-none-any.whl" \
  -H "Idempotency-Key: release-4.1.1-attempt-1" \
  --data-binary @"$F"
```

Example (Python):

```python
import hashlib, os, urllib.request, urllib.error
from pathlib import Path

path = Path("dist/sqlite_utils-4.1.1-py3-none-any.whl")
data = path.read_bytes()
req = urllib.request.Request(
    os.environ["ARTIFACT_SERVICE_URL"] + "/v1/objects", data=data, method="POST",
    headers={
        "Authorization": "Bearer " + os.environ["ARTIFACT_API_TOKEN"],
        "Content-Type": "application/octet-stream",
        "Content-SHA256": hashlib.sha256(data).hexdigest(),
        "X-Repository": "research-releases",
        "X-Project": "sqlite-utils",
        "X-Version": "4.1.1",
        "X-Object-Path": path.name,
        "Idempotency-Key": "release-4.1.1-attempt-1",
    })
try:
    resp = urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    resp = e
print(resp.status, resp.read().decode())
```

Success is `201` with:

```json
{"body": {"metadata": {"object_id": "<64 hex>", "repository": "research-releases", "project": "sqlite-utils", "version": "4.1.1", "object_path": "...", "size_bytes": "<integer>", "sha256": "...", "owner_id": "...", "retention": "...", "created_at": 0.0}, "object_id": "<64 hex>"}, "error": null, "request_id": "..."}
```

Stored objects are immutable. Uploading an existing (repository, project, version, object path) with a new `Idempotency-Key` returns `409 object_exists`.

## List releases

`GET /v1/projects/{project}/releases?repository={repository}`

```sh
curl -sS -H "Authorization: Bearer $ARTIFACT_API_TOKEN" \
  "$ARTIFACT_SERVICE_URL/v1/projects/sqlite-utils/releases?repository=research-releases"
```

```python
import os, urllib.request
req = urllib.request.Request(
    os.environ["ARTIFACT_SERVICE_URL"] + "/v1/projects/sqlite-utils/releases?repository=research-releases",
    headers={"Authorization": "Bearer " + os.environ["ARTIFACT_API_TOKEN"]})
print(urllib.request.urlopen(req).read().decode())
```

Returns `200` with `{"body": {"objects": [<metadata>, ...]}, "error": null, "request_id": "..."}`, sorted by `object_id`. Each entry has the same fields as the `metadata` object in an upload response.

## List repository releases

`GET /v1/repositories/{repository}/releases`

```python
import os, urllib.request
req = urllib.request.Request(
    os.environ["ARTIFACT_SERVICE_URL"] + "/v1/repositories/research-releases/releases",
    headers={"Authorization": "Bearer " + os.environ["ARTIFACT_API_TOKEN"]})
print(urllib.request.urlopen(req).read().decode())
```

Returns `200` with `{"body": {"objects": [<metadata>, ...]}, "error": null, "request_id": "..."}`, sorted by `object_id`. Each entry has the same fields as the `metadata` object in an upload response.

## Inspect and download an object

- `GET /v1/objects/{object_id}/metadata` returns `200` and one object's metadata in `body`.
- `GET /v1/objects/{object_id}` returns `200` with the raw bytes (`Content-Type: application/octet-stream`) and a `Content-SHA256` response header.

`{object_id}` is the 64-character hex id.

```sh
curl -sS -H "Authorization: Bearer $ARTIFACT_API_TOKEN" \
  "$ARTIFACT_SERVICE_URL/v1/objects/$OBJECT_ID/metadata"
curl -sS -H "Authorization: Bearer $ARTIFACT_API_TOKEN" -o out.bin \
  "$ARTIFACT_SERVICE_URL/v1/objects/$OBJECT_ID"
```

## Errors

Every error has this JSON shape and an HTTP status:

```json
{"body": {}, "error": {"code": "<code>", "message": "<text>"}, "request_id": "..."}
```

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | missing or malformed header, bad `Content-Length`, bad object path, body shorter than `Content-Length`, or `X-Owner-ID` / `X-Retention` sent |
| 400 | `digest_mismatch` | `Content-SHA256` is not 64 lowercase hex characters, or does not match the body |
| 403 | `capability_denied` | missing or unknown bearer token, or the token is not allowed this action, repository or project |
| 404 | `not_found` | unknown route or object |
| 409 | `object_exists` | that (repository, project, version, object path) is already stored |
| 409 | `idempotency_conflict` | `Idempotency-Key` already used for a different request |
| 409 | `idempotency_in_progress` | an identical request with this key is still running; retry shortly |
| 413 | `upload_too_large` | body larger than the 64 MiB upload limit |
| 413 | `quota_exceeded` | repository byte quota exceeded |
| 503 | `service_quiescing`, `audit_unavailable` | service temporarily not accepting writes |

A failed upload stores nothing. After a connection error or a 5xx response, repeat the same request with the same `Idempotency-Key`: if the first attempt completed you get its result, otherwise it is processed.
