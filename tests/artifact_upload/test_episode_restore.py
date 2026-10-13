"""Episode.restore against an in-memory Docker: capture a snapshot, then rebuild it from scratch."""
import io
import json
import posixpath
import tarfile
from types import SimpleNamespace

import pytest

from eval.artifact_upload import lifecycle
from eval.artifact_upload.lifecycle import Episode, InfrastructureError

PROJECT = "artifact-t007-dur"
NET = PROJECT + "_episode"
WVOL, SVOL = PROJECT + "_natural-workspace", PROJECT + "_service-state"
WORKTREE = "/home/alex/work/sqlite-utils"


def mk_tar(entries):
    """entries: {name: bytes | None (directory)}; numeric owner 1000."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 1000
            if data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeDocker:
    def __init__(self, token):
        self.token = token
        self.volumes, self.networks, self.containers, self.log = {}, set(), {}, []
        self.fail_extract = False
        self.git = "HEAD abc\nrefs\n# branch.oid abc\n"
        self.images = {"img-w", "img-s"}

    # container filesystem helpers -------------------------------------------------
    def put(self, ctr, path, data):  # path -> (kind, bytes)
        self.containers[ctr]["fs"][path] = data

    def tar_in(self, ctr, dest, raw):
        fs = self.containers[ctr]["fs"]
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for m in tar:
                path = posixpath.join(dest, posixpath.normpath(m.name))
                fs[path] = (m.type, m.mode, m.uid, m.gid, tar.extractfile(m).read() if m.isfile() else b"")

    def tar_out(self, ctr, path):
        fs, base = self.containers[ctr]["fs"], posixpath.basename(path)
        hit = {p: v for p, v in fs.items() if p == path or p.startswith(path + "/")}
        if not hit:
            return None
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for p in sorted(hit):
                kind, mode, uid, gid, data = hit[p]
                info = tarfile.TarInfo(posixpath.join(base, posixpath.relpath(p, path)) if p != path else base)
                info.type, info.mode, info.uid, info.gid = kind, mode, uid, gid
                info.size = len(data) if kind == tarfile.REGTYPE else 0
                tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        return buf.getvalue()

    def new_container(self, name, cid, image, fs=None):
        self.containers[name] = {"id": cid, "image": image, "fs": dict(fs or {}), "running": False, "args": []}
        self.containers[cid] = self.containers[name]

    # dispatcher ---------------------------------------------------------------------
    def row(self, owner):
        worker = owner == "worker"
        return {"Id": ("wc" if worker else "sc") + "-full-id", "Name": "/" + PROJECT + ("-worker-1" if worker else "-service-1"),
                "Image": "img-w" if worker else "img-s",
                "Config": {"Hostname": "alex-dev" if worker else "oldid", "User": "1000:1000" if worker else "", "WorkingDir": WORKTREE if worker else "/app",
                           "Env": [f"ARTIFACT_API_TOKEN={self.token}", "PATH=/usr/bin"], "Entrypoint": None,
                           "Cmd": ["sleep", "infinity"] if worker else ["python", "-c", "serve"],
                           "Healthcheck": None if worker else {"Test": ["CMD", "python", "-c", "probe"], "Interval": 2_000_000_000, "Retries": 30}},
                "HostConfig": {"Privileged": False, "Memory": 0, "NanoCpus": 0, "CapAdd": None, "CapDrop": None, "SecurityOpt": None,
                               "PortBindings": {}, "Binds": [f"{WVOL if worker else SVOL}:x:rw"], "RestartPolicy": {"Name": "no"}},
                "Mounts": [{"Type": "volume", "Name": WVOL if worker else SVOL, "Destination": WORKTREE if worker else "/srv/store", "RW": True}],
                "NetworkSettings": {"Networks": {NET: {"Aliases": ["service", "sc"] if not worker else ["worker-natural", "wc"]}}}}

    def call(self, a, stdin=None):
        """Returns (returncode, stdout bytes)."""
        self.log.append(a)
        cmd = a[0]
        if a[:2] == ["image", "inspect"]:
            return (0 if a[2] in self.images else 1), b""
        if a[:2] == ["container", "inspect"]:
            return (0 if a[2] in self.containers else 1), b""
        if a[:2] == ["volume", "inspect"]:
            return (0 if a[2] in self.volumes else 1), b""
        if a[:2] == ["network", "inspect"]:
            return (0 if a[2] in self.networks else 1), b""
        if a[:2] == ["volume", "create"]:
            self.volumes[a[-1]] = b""
            return 0, b""
        if a[:2] == ["volume", "rm"]:
            return (0, b"") if self.volumes.pop(a[2], None) is not None else (1, b"")
        if a[:2] == ["network", "create"]:
            self.networks.add(a[-1])
            return 0, b""
        if a[:2] == ["network", "rm"]:
            self.networks.discard(a[2])
            return 0, b""
        if a[:3] == ["container", "diff", "wc"]:
            return 0, b"A /home/alex/.cache\nA /home/alex/.cache/pip\nA /home/alex/notes.txt\nD /tmp/gone\nC /etc/passwd\n"
        if a[:3] == ["container", "diff", "sc"]:
            return 0, b"A /srv/store.audit.jsonl\n"
        if cmd == "inspect" and a[1] != "--format":
            return 0, json.dumps([self.row("worker" if a[1] == "wc" else "service")]).encode()
        if cmd == "inspect":
            return 0, self.containers[a[-1]]["id"].encode()
        if cmd in ("stop", "top", "logs"):
            return 0, b""
        if cmd == "create":
            name = a[a.index("--name") + 1]
            self.new_container(name, name + "-id", a[-3] if False else next(x for x in a if x.startswith("img-")))
            self.containers[name]["args"] = a
            return 0, (name + "-id").encode()
        if cmd == "start":
            self.containers[a[1]]["running"] = True
            return 0, b""
        if cmd == "rm":
            gone = self.containers.get(a[-1])
            for key in [k for k, v in self.containers.items() if v is gone]:
                del self.containers[key]
            return 0, b""
        if cmd == "run":
            vol = next(x for x in a if x.endswith(":/v") or x.endswith(":/v:ro")).split(":")[0]
            if "-cpf" in a:
                return 0, self.volumes[vol]
            if self.fail_extract:
                return 1, b""
            self.volumes[vol] = stdin.read()
            return 0, b""
        if cmd == "cp":
            if a[1] == "-":
                ctr, dest = a[2].split(":", 1)
                self.tar_in(ctr, dest, stdin.read())
                return 0, b""
            ctr, path = a[1].split(":", 1)
            out = self.tar_out(ctr, path)
            return (0, out) if out is not None else (1, b"")
        if cmd == "exec":
            joined = " ".join(a)
            if "git rev-parse" in joined:
                return 0, self.git.encode()
            if "rm" in a:
                self.containers[a[3]]["fs"].pop(a[-1], None)
            if "test" in joined and "$1" in joined:
                return (0 if a[-1] not in self.containers[a[1]]["fs"] else 1), b""
            if "index.json" in joined:
                return 0, b'{"objects": {"a": {}}}'
            return 0, b""
        raise AssertionError(f"unhandled docker call {a}")


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker(token="tok-original")

    def fake_run(args, *, check=True, capture=True):
        rc, out = fake.call(args[1:])
        if check and rc:
            raise InfrastructureError(f"failed {args}")
        return SimpleNamespace(returncode=rc, stdout=out.decode(errors="replace"), stderr="")

    def fake_subprocess(args, **kw):
        stdin = kw.get("stdin")
        rc, out = fake.call(args[1:], stdin)
        if kw.get("stdout") is not None and kw["stdout"] not in (-1, None) and hasattr(kw["stdout"], "write"):
            kw["stdout"].write(out)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=b"")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    monkeypatch.setattr(lifecycle.subprocess, "run", fake_subprocess)
    # Original (captured) world.
    fake.volumes[WVOL] = mk_tar({"./": None, "./src.py": b"print(1)\n", "./.git": None, "./.git/HEAD": b"ref: x\n"})
    fake.volumes[SVOL] = mk_tar({"./": None, "./index.json": b'{"objects": {"a": {}}}'})
    fake.new_container("wc", "wc", "img-w", {
        "/home/alex/.cache": (tarfile.DIRTYPE, 0o700, 1000, 1000, b""),
        "/home/alex/.cache/pip": (tarfile.DIRTYPE, 0o755, 1000, 1000, b""),
        "/home/alex/.cache/pip/x": (tarfile.REGTYPE, 0o644, 1000, 1000, b"wheel"),
        "/home/alex/notes.txt": (tarfile.REGTYPE, 0o644, 1000, 1000, b"keep me"),
        "/etc/passwd": (tarfile.REGTYPE, 0o644, 0, 0, b"alex:x:1000")})
    fake.new_container("sc", "sc", "img-s", {"/srv/store.audit.jsonl": (tarfile.REGTYPE, 0o600, 0, 0, b'{"a":1}\n')})
    return fake


@pytest.fixture
def snapshot(docker, tmp_path):
    episode = Episode("natural-looking", "dur", tmp_path / "ev")
    episode.api_token = "tok-original"
    episode.resources.update({"worker_container": "wc", "service_container": "sc", "workspace_volume": WVOL,
                              "service_volume": SVOL, "worker_identity": {"worktree": WORKTREE},
                              "profile_manifest": {"worktree": WORKTREE}, "network": NET})
    episode.common_image = "img-w"
    snap = episode.continuation_snapshot(tmp_path / "dur")
    assert snap["durable_volume_export"], snap["failures"]
    # Original resources are gone after teardown; the snapshot is all that is left.
    for name in ("wc", "sc", "wc-id", "sc-id"):
        docker.containers.pop(name, None)
    docker.volumes.clear()
    docker.log.clear()
    return snap


def mutations(docker):
    return [c for c in docker.log if c[0] in ("create", "start", "rm") or c[:2] in (["volume", "create"], ["network", "create"], ["volume", "rm"], ["network", "rm"])]


def test_capture_keeps_added_ancestor_headers_and_private_resources(snapshot):
    kinds = [(a["owner"], a["kind"], a.get("source")) for a in snapshot["artifacts"]]
    assert ("worker", "layer-dir", "/home/alex/.cache") in kinds and ("worker", "layer-path", "/home/alex/.cache/pip") in kinds
    secret = json.loads(open(next(a["path"] for a in snapshot["artifacts"] if a["kind"] == "episode-secret-private")).read())
    assert secret["ARTIFACT_API_TOKEN"] == "tok-original" and secret["resources"]["worker_container"] == "wc"


def test_restore_rebuilds_stopped_resources_without_initialization(snapshot, docker, tmp_path):
    episode = Episode("natural-looking", "dur", tmp_path / "ev2")
    assert episode.api_token != "tok-original"
    resources = episode.restore(snapshot, segment=1)
    assert episode.project == PROJECT + "-s1" and episode.api_token == "tok-original"
    assert docker.volumes[PROJECT + "-s1_natural-workspace"] == mk_tar({"./": None, "./src.py": b"print(1)\n", "./.git": None, "./.git/HEAD": b"ref: x\n"})
    worker = docker.containers[PROJECT + "-s1-worker-1"]
    assert worker["running"] and docker.containers[PROJECT + "-s1-service-1"]["running"]
    assert worker["fs"]["/home/alex/notes.txt"][4] == b"keep me" and worker["fs"]["/home/alex/.cache/pip/x"][4] == b"wheel"
    assert worker["fs"]["/home/alex/.cache"][:4][1:] == (0o700, 1000, 1000)  # ancestor owner/mode restored from its header
    assert "/tmp/gone" not in worker["fs"]
    # Original network alias and token reached the recreated service; no source copy, repair, seed or compose.
    create = next(c for c in docker.log if c[0] == "create" and "service-1" in c[c.index("--name") + 1])
    assert create[create.index("--network-alias") + 1] == "service" and "--health-cmd" in create
    assert not any(c[0] == "compose" or (c[0] == "cp" and "protected.whl" in " ".join(c)) for c in docker.log)
    assert resources["restored"]["checks"] and resources["service_container"] == PROJECT + "-s1-service-1-id"
    assert "tok-original" not in json.dumps(create)  # token travels in a private env file, not argv
    # Scoped cleanup removes only the restored project's resources.
    episode.resources["service_evidence"] = {"done": True}
    docker.volumes["unrelated"] = b""
    episode.cleanup()
    assert "unrelated" in docker.volumes and not any(k.startswith(PROJECT + "-s1") for k in docker.volumes)
    assert episode.resources["cleanup_exit"] == 0


def test_preflight_refuses_unsupported_cases_before_any_mutation(snapshot, docker, tmp_path):
    episode = Episode("natural-looking", "dur", tmp_path / "ev2")
    cfg = next(a["path"] for a in snapshot["artifacts"] if a["kind"] == "container-config-private")
    original = open(cfg).read()
    # Unsupported privileged worker; existing target name; image missing - all fail with zero creates.
    for edit, message in (
            (lambda d: d["worker"]["HostConfig"].update(Privileged=True), "Privileged"),
            (lambda d: d["service"]["Mounts"].append({"Type": "bind", "Name": "", "Destination": "/x"}), "mounts"),
            (lambda d: d["service"]["NetworkSettings"]["Networks"].update(other={}), "network")):
        data = json.loads(original)
        edit(data)
        raw = json.dumps(data, sort_keys=True).encode()
        art = next(a for a in snapshot["artifacts"] if a["kind"] == "container-config-private")
        open(cfg, "wb").write(raw)
        patched = dict(snapshot, artifacts=[dict(a, sha256=lifecycle.hashlib.sha256(raw).hexdigest(), bytes=len(raw)) if a is art else a for a in snapshot["artifacts"]])
        with pytest.raises(InfrastructureError, match=message):
            episode.restore(patched)
        assert mutations(docker) == []
    open(cfg, "w").write(original)
    docker.images.discard("img-s")
    with pytest.raises(InfrastructureError, match="not available locally"):
        episode.preflight_restore(snapshot)
    docker.images.add("img-s")
    docker.volumes[PROJECT + "-s1_service-state"] = b""
    with pytest.raises(InfrastructureError, match="already exists"):
        episode.preflight_restore(snapshot)
    assert mutations(docker) == []


def test_verification_failure_rolls_back_only_new_resources(snapshot, docker, tmp_path):
    docker.volumes["unrelated"] = b"keep"
    docker.git = "different refs\n"
    episode = Episode("natural-looking", "dur", tmp_path / "ev2")
    with pytest.raises(InfrastructureError, match="guest git"):
        episode.restore(snapshot)
    assert set(docker.volumes) == {"unrelated"} and docker.networks == set()
    assert not [k for k in docker.containers if k.startswith(PROJECT + "-s1")]
    assert episode.resources["restore_failure"]["rollback"]["failed"] == [] and episode.resources["ready"] is False


def test_extraction_failure_leaves_nothing_behind(snapshot, docker, tmp_path):
    docker.fail_extract = True
    episode = Episode("natural-looking", "dur", tmp_path / "ev2")
    with pytest.raises(InfrastructureError, match="extraction"):
        episode.restore(snapshot)
    assert docker.volumes == {} and docker.networks == set()
