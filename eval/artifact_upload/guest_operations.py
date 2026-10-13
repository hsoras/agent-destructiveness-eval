"""Harness-owned catalog of recoverable guest operations, with durable transactions.

The proposer LLM only emits *structured requests* ``{operation_id, kind, args, preconditions, ...}``; every
request is validated against ``Catalog`` (typed args, writable scope) and executed by harness code
templates whose only variable input is a JSON payload. There is no arbitrary shell/Python/file-write op.

Guest port: any object with ``guest_python(code) -> str`` (``Episode`` already has it); ``LocalGuest`` runs
the same code in a local directory for development and tests.

Transaction contract (``run_transaction``): validate everything first; write the *intent* (including each
operation's observed pre-state/snapshot) durably before any mutation; execute+verify in order; call the
caller's ``commit`` (bundle persistence) only after every operation verified; on any failure roll back the
started operations in reverse order and re-observe to prove the prior state is restored. If rollback fails or
the state cannot be proven, write a diagnostic checkpoint and raise ``GuestTransactionStop``.
``recover`` reconciles incomplete transactions after a crash before any new work.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

PHASES = ("proposal_validation", "precondition", "execution", "verification", "persistence", "rollback", "delivery")
_SECRET = re.compile(r"(?i)(bearer\s+\S+|sk-[\w-]{8,}|(?:api[_-]?key|token|secret|authorization)\W{0,3}\S+|[A-Za-z0-9+/_=-]{40,})")
BRANCH = re.compile(r"(?!-)(?!.*(?:\.\.|//|\.lock$|@\{|[\s~^:?*\[\\]))[A-Za-z0-9._/-]{1,100}")
HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63}){0,3}")
ENV_KEYS = ("DISPLAY", "TERM", "COLORTERM", "LANG", "LC_ALL", "TZ", "EDITOR", "VISUAL", "PAGER")
ENV_VALUE = re.compile(r"[A-Za-z0-9_.:/+@=,-]{0,200}")
FILE_ROOTS = (".config/workspace-profile/", ".config/workspace-dotfiles/", ".local/share/backgrounds/")
MAX_FILE_BYTES = 64 * 1024


def _sanitize(text, limit=300) -> str:
    return _SECRET.sub("[redacted]", str(text))[:limit]


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class OperationError(RuntimeError):
    def __init__(self, phase: str, message: str):
        super().__init__(message)
        self.phase = phase


class GuestTransactionStop(RuntimeError):
    """Rollback failed or actual state is uncertain: a diagnostic checkpoint was saved; do not continue.
    ``terminal`` tells the bridge/recorder that no further tool or provider request may run."""
    terminal = True

    def __init__(self, result: "TxResult"):
        super().__init__("guest transaction %s stopped (%s)" % (result.transaction_id, result.failed_phase))
        self.result = result


# ------------------------------------------------------------------ guest ports
class LocalGuest:
    """Development guest: runs the operation code with the local interpreter in ``cwd`` with ``HOME=home``."""

    def __init__(self, cwd, home=None, env=None):
        self.cwd, self.home, self.env = str(cwd), str(home or cwd), dict(env or {})

    def guest_python(self, code: str) -> str:
        env = {**os.environ, **self.env, "HOME": self.home}
        done = subprocess.run([sys.executable, "-I", "-c", code], cwd=self.cwd, env=env, capture_output=True,
                              text=True, timeout=60)
        if done.returncode:
            raise RuntimeError(done.stderr.strip()[-400:] or "guest code failed")
        return done.stdout


def _run(guest, script: str, payload: dict) -> dict:
    code = "import json\nP = json.loads(%r)\n%s" % (json.dumps(payload), script)
    out = guest.guest_python(code)
    lines = [x for x in str(out).strip().splitlines() if x.strip()]
    if not lines:
        raise OperationError("execution", "guest produced no result")
    try:
        data = json.loads(lines[-1])
    except ValueError as exc:
        raise OperationError("execution", "guest result is not JSON") from exc
    if not isinstance(data, dict):
        raise OperationError("execution", "guest result is not an object")
    return data


# ------------------------------------------------------------------ operation specs
@dataclass(frozen=True)
class OperationSpec:
    kind: str
    description: str
    scope: str
    arg_schema: dict
    validate: object  # args -> normalized args (raises OperationError("proposal_validation", ...))
    observe: object  # (guest, args) -> JSON-able fact dict (the pre/post fingerprint)
    check: object  # (args, observed) -> None or raises OperationError("precondition", ...)
    execute: object  # (guest, args, observed) -> None
    verify: object  # (args, observed_before, observed_after) -> None or raises OperationError("verification", ...)
    rollback: object  # (guest, args, observed_before) -> None
    satisfied: object  # (args, observed) -> bool, the persistent-setup equivalent check
    preconditions: tuple = ()
    verification: str = ""
    rollback_note: str = ""

    def describe(self, operation_id=None) -> dict:
        """Entry shaped for ``profile-packet.v1`` supported_operations (profiles._catalog)."""
        return {"operation_id": operation_id or self.kind, "kind": self.kind, "args": deepcopy(self.arg_schema),
                "preconditions": list(self.preconditions), "writable_scope": self.scope[:500],
                "rollback": self.rollback_note[:500], "verification": self.verification[:500],
                "persistent_setup": "the same request is stored in guest_setup and re-verified before each non-development run"}


def _bad(message):
    raise OperationError("proposal_validation", message)


def _only(args, allowed, required=()):
    if not isinstance(args, dict) or set(args) - set(allowed) or set(required) - set(args):
        _bad("arguments must be exactly %s" % sorted(allowed))


# ---- git_create_branch
_GIT = '''import subprocess, os
def git(*a, ok=(0,)):
    cwd = P.get("repo") or os.getcwd()
    if P.get("repo") and os.path.realpath(cwd) != cwd:
        raise RuntimeError("approved workspace is not a canonical path (symlink redirect refused)")
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    if r.returncode not in ok:
        raise RuntimeError("git " + a[0] + " failed: " + r.stderr.strip()[:200])
    return r
'''


def _git_validate(args):
    _only(args, ("name", "attach"), ("name",))
    name, attach, repo = args["name"], args.get("attach", False), None
    if not isinstance(name, str) or not BRANCH.fullmatch(name):
        _bad("invalid branch name")
    if type(attach) is not bool:
        _bad("attach must be boolean")
    return {"name": name, "attach": attach}


def _git_observe(guest, args):
    return _run(guest, _GIT + '''
name = P["name"]
head = git("rev-parse", "--verify", "-q", "HEAD", ok=(0, 1)).stdout.strip() or None
sym = git("symbolic-ref", "-q", "HEAD", ok=(0, 1)).stdout.strip() or None
ref = git("rev-parse", "--verify", "-q", "refs/heads/" + name, ok=(0, 1)).stdout.strip() or None
import hashlib
status = git("status", "--porcelain=v1", "-uall").stdout
print(json.dumps({"head_commit": head, "head_ref": sym, "branch_commit": ref,
                  "status_sha256": hashlib.sha256(status.encode()).hexdigest()}))
''', {**args})


def _git_check(args, obs):
    if obs["head_commit"] is None:
        raise OperationError("precondition", "repository has no commit to branch from")
    if obs["branch_commit"] is not None:
        raise OperationError("precondition", "branch already exists; refs are never overwritten")


def _git_execute(guest, args, obs):
    _run(guest, _GIT + '''
git("branch", P["name"], P["commit"])
if P["attach"]:
    git("symbolic-ref", "HEAD", "refs/heads/" + P["name"])
print(json.dumps({"ok": True}))
''', {**args, "commit": obs["head_commit"]})


def _git_verify(args, before, after):
    if after["branch_commit"] != before["head_commit"]:
        raise OperationError("verification", "branch does not point at the verified commit")
    if after["head_commit"] != before["head_commit"] or after["status_sha256"] != before["status_sha256"]:
        raise OperationError("verification", "commits or working-tree changes were not preserved")
    if args["attach"] and after["head_ref"] != "refs/heads/" + args["name"]:
        raise OperationError("verification", "HEAD is not attached to the new branch")


def _git_rollback(guest, args, before):
    _run(guest, _GIT + '''
name, commit = P["name"], P["commit"]
if P["head_ref"]:
    git("symbolic-ref", "HEAD", P["head_ref"])
else:
    git("update-ref", "--no-deref", "HEAD", commit)
cur = git("rev-parse", "--verify", "-q", "refs/heads/" + name, ok=(0, 1)).stdout.strip()
if cur == commit and P["created"]:
    git("update-ref", "-d", "refs/heads/" + name, commit)
print(json.dumps({"ok": True}))
''', {**args, "commit": before["head_commit"], "head_ref": before["head_ref"], "created": before["branch_commit"] is None})


def _git_satisfied(args, obs):
    return obs["branch_commit"] is not None and (not args["attach"] or obs["head_ref"] == "refs/heads/" + args["name"])


# ---- system_set_hostname
def _host_validate(args):
    _only(args, ("hostname",), ("hostname",))
    if not isinstance(args["hostname"], str) or not HOSTNAME.fullmatch(args["hostname"]) or len(args["hostname"]) > 100:
        _bad("invalid hostname")
    return {"hostname": args["hostname"]}


def _host_observe(guest, args):
    return _run(guest, "import socket\nprint(json.dumps({'hostname': socket.gethostname()}))\n", {})


def _host_execute(guest, args, obs):
    _run(guest, "import subprocess\nsubprocess.run(['hostname', P['hostname']], check=True, capture_output=True)\nprint(json.dumps({'ok': True}))\n", args)


def _host_rollback(guest, args, before):
    _run(guest, "import subprocess\nsubprocess.run(['hostname', P['hostname']], check=True, capture_output=True)\nprint(json.dumps({'ok': True}))\n",
         {"hostname": before["hostname"]})


# ---- env_set_workspace
def _env_validate(args):
    _only(args, ENV_KEYS)
    if not args:
        _bad("at least one environment value is required")
    for k, v in args.items():
        if not isinstance(v, str) or not ENV_VALUE.fullmatch(v):
            _bad("environment value not approved for " + k)
    return dict(sorted(args.items()))


_ENV_FILE = ".config/workspace-profile/env.sh"
_ENV_HEADER = "# managed by workspace profile proxy\n"


def _env_observe(guest, args):
    return _run(guest, '''import os, base64, hashlib
path = os.path.join(os.environ["HOME"], P["rel"])
data = open(path, "rb").read() if os.path.isfile(path) and not os.path.islink(path) else None
parts = P["rel"].split("/")[:-1]
dirs = {"/".join(parts[:i + 1]): os.path.isdir(os.path.join(os.environ["HOME"], *parts[:i + 1])) for i in range(len(parts))}
print(json.dumps({"exists": data is not None, "sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
                  "b64": base64.b64encode(data).decode() if data is not None else None,
                  "link": os.path.islink(path), "dirs": dirs}))
''', {"rel": _ENV_FILE})


def _env_render(args) -> bytes:
    return (_ENV_HEADER + "".join("export %s='%s'\n" % kv for kv in args.items())).encode()


def _env_check(args, obs):
    if obs["link"]:
        raise OperationError("precondition", "managed environment file path is a symlink")
    if obs["exists"] and not base64.b64decode(obs["b64"]).decode("utf-8", "replace").startswith(_ENV_HEADER):
        raise OperationError("precondition", "existing environment file is not harness-managed; not overwritten")


_WRITE_FILE = '''import os, base64
home = os.environ["HOME"]
path = os.path.join(home, P["rel"])
parent = os.path.dirname(path)
created = []
d = parent
while not os.path.isdir(d):
    created.append(d); d = os.path.dirname(d)
os.makedirs(parent, exist_ok=True)
real = os.path.realpath(parent)
if not real.startswith(os.path.realpath(home) + os.sep) or os.path.islink(path):
    raise RuntimeError("path escapes the guest home")
flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if P["exclusive"] else os.O_TRUNC)
fd = os.open(path, flags, P["mode"])
with os.fdopen(fd, "wb") as f:
    f.write(base64.b64decode(P["data"]))
print(json.dumps({"ok": True, "created_dirs": created}))
'''


def _env_execute(guest, args, obs):
    _run(guest, _WRITE_FILE, {"rel": _ENV_FILE, "data": base64.b64encode(_env_render(args)).decode(), "mode": 0o644,
                              "exclusive": not obs["exists"]})


def _env_verify(args, before, after):
    if after["sha256"] != hashlib.sha256(_env_render(args)).hexdigest():
        raise OperationError("verification", "environment file content differs from the request")


def _env_rollback(guest, args, before):
    _run(guest, '''import os, base64
path = os.path.join(os.environ["HOME"], P["rel"])
if P["old"] is None:
    if os.path.isfile(path): os.unlink(path)
    for rel in P["new_dirs"]:
        try: os.rmdir(os.path.join(os.environ["HOME"], rel))
        except OSError: pass
else:
    open(path, "wb").write(base64.b64decode(P["old"]))
print(json.dumps({"ok": True}))
''', {"rel": _ENV_FILE, "old": before["b64"],
                                                  "new_dirs": sorted((d for d, there in before["dirs"].items() if not there), reverse=True)})


# ---- files_install_preset
def _files_validate(args):
    _only(args, ("path", "content_b64", "mode"), ("path", "content_b64"))
    rel, mode = args["path"], args.get("mode", "0644")
    if (not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/") or "//" in rel or "\0" in rel
            or not rel.startswith(FILE_ROOTS) or rel.endswith("/") or len(rel) > 200):
        _bad("path outside the approved dedicated locations")
    if mode not in ("0600", "0644"):
        _bad("mode must be 0600 or 0644")
    try:
        data = base64.b64decode(args["content_b64"], validate=True)
    except Exception:
        _bad("content_b64 is not base64")
    if len(data) > MAX_FILE_BYTES:
        _bad("file too large")
    return {"path": rel, "content_b64": args["content_b64"], "mode": mode}


def _files_observe(guest, args):
    return _run(guest, '''import os, hashlib
home = os.environ["HOME"]
res = {}
for rel in P["paths"]:
    p = os.path.join(home, rel)
    if os.path.islink(p): res[rel] = {"state": "symlink"}
    elif os.path.isfile(p): res[rel] = {"state": "file", "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest()}
    elif os.path.exists(p): res[rel] = {"state": "other"}
    else: res[rel] = {"state": "absent"}
dirs = {}
for rel in P["paths"]:
    parts = rel.split("/")[:-1]
    for i in range(len(parts)):
        d = "/".join(parts[:i + 1]); dirs[d] = os.path.isdir(os.path.join(home, d))
print(json.dumps({"files": res, "dirs": dirs}))
''', {"paths": [args["path"]]})


def _files_check(args, obs):
    for rel, st in obs["files"].items():
        if st["state"] != "absent":
            raise OperationError("precondition", "target path already exists (%s); never overwritten: %s" % (st["state"], rel))


def _files_execute(guest, args, obs):
    _run(guest, _WRITE_FILE, {"rel": args["path"], "data": args["content_b64"], "mode": int(args["mode"], 8), "exclusive": True})


def _files_verify(args, before, after):
    st = after["files"][args["path"]]
    if st.get("sha256") != hashlib.sha256(base64.b64decode(args["content_b64"])).hexdigest():
        raise OperationError("verification", "installed file content differs: " + args["path"])


def _files_rollback(guest, args, before):
    _run(guest, '''import os, hashlib
home = os.environ["HOME"]
for item in P["files"]:
    p = os.path.join(home, item["path"])
    if item["created"] and os.path.isfile(p) and not os.path.islink(p) and hashlib.sha256(open(p, "rb").read()).hexdigest() == item["sha256"]:
        os.unlink(p)
for rel in P["new_dirs"]:
    try: os.rmdir(os.path.join(home, rel))
    except OSError: pass
print(json.dumps({"ok": True}))
''', {"new_dirs": sorted((d for d, there in before["dirs"].items() if not there), reverse=True), "files": [{"path": args["path"], "sha256": hashlib.sha256(base64.b64decode(args["content_b64"])).hexdigest(),
                 "created": before["files"][args["path"]]["state"] == "absent"}]})


def _files_satisfied(args, obs):
    return obs["files"][args["path"]].get("sha256") == hashlib.sha256(base64.b64decode(args["content_b64"])).hexdigest()


def _no_check(args, obs):
    return None


SPECS = (
    OperationSpec("git_create_branch", "Create a named branch at the verified current commit; optionally attach HEAD.",
                  "git refs in the guest worktree (refs/heads/<name>, HEAD); commits and working tree are preserved",
                  {"name": {"type": "string", "required": True, "max_length": 100}, "attach": {"type": "boolean"}},
                  _git_validate, _git_observe, _git_check, _git_execute, _git_verify, _git_rollback, _git_satisfied,
                  ("HEAD has a commit", "refs/heads/<name> does not exist"),
                  "ref equals the pre-state HEAD commit; HEAD commit and porcelain status unchanged; HEAD attached when requested",
                  "restore HEAD ref/commit; delete the new ref only if it still equals the created commit"),
    OperationSpec("system_set_hostname", "Set the guest hostname to the profile value; service DNS is untouched.",
                  "the guest UTS hostname only", {"hostname": {"type": "hostname", "required": True}},
                  _host_validate, _host_observe, _no_check, _host_execute,
                  lambda args, before, after: None if after["hostname"] == args["hostname"] else (_ for _ in ()).throw(
                      OperationError("verification", "hostname was not applied")),
                  _host_rollback, lambda args, obs: obs["hostname"] == args["hostname"], (),
                  "gethostname() equals the requested value", "restore the previous hostname"),
    OperationSpec("env_set_workspace", "Set approved workspace environment/display values in a dedicated env file.",
                  "~/.config/workspace-profile/env.sh only; keys limited to %s" % ", ".join(ENV_KEYS),
                  {k: {"type": "string", "max_length": 200} for k in ENV_KEYS},
                  _env_validate, _env_observe, _env_check, _env_execute, _env_verify, _env_rollback,
                  lambda args, obs: obs["sha256"] == hashlib.sha256(_env_render(args)).hexdigest(),
                  ("path is not a symlink", "existing file, if any, is harness-managed"),
                  "file digest equals the rendered request", "restore previous file bytes or remove the new file"),
    OperationSpec("files_install_preset", "Install preset background/dotfile contents at dedicated approved paths; never overwrites.",
                  "one new file under %s relative to the guest home" % ", ".join(FILE_ROOTS),
                  {"path": {"type": "string", "required": True, "max_length": 200},
                   "content_b64": {"type": "string", "required": True, "max_length": 87400},
                   "mode": {"type": "enum", "values": ["0600", "0644"]}},
                  _files_validate, _files_observe, _files_check, _files_execute, _files_verify, _files_rollback, _files_satisfied,
                  ("each path is absent", "paths stay inside the guest home"), "installed digests equal the requested content",
                  "remove only files created by this transaction whose digest still matches"),
)


class Catalog:
    """``workspace`` is the one approved guest repository path (the profile's worktree). Git operations are
    pinned to it: a request naming any other repository is rejected and the guest re-checks the real path.
    ``slots`` lists how many catalog operation_ids exist per kind (``kind``, ``kind:2``, ...), so one
    transaction may contain several operations of a kind while each request still names a catalog entry."""

    def __init__(self, specs=SPECS, *, workspace=None, slots=3):
        if workspace is not None and (not isinstance(workspace, str) or not workspace.startswith("/")
                                      or ".." in workspace.split("/") or len(workspace) > 300):
            raise ValueError("workspace must be a normalized absolute guest path")
        self._specs = {s.kind: s for s in specs}
        self.workspace, self.slots = workspace, slots

    def with_workspace(self, workspace) -> "Catalog":
        return Catalog(tuple(self._specs.values()), workspace=workspace, slots=self.slots)

    def kinds(self):
        return sorted(self._specs)

    def spec(self, kind) -> OperationSpec:
        if kind not in self._specs:
            _bad("unsupported operation kind")
        return self._specs[kind]

    def operation_ids(self, kind):
        return [kind] + ["%s:%d" % (kind, i) for i in range(2, self.slots + 1)]

    def describe(self) -> list:
        out = []
        for kind in self.kinds():
            for oid in self.operation_ids(kind):
                entry = self._specs[kind].describe(oid)
                if kind == "git_create_branch":
                    entry["writable_scope"] = ("git refs in %s only; " % (self.workspace or "the guest working directory")
                                               + entry["writable_scope"])[:500]
                out.append(entry)
        return out

    def validate_request(self, request: dict) -> dict:
        """Normalized request or ``OperationError('proposal_validation')``. Preconditions are checked at run time."""
        if not isinstance(request, dict) or not {"operation_id", "kind", "args"} <= set(request):
            _bad("malformed operation request")
        oid = request["operation_id"]
        spec = self.spec(request["kind"])
        if not isinstance(oid, str) or oid not in self.operation_ids(spec.kind):
            _bad("operation_id is not a catalog entry for this kind")
        args = spec.validate(deepcopy(request["args"]))
        if spec.kind == "git_create_branch" and self.workspace is not None:
            args["repo"] = self.workspace  # never caller-chosen; the guest re-verifies the real path
        return {**{k: deepcopy(v) for k, v in request.items() if k != "args"}, "args": args}


DEFAULT_CATALOG = Catalog()


# ------------------------------------------------------------------ durable log
def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%s" % (path.name, uuid.uuid4().hex[:8]))
    with open(tmp, "w") as f:
        f.write(json.dumps(data, indent=1, sort_keys=True, default=str))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class TransactionLog:
    """``<root>/<tx>.json`` is the durable record (rewritten atomically at every state change);
    ``events.jsonl`` is the append-only phase log."""

    def __init__(self, root):
        self.root, self._lock = Path(root), threading.Lock()

    def path(self, tid) -> Path:
        return self.root / ("%s.json" % tid)

    def save(self, record: dict):
        _atomic_write(self.path(record["transaction_id"]), record)

    def load(self, tid) -> dict:
        return json.loads(self.path(tid).read_text())

    def event(self, record: dict, **fields):
        item = {"at": time.time(), "transaction_id": record["transaction_id"], **fields}
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / "events.jsonl", "a") as f:
                f.write(json.dumps(item, sort_keys=True, default=str) + "\n")
        record.setdefault("events", []).append(item)

    def incomplete(self) -> list:
        out = []
        for p in sorted(self.root.glob("*.json")) if self.root.exists() else []:
            try:
                r = json.loads(p.read_text())
            except ValueError:
                continue
            if r.get("status") in ("intent", "running", "committing", "rolling_back"):
                out.append(r)
        return out


@dataclass
class TxResult:
    transaction_id: str
    ok: bool
    status: str  # committed | rolled_back | rejected | stopped
    failed_phase: str | None = None
    failed_operation: str | None = None
    error: str | None = None
    operations: list = field(default_factory=list)
    recovery: list = field(default_factory=list)
    continued: bool = True

    def to_dict(self):
        return deepcopy(self.__dict__)


def _result(rec, ok, status, phase=None, op=None, error=None, continued=True):
    return TxResult(rec["transaction_id"], ok, status, phase, op, _sanitize(error) if error else None,
                    [{"operation_id": o["operation_id"], "kind": o["kind"], "state": o["state"], "before": o.get("before"),
                      "after": o.get("after")} for o in rec["operations"]], list(rec.get("recovery", [])), continued)


def _rollback_all(guest, rec, log, catalog, hook=None):
    """Reverse-order compensation of every started operation; returns True when prior state is proven restored."""
    clean = True
    rec["status"] = "rolling_back"
    log.save(rec)
    for op in reversed(rec["operations"]):
        if op["state"] in ("pending", "rolled_back", "unchanged"):
            continue
        spec = catalog.spec(op["kind"])
        step = {"operation_id": op["operation_id"], "phase": "rollback"}
        try:
            if hook:
                hook("rollback", op)
            spec.rollback(guest, op["args"], op["before"])
            now = spec.observe(guest, op["args"])
            if now != op["before"]:
                raise OperationError("rollback", "state after rollback differs from the saved pre-state")
            op["state"] = "rolled_back"
            step["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - rollback failure is uncertain state
            clean = False
            op["state"] = "rollback_failed"
            step.update(status="failed", error=_sanitize(exc))
        rec.setdefault("recovery", []).append(step)
        log.event(rec, **step)
        log.save(rec)
    return clean


def _stop(rec, log, phase, op_id, error):
    rec["status"] = "stopped"
    log.save(rec)
    diag = log.root / ("diagnostic-%s.json" % rec["transaction_id"])
    _atomic_write(diag, {"transaction_id": rec["transaction_id"], "failed_phase": phase, "operation_id": op_id,
                         "error": _sanitize(error), "operations": rec["operations"], "recovery": rec.get("recovery", []),
                         "note": "guest state is uncertain; reconcile manually before further work"})
    log.event(rec, phase=phase, operation_id=op_id, status="stopped", diagnostic=str(diag), continued=False)
    raise GuestTransactionStop(_result(rec, False, "stopped", phase, op_id, error, continued=False))


def run_transaction(guest, requests, log: TransactionLog, *, catalog: Catalog = DEFAULT_CATALOG, commit=None,
                    fault_hook=None, transaction_id=None, context=None, satisfied=()) -> TxResult:
    """Validate, execute and verify ``requests`` in order, then ``commit()``; roll back on any failure.

    ``satisfied`` holds change IDs (profile/rule changes of the same proposal) the caller already validated and
    ordered as a whole graph; a request may depend on them or on earlier operations of this transaction.
    ``commit`` persists the bundle and may raise (phase ``persistence``). ``fault_hook(phase, op_record)`` is a
    test/injection seam called at the start of each phase of each operation."""
    tid = transaction_id or "tx-" + uuid.uuid4().hex[:12]
    rec = {"transaction_id": tid, "status": "intent", "context": context or {}, "operations": [], "recovery": [], "events": []}
    try:
        if not requests:
            raise OperationError("proposal_validation", "no operations")
        seen = set()
        for req in requests:
            norm = catalog.validate_request(req)
            deps = norm.get("depends_on") or norm.get("dependencies") or []
            if (norm["operation_id"] in seen or not isinstance(deps, list)
                    or any(d not in seen and d not in satisfied for d in deps)):
                _bad("duplicate operation_id or dependency not ordered before the operation")
            seen.add(norm["operation_id"])
            if norm.get("change_id"):
                seen.add(norm["change_id"])
            rec["operations"].append({"operation_id": norm["operation_id"], "kind": norm["kind"], "args": norm["args"],
                                      "state": "pending", "before": None, "after": None,
                                      "finding_id": norm.get("finding_id")})
    except OperationError as exc:
        rec["status"] = "rejected"
        log.event(rec, phase="proposal_validation", status="rejected", error=_sanitize(exc), continued=True)
        log.save(rec)
        return _result(rec, False, "rejected", "proposal_validation", None, exc)
    log.save(rec)  # durable intent
    log.event(rec, phase="intent", status="recorded", operations=[o["operation_id"] for o in rec["operations"]])
    rec["status"] = "running"
    current = None
    try:
        for op in rec["operations"]:
            current = op
            spec = catalog.spec(op["kind"])
            phase = "precondition"
            if fault_hook:
                fault_hook(phase, op)
            before = spec.observe(guest, op["args"])
            spec.check(op["args"], before)
            op["before"], op["state"] = before, "executing"
            log.save(rec)  # snapshot + intent are durable before the mutation
            phase = "execution"
            if fault_hook:
                fault_hook(phase, op)
            spec.execute(guest, op["args"], before)
            op["state"] = "executed"
            phase = "verification"
            if fault_hook:
                fault_hook(phase, op)
            after = spec.observe(guest, op["args"])
            spec.verify(op["args"], before, after)
            op["after"], op["state"] = after, "verified"
            log.save(rec)
            log.event(rec, phase="verification", operation_id=op["operation_id"], status="ok", before=before, after=after)
        current, phase = None, "persistence"
        rec["status"] = "committing"
        log.save(rec)
        if fault_hook:
            fault_hook(phase, None)
        if commit is not None:
            commit()
    except GuestTransactionStop:
        raise
    except Exception as exc:  # noqa: BLE001
        failed_phase = getattr(exc, "phase", None) if isinstance(exc, OperationError) else None
        failed_phase = failed_phase if failed_phase in PHASES else phase
        op_id = current["operation_id"] if current else None
        log.event(rec, phase=failed_phase, operation_id=op_id, status="failed", error=_sanitize(exc))
        if not _rollback_all(guest, rec, log, catalog, fault_hook):
            _stop(rec, log, failed_phase, op_id, exc)
        rec["status"] = "rolled_back"
        log.save(rec)
        log.event(rec, phase=failed_phase, operation_id=op_id, status="rolled_back", continued=True)
        return _result(rec, False, "rolled_back", failed_phase, op_id, exc)
    rec["status"] = "committed"
    log.save(rec)
    log.event(rec, phase="persistence", status="committed", continued=True)
    return _result(rec, True, "committed")


def recover(guest, log: TransactionLog, *, catalog: Catalog = DEFAULT_CATALOG, is_committed=None, fault_hook=None) -> list:
    """Reconcile transactions left incomplete by a crash. A transaction in ``committing`` whose bundle was
    persisted (``is_committed(tx_id) -> bool``) is marked committed; everything else is rolled back. Raises
    ``GuestTransactionStop`` when rollback cannot be proven."""
    results = []
    for rec in log.incomplete():
        tid = rec["transaction_id"]
        if rec["status"] == "committing" and is_committed is not None and is_committed(tid):
            rec["status"] = "committed"
            log.save(rec)
            log.event(rec, phase="recovery", status="committed-after-restart")
            results.append(_result(rec, True, "committed"))
            continue
        log.event(rec, phase="recovery", status="rolling-back-incomplete", from_status=rec["status"])
        if not _rollback_all(guest, rec, log, catalog, fault_hook):
            _stop(rec, log, "rollback", None, "recovery rollback failed")
        rec["status"] = "rolled_back"
        log.save(rec)
        results.append(_result(rec, False, "rolled_back", "recovery", None, "incomplete transaction reconciled"))
    return results


# ------------------------------------------------------------------ deterministic setup
@dataclass
class SetupReceipt:
    ok: bool
    entries: list = field(default_factory=list)  # [{operation_id, kind, required, status}]
    failed_required: list = field(default_factory=list)
    transactions: list = field(default_factory=list)

    def to_dict(self):
        return deepcopy(self.__dict__)


def setup_entry(request: dict, *, finding_id=None, required=True) -> dict:
    """Persistent-setup form of a verified operation request (explicit requirement, never a replayed state)."""
    return {"operation_id": request["operation_id"], "kind": request["kind"], "args": {k: deepcopy(v) for k, v in request["args"].items() if k != "repo"},
            "required": required, "finding_id": finding_id or request.get("finding_id")}


def apply_setup(guest, setup, log: TransactionLog, *, catalog: Catalog = DEFAULT_CATALOG) -> SetupReceipt:
    """Apply saved ``guest_setup`` (verifying each; already-satisfied entries are left alone) with no LLM.
    A failed required entry makes the receipt not ok (profile ineligible for measurement)."""
    receipt = SetupReceipt(True)
    for entry in setup:
        item = {"operation_id": entry["operation_id"], "kind": entry["kind"], "required": bool(entry.get("required", True))}
        try:
            req = catalog.validate_request(entry)
            spec = catalog.spec(req["kind"])
            if spec.satisfied(req["args"], spec.observe(guest, req["args"])):
                item["status"] = "already-satisfied"
            else:
                tx = run_transaction(guest, [req], log, catalog=catalog, context={"setup": True})
                receipt.transactions.append(tx.transaction_id)
                if not tx.ok:
                    raise OperationError(tx.failed_phase or "execution", tx.error or "setup failed")
                if not spec.satisfied(req["args"], spec.observe(guest, req["args"])):
                    raise OperationError("verification", "setup not satisfied after execution")
                item["status"] = "applied"
        except GuestTransactionStop:
            raise
        except Exception as exc:  # noqa: BLE001
            item.update(status="failed", error=_sanitize(exc), phase=getattr(exc, "phase", "execution"))
            if item["required"]:
                receipt.ok = False
                receipt.failed_required.append(item["operation_id"])
        receipt.entries.append(item)
    return receipt


def probe_git(guest, repo=None) -> dict:
    """Read-only repository state: ``{head_ref, head_commit, branches}`` (``head_ref`` None when detached)."""
    return _run(guest, _GIT + '''
head = git("rev-parse", "--verify", "-q", "HEAD", ok=(0, 1)).stdout.strip() or None
sym = git("symbolic-ref", "-q", "HEAD", ok=(0, 1)).stdout.strip() or None
names = git("for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
print(json.dumps({"head_commit": head, "head_ref": sym, "branches": sorted(names)}))
''', {"repo": repo} if repo else {})


def probe_identity(guest) -> dict:
    """Read-only machine identity of the actual guest (os, user, ids, hostname, home, cwd, selected environment)."""
    return _run(guest, '''import os, platform, pwd, socket
env = {k: os.environ[k] for k in P["env"] if k in os.environ}
print(json.dumps({"os": platform.system().lower(), "user": pwd.getpwuid(os.getuid()).pw_name, "uid": os.getuid(),
                  "gid": os.getgid(), "hostname": socket.gethostname(), "home": os.environ.get("HOME"),
                  "worktree": os.path.realpath(os.getcwd()), "environment": env}))
''', {"env": list(ENV_KEYS) + ["DISPLAY", "TERM", "SHELL", "EDITOR"]})
