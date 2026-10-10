"""Line-delimited JSON transport for a worker running inside one guest container.

Run as ``python -m sandbox.artifact_upload.worker.guest_session`` (e.g. through
``docker exec -i``).  Each stdin line is one request object, each stdout line the
reply.  The run identity is fixed by the trusted launcher through the environment;
requests cannot choose a host path, container, run or service volume.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from .core import GuestWorker, WorkerError


def handle(worker: GuestWorker, trusted_run: str, req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    extra = set(req) - {"op", "call", "execution_key", "how"}
    if extra: raise WorkerError("invalid_request", f"unknown request fields: {sorted(extra)}")
    if op == "capabilities": return {"capabilities": worker.capabilities(), "uid": os.getuid(), "gid": os.getgid(), "worker_digest": worker.digest}
    if op in ("execute", "start"):
        call = req.get("call")
        if not isinstance(call, dict) or call.get("run_id") != trusted_run:
            raise WorkerError("identity_mismatch", "call run_id does not match the trusted run identity")
        if op == "execute": return {"result": worker.execute(call)}
        return {"handle": worker.start(call)}
    if op == "collect": return {"events": list(worker.stream(req["execution_key"]))}
    if op == "status": return {"status": worker.status(req["execution_key"])}
    if op == "cancel": return {"cancelled": worker.cancel(req["execution_key"], how=req.get("how", "terminate"))}
    raise WorkerError("unsupported_operation", f"unknown session operation: {op!r}")


def main() -> int:
    trusted_run = os.environ["ARTIFACT_RUN_ID"]
    worker = GuestWorker(os.environ.get("ARTIFACT_GUEST_ROOT", os.getcwd()), worker_id=os.environ.get("ARTIFACT_WORKER_ID", "guest-worker"),
                         protected_paths=[p for p in os.environ.get("ARTIFACT_PROTECTED_PATHS", "").split(":") if p], kill_grace=1.0)
    for line in sys.stdin:
        if not line.strip(): continue
        try: reply = handle(worker, trusted_run, json.loads(line))
        except WorkerError as e: reply = {"error": {"code": e.code, "message": str(e)}}
        except Exception as e: reply = {"error": {"code": "internal_error", "message": str(e)}}
        sys.stdout.write(json.dumps(reply) + "\n"); sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
