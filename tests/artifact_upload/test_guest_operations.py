"""Guest operation catalog and transactions against a real local git repo (no Docker, no model)."""
import base64
import subprocess

import pytest

from eval.artifact_upload.guest_operations import (
    DEFAULT_CATALOG, Catalog, GuestTransactionStop, LocalGuest, TransactionLog, apply_setup, recover, run_transaction, setup_entry)


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "trunk")
    git(repo, "config", "user.email", "a@b.c")
    git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "c1")
    git(repo, "checkout", "-q", "--detach")
    (repo / "dirty.txt").write_text("uncommitted")
    home = tmp_path / "home"
    home.mkdir()
    return repo, LocalGuest(repo, home), TransactionLog(tmp_path / "tx"), home


def branch_req(name="main", attach=True, oid="git_create_branch"):
    return {"operation_id": oid, "kind": "git_create_branch", "args": {"name": name, "attach": attach}}


def test_branch_create_attach_real_and_later_git_agrees(world):
    repo, guest, log, _ = world
    head = git(repo, "rev-parse", "HEAD")
    res = run_transaction(guest, [branch_req()], log)
    assert res.ok and res.status == "committed"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and git(repo, "rev-parse", "main") == head
    assert (repo / "dirty.txt").read_text() == "uncommitted"
    assert "?? dirty.txt" in git(repo, "status", "--short")


def test_existing_ref_never_overwritten(world):
    repo, guest, log, _ = world
    git(repo, "branch", "main")
    res = run_transaction(guest, [branch_req()], log)
    assert not res.ok and res.failed_phase == "precondition" and res.status == "rolled_back"


@pytest.mark.parametrize("phase", ["execution", "verification", "persistence"])
def test_failure_rolls_back_and_names_phase(world, phase):
    repo, guest, log, _ = world
    head = git(repo, "rev-parse", "HEAD")

    def fault(p, op):
        if p == phase:
            if p == "execution":
                # let the mutation happen, then fail verification-like after execute: simulate by failing in hook later
                return
            raise RuntimeError("injected %s token=abc123456789" % p)

    if phase == "execution":
        class Boom(LocalGuest):
            def guest_python(self, code):
                out = super().guest_python(code)
                if "symbolic-ref" in code and "HEAD" in code and '"branch"' in code:
                    raise RuntimeError("guest lost after mutation")
                return out
        guest = Boom(repo, guest.home)
    res = run_transaction(guest, [branch_req()], log, fault_hook=fault)
    assert not res.ok and res.status == "rolled_back" and res.failed_phase == phase
    assert git(repo, "rev-parse", "HEAD") == head and subprocess.run(["git", "show-ref", "--verify", "-q", "refs/heads/main"], cwd=repo).returncode
    assert git(repo, "symbolic-ref", "-q", "HEAD") if False else True
    assert "abc123456789" not in (res.error or "")
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=repo).returncode == 1  # detached again
    events = [e for e in log.load(res.transaction_id)["events"] if e.get("status") == "failed"]
    assert events and events[0]["phase"] == phase


def test_rollback_failure_stops_with_diagnostic(world):
    repo, guest, log, _ = world

    def fault(p, op):
        if p == "verification":
            raise RuntimeError("verify boom")
        if p == "rollback":
            raise RuntimeError("rollback boom")
    with pytest.raises(GuestTransactionStop) as e:
        run_transaction(guest, [branch_req()], log, fault_hook=fault)
    assert e.value.result.status == "stopped" and e.value.result.failed_phase == "verification"
    assert list(log.root.glob("diagnostic-*.json"))


def test_crash_recovery_rolls_back_incomplete_transaction(world):
    repo, guest, log, _ = world
    head = git(repo, "rev-parse", "HEAD")

    class Crash(BaseException):
        pass

    def fault(p, op):
        if p == "verification":
            raise Crash()
    with pytest.raises(Crash):
        run_transaction(guest, [branch_req()], log, fault_hook=fault)
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # mutation survived the "crash"
    results = recover(guest, log)
    assert results and results[0].status == "rolled_back"
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=repo).returncode == 1
    assert git(repo, "rev-parse", "HEAD") == head and not recover(guest, log)


def test_catalog_rejects_arbitrary_requests(world):
    _, guest, log, _ = world
    for req in ({"operation_id": "a", "kind": "shell", "args": {"command": "rm -rf /"}},
                {"operation_id": "git_create_branch", "kind": "git_create_branch", "args": {"name": "--force"}},
                {"operation_id": "git_create_branch", "kind": "git_create_branch", "args": {"name": "x", "extra": 1}},
                {"operation_id": "files_install_preset", "kind": "files_install_preset",
                 "args": {"path": ".ssh/authorized_keys", "content_b64": ""}},
                {"operation_id": "env_set_workspace", "kind": "env_set_workspace", "args": {"LD_PRELOAD": "x"}}):
        res = run_transaction(guest, [req], log)
        assert res.status == "rejected" and res.failed_phase == "proposal_validation"


def test_files_and_env_install_verify_rollback_and_no_overwrite(world):
    repo, guest, log, home = world
    data = base64.b64encode(b"bg").decode()
    files = {"operation_id": "files_install_preset", "kind": "files_install_preset",
             "args": {"path": ".local/share/backgrounds/a.txt", "content_b64": data}}
    env = {"operation_id": "env_set_workspace", "kind": "env_set_workspace", "args": {"DISPLAY": ":0", "TERM": "xterm"}}
    assert run_transaction(guest, [files, env], log).ok
    assert (home / ".local/share/backgrounds/a.txt").read_bytes() == b"bg"
    assert "export DISPLAY=':0'" in (home / ".config/workspace-profile/env.sh").read_text()
    again = run_transaction(guest, [files], log)
    assert not again.ok and again.failed_phase == "precondition"
    (home / ".local/share/backgrounds/a.txt").unlink()
    (home / ".config/workspace-profile/env.sh").unlink()
    res = run_transaction(guest, [files, env], log, fault_hook=lambda p, o: (_ for _ in ()).throw(RuntimeError("x")) if p == "persistence" else None)
    assert res.status == "rolled_back" and not (home / ".local/share/backgrounds/a.txt").exists()
    assert not (home / ".config/workspace-profile/env.sh").exists()


def test_dependencies_on_profile_and_rule_changes_are_accepted_but_unknown_are_not(world):
    repo, guest, log, _ = world
    first = {**branch_req("a", attach=False, oid="git_create_branch"), "change_id": "g1", "depends_on": ["profile-change", "rule-change"]}
    second = {**branch_req("b", attach=False, oid="git_create_branch:2"), "change_id": "g2", "depends_on": ["g1"]}
    ok = run_transaction(guest, [first, second], log, satisfied={"profile-change", "rule-change"})
    assert ok.ok
    # same chain without the caller-validated IDs, or ordered backwards, is rejected before any mutation
    git(repo, "branch", "-D", "a")
    git(repo, "branch", "-D", "b")
    assert run_transaction(guest, [first, second], log).status == "rejected"
    assert run_transaction(guest, [second, first], log, satisfied={"profile-change", "rule-change"}).status == "rejected"
    assert subprocess.run(["git", "show-ref", "--verify", "-q", "refs/heads/a"], cwd=repo).returncode != 0


def test_git_operations_are_pinned_to_the_approved_workspace(world, tmp_path):
    repo, guest, log, _ = world
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "trunk")
    git(other, "config", "user.email", "a@b.c")
    git(other, "config", "user.name", "t")
    git(other, "commit", "-q", "--allow-empty", "-m", "x")
    req = branch_req("zzz", attach=False)
    # a caller-chosen repo is never accepted, with or without a workspace
    for catalog in (DEFAULT_CATALOG, Catalog(workspace=str(repo))):
        bad = {**req, "args": {**req["args"], "repo": str(other)}}
        assert run_transaction(guest, [bad], log, catalog=catalog).status == "rejected"
    # the harness injects the approved workspace: the branch lands there, never in the guest cwd
    pinned = Catalog(workspace=str(other.resolve()))
    assert run_transaction(guest, [req], log, catalog=pinned).ok
    assert subprocess.run(["git", "show-ref", "--verify", "-q", "refs/heads/zzz"], cwd=other).returncode == 0
    assert subprocess.run(["git", "show-ref", "--verify", "-q", "refs/heads/zzz"], cwd=repo).returncode != 0
    # a symlinked workspace path is not canonical and is refused before any mutation
    link = tmp_path / "link"
    link.symlink_to(other)
    res = run_transaction(guest, [branch_req("yyy", attach=False)], log, catalog=Catalog(workspace=str(link)))
    assert res.status == "rolled_back" and res.failed_phase == "precondition"
    assert subprocess.run(["git", "show-ref", "--verify", "-q", "refs/heads/yyy"], cwd=other).returncode != 0
    with pytest.raises(ValueError):
        Catalog(workspace="/opt/../etc")


def test_catalog_entries_satisfy_packet_validator(world):
    from eval.artifact_upload.profiles import _catalog
    entries = Catalog(workspace="/work").describe()
    assert _catalog(entries) and {e["kind"] for e in entries} == set(DEFAULT_CATALOG.kinds())


def test_apply_setup_verifies_and_reports_required_failure(world):
    repo, guest, log, _ = world
    entries = [setup_entry(branch_req())]
    r = apply_setup(guest, entries, log)
    assert r.ok and r.entries[0]["status"] == "applied"
    assert apply_setup(guest, entries, log).entries[0]["status"] == "already-satisfied"
    bad = [setup_entry({"operation_id": "h", "kind": "git_create_branch", "args": {"name": "bad name"}})]
    r = apply_setup(guest, bad, log)
    assert not r.ok and r.failed_required == ["h"]
