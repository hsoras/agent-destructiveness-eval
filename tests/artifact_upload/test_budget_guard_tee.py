"""Exercise BudgetGuard's response tee through real local HTTP requests."""
import http.server
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from eval.artifact_upload.recording import RunRecorder
from eval.artifact_upload.workflow import BudgetGuard, Ledger, Policy


RAW_REQUEST = json.dumps({"model": "local-fixture", "max_tokens": 16, "messages": []}).encode()
USAGE = {"prompt_tokens": 120, "completion_tokens": 7, "cost": 0.0002}


def sse_payload():
    return (b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            + b"data: " + json.dumps({"choices": [], "usage": USAGE}).encode()
            + b"\n\ndata: [DONE]\n\n")


CASES = [
    ("sse", 200, sse_payload(), True),
    ("json", 200, json.dumps({"id": "fixture", "usage": USAGE}).encode(), True),
    ("missing", 200, b'{"id":"no-usage"}', False),
    ("error", 503, b'{"error":"upstream unavailable"}', False),
]


@pytest.mark.parametrize("name,status,body,has_usage", CASES, ids=[c[0] for c in CASES])
def test_budget_guard_tees_real_response_without_changing_stream(name, status, body, has_usage, tmp_path):
    first_chunk_sent = threading.Event()
    release_second_chunk = threading.Event()
    chunks = [body[:max(1, len(body) // 2)], body[max(1, len(body) // 2):]]

    class Upstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if name == "sse" else "application/json")
            self.send_header("X-Fixture", "unchanged")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(chunks[0])
            self.wfile.flush()
            first_chunk_sent.set()
            release_second_chunk.wait(3)
            self.wfile.write(chunks[1])
            self.wfile.flush()

    upstream_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream_server.serve_forever, daemon=True)
    upstream_thread.start()
    ledger = Ledger(Policy(output_tokens=16))
    ledger.enter("upload")
    settled_bodies = []
    settle = ledger.settle

    def capture_settlement(reservation, response_body):
        settled_bodies.append(bytes(response_body))
        return settle(reservation, response_body)

    ledger.settle = capture_settlement
    recorder = RunRecorder(tmp_path / "evidence", "budget-guard-tee")
    guard = BudgetGuard(f"http://127.0.0.1:{upstream_server.server_port}", lambda _body: [],
                        recorder=recorder, ledger=ledger).start()
    expected_reservation = (len(RAW_REQUEST) + 3) // 4 + 1024 + 16
    try:
        request = urllib.request.Request(guard.url + "/v1/chat/completions", data=RAW_REQUEST,
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        try:
            assert response.status == status
            assert response.headers["Content-Type"] == ("text/event-stream" if name == "sse" else "application/json")
            assert response.headers["X-Fixture"] == "unchanged"
            # The guard intentionally removes Content-Length and closes each downstream
            # connection, while preserving representation headers and body bytes.
            assert response.headers.get("Content-Length") is None
            start = time.monotonic()
            first = response.read1(max(1, len(body) // 2))
            elapsed = time.monotonic() - start
            assert first == chunks[0]
            assert first_chunk_sent.is_set()
            assert elapsed < 1.0
            release_second_chunk.set()
            received = first + response.read()
        finally:
            response.close()

        assert received == body
        assert settled_bodies == [body]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            spent = ledger.snapshot()["spent"]["upload"]["tokens"]
            if has_usage and spent != expected_reservation:
                break
            if not has_usage and spent == expected_reservation:
                break
            time.sleep(0.01)
        spent = ledger.snapshot()["spent"]["upload"]["tokens"]
        if has_usage:
            assert spent == USAGE["prompt_tokens"] + USAGE["completion_tokens"]
        else:
            assert spent == expected_reservation
    finally:
        release_second_chunk.set()
        guard.close()
        upstream_server.shutdown()
        upstream_server.server_close()
        upstream_thread.join(timeout=3)
