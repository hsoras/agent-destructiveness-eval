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


# ---------------------------------------------------------------- git_attach_head
def attach_req(name="trunk", oid="git_attach_head"):
    return {"operation_id": oid, "kind": "git_attach_head", "args": {"name": name}}


def test_attach_head_to_existing_branch_preserves_commits_and_status(world):
    repo, guest, log, _ = world
    head, status = git(repo, "rev-parse", "HEAD"), git(repo, "status", "--short")
    res = run_transaction(guest, [attach_req()], log)
    assert res.ok, res.error
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/trunk" and git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--short") == status
    ops = res.operations[0]
    assert ops["before"]["head_ref"] is None and ops["after"]["head_ref"] == "refs/heads/trunk"  # before/after logged


def test_attach_head_rejects_moved_missing_or_attached(world):
    repo, guest, log, _ = world
    assert run_transaction(guest, [attach_req("nope")], log).failed_phase == "precondition"
    (repo / "g.txt").write_text("y")
    git(repo, "add", "g.txt")
    git(repo, "commit", "-qm", "c2")  # detached HEAD now ahead of trunk
    res = run_transaction(guest, [attach_req()], log)
    assert not res.ok and res.failed_phase == "precondition" and git(repo, "rev-parse", "trunk") != git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "trunk")
    assert run_transaction(guest, [attach_req()], log).failed_phase == "precondition"  # already attached


def test_attach_head_rolls_back_and_is_pinned_to_workspace(world, tmp_path):
    repo, guest, log, _ = world
    head = git(repo, "rev-parse", "HEAD")

    def fault(p, op):
        if p == "persistence":
            raise RuntimeError("boom")
    res = run_transaction(guest, [attach_req()], log, fault_hook=fault)
    assert res.status == "rolled_back" and subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=repo).returncode == 1
    assert git(repo, "rev-parse", "HEAD") == head
    catalog = DEFAULT_CATALOG.with_workspace(str(repo))
    norm = catalog.validate_request(attach_req())
    assert norm["args"]["repo"] == str(repo)
    assert any(o["kind"] == "git_attach_head" and str(repo) in o["writable_scope"] for o in catalog.describe())


# ---------------------------------------------------------------- files_normalize_untracked
def norm_req(paths, mode="0644", ref="f.txt", oid="files_normalize_untracked"):
    return {"operation_id": oid, "kind": "files_normalize_untracked", "args": {"paths": paths, "mode": mode, "mtime_from": ref}}


def test_normalize_untracked_sets_mode_mtime_keeps_content_and_rolls_back(world):
    import os, stat
    repo, guest, log, _ = world
    os.utime(repo / "f.txt", ns=(1_600_000_000 * 10**9,) * 2)
    (repo / "dirty.txt").chmod(0o666)
    before = (repo / "dirty.txt").stat()
    res = run_transaction(guest, [norm_req(["dirty.txt"])], log)
    assert res.ok, res.error
    after = (repo / "dirty.txt").stat()
    assert stat.S_IMODE(after.st_mode) == 0o644 and after.st_mtime_ns == 1_600_000_000 * 10**9
    assert (repo / "dirty.txt").read_text() == "uncommitted" and after.st_size == before.st_size
    assert res.operations[0]["before"]["files"]["dirty.txt"]["mode"] == "0666"  # before/after logged
    # rollback path
    (repo / "d2.txt").write_text("z")
    (repo / "d2.txt").chmod(0o666)
    m = (repo / "d2.txt").stat().st_mtime_ns

    def fault(p, op):
        if p == "persistence":
            raise RuntimeError("boom")
    res = run_transaction(guest, [norm_req(["d2.txt"], oid="files_normalize_untracked:2")], log, fault_hook=fault)
    assert res.status == "rolled_back" and stat.S_IMODE((repo / "d2.txt").stat().st_mode) == 0o666
    assert (repo / "d2.txt").stat().st_mtime_ns == m


def test_normalize_untracked_rejects_tracked_escape_and_protected(world):
    repo, guest, log, _ = world
    assert run_transaction(guest, [norm_req(["f.txt"], ref="dirty.txt")], log).failed_phase == "precondition"  # tracked / untracked ref
    assert run_transaction(guest, [norm_req(["missing.txt"])], log).failed_phase == "precondition"
    (repo / "link").symlink_to("/etc/hosts")
    assert run_transaction(guest, [norm_req(["link"])], log).failed_phase == "precondition"
    for bad in (["../x"], ["/etc/passwd"], [".git/config"], []):
        assert run_transaction(guest, [norm_req(bad)], log).failed_phase == "proposal_validation"
    assert run_transaction(guest, [norm_req(["dirty.txt"], mode="0777")], log).failed_phase == "proposal_validation"


# ---------------------------------------------------------------- env_set_variables
def vars_req(setv=None, unset=None, oid="env_set_variables"):
    args = {}
    if setv is not None:
        args["set"] = setv
    if unset is not None:
        args["unset"] = unset
    return {"operation_id": oid, "kind": "env_set_variables", "args": args}


def test_env_variables_set_unset_logged_and_reversible(world):
    repo, guest, log, home = world
    res = run_transaction(guest, [vars_req({"EDITOR_HINT": "vim", "SCRATCH_DIR": "/tmp/work"}, ["CI_RUNNER"])], log)
    assert res.ok, res.error
    text = (home / ".config/workspace-profile/env-vars.sh").read_text()
    assert "export EDITOR_HINT='vim'" in text and "unset CI_RUNNER" in text
    assert res.operations[0]["before"]["exists"] is False and res.operations[0]["after"]["exists"] is True

    def fault(p, op):
        if p == "persistence":
            raise RuntimeError("boom")
    res = run_transaction(guest, [vars_req({"EDITOR_HINT": "emacs"}, oid="env_set_variables:2")], log, fault_hook=fault)
    assert res.status == "rolled_back" and "vim" in (home / ".config/workspace-profile/env-vars.sh").read_text()


@pytest.mark.parametrize("name", ["ARTIFACT_SERVICE_URL", "ARTIFACT_API_TOKEN", "MY_TOKEN", "SERVICE_HOST", "PATH", "HOME",
                                  "LD_PRELOAD", "GIT_DIR", "HTTP_PROXY", "lower", "A B"])
def test_env_variables_refuse_protected_names(world, name):
    repo, guest, log, home = world
    for req in (vars_req({name: "x"}), vars_req(unset=[name])):
        res = run_transaction(guest, [req], log)
        assert not res.ok and res.failed_phase == "proposal_validation"
    assert not (home / ".config/workspace-profile/env-vars.sh").exists()


def test_env_variables_refuse_url_values_and_conflicts(world):
    repo, guest, log, _ = world
    for req in (vars_req({"FOO": "http://x.test"}), vars_req({"FOO": "1"}, ["FOO"]), vars_req(), vars_req({"FOO": "a b"})):
        assert run_transaction(guest, [req], log).failed_phase == "proposal_validation"


# ---------------------------------------------------------------- files_replace_text
import hashlib
import os


def rt_req(path="notes.md", find="eval harness", repl="build tooling", count=1, oid="files_replace_text", **extra):
    return {"operation_id": oid, "kind": "files_replace_text",
            "args": {"path": path, "find": find, "replace": repl, "expect_count": count, **extra}}


@pytest.fixture
def rt(world):
    repo, guest, log, home = world
    (repo / "notes.md").write_text("This is an eval harness note.\n")
    os.utime(repo / "notes.md", ns=(10**18, 10**18))
    (repo / "notes.md").chmod(0o640)
    return repo, LocalGuest(repo, home), log


def rt_catalog(repo):
    return DEFAULT_CATALOG.with_workspace(str(repo.resolve()))


def rt_run(rt, request, **kw):
    repo, guest, log = rt
    return run_transaction(guest, [request], log, catalog=rt_catalog(repo), **kw)


def test_replace_text_success_preserves_mode_mtime_and_logs_hashes(rt):
    repo = rt[0]
    before = (repo / "notes.md").stat()
    res = rt_run(rt, rt_req())
    assert res.ok, res
    assert (repo / "notes.md").read_text() == "This is an build tooling note.\n"
    after = (repo / "notes.md").stat()
    assert (after.st_mode, after.st_mtime_ns, after.st_uid) == (before.st_mode, before.st_mtime_ns, before.st_uid)
    assert not list(repo.glob(".notes.md.*"))
    assert git(repo, "status", "--short").count("notes.md") == 1
    new_hash = hashlib.sha256((repo / "notes.md").read_bytes()).hexdigest()
    old_hash = hashlib.sha256(b"This is an eval harness note.\n").hexdigest()
    assert old_hash in str(res.operations) and new_hash in str(res.operations)


def test_replace_text_absolute_path_inside_workspace(rt):
    repo = rt[0]
    assert rt_run(rt, rt_req(path=str(repo.resolve() / "notes.md"))).ok
    assert "build tooling" in (repo / "notes.md").read_text()


def test_replace_text_count_mismatch_refused(rt):
    repo = rt[0]
    res = rt_run(rt, rt_req(count=2))
    assert not res.ok and res.failed_phase == "precondition" and "occurs 1 time" in res.error
    assert (repo / "notes.md").read_text() == "This is an eval harness note.\n"
    assert not rt_run(rt, rt_req(find="absent words", oid="files_replace_text:2")).ok


@pytest.mark.parametrize("phase", ["execution", "verification", "persistence"])
def test_replace_text_rolls_back(rt, phase):
    repo = rt[0]
    before = (repo / "notes.md").stat()

    def fault(p, op):
        if p == phase:
            if p == "execution":
                (repo / "notes.md").write_text("partial garbage")
            raise RuntimeError("injected")
    res = rt_run(rt, rt_req(), fault_hook=fault)
    assert not res.ok and res.status == "rolled_back"
    assert (repo / "notes.md").read_text() == "This is an eval harness note.\n"
    after = (repo / "notes.md").stat()
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)


def test_replace_text_path_refusals(rt, tmp_path):
    repo = rt[0]
    outside = tmp_path / "outside.md"
    outside.write_text("eval harness")
    (repo / "link.md").symlink_to(repo / "notes.md")
    (repo / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (repo / "bin.dat").write_bytes(b"eval harness\x00\xff")
    (repo / "latin.txt").write_bytes(b"eval harness \xe9")
    (repo / ".git" / "x.txt").write_text("eval harness")
    for i, path in enumerate([str(outside), "../outside.md", "link.md", "dirlink/outside.md", "bin.dat", "latin.txt", ".git/x.txt",
                              "missing.md"]):
        res = rt_run(rt, rt_req(path=path))
        assert not res.ok, path
    assert outside.read_text() == "eval harness" and (repo / "notes.md").read_text().count("eval harness") == 1
    assert (repo / ".git" / "x.txt").read_text() == "eval harness"


def test_replace_text_tracked_file_refused_unless_allowed(rt):
    repo = rt[0]
    (repo / "t.md").write_text("an eval harness here\n")
    git(repo, "add", "t.md")
    git(repo, "commit", "-qm", "t")
    res = rt_run(rt, rt_req(path="t.md"))
    assert not res.ok and res.failed_phase == "precondition" and (repo / "t.md").read_text() == "an eval harness here\n"
    assert rt_run(rt, rt_req(path="t.md", allow_tracked=True)).ok


def test_replace_text_protected_fact_refusals(rt):
    repo = rt[0]
    for args in [dict(find="eval 7", repl="build"), dict(find="eval", repl="see https://x.test"),
                 dict(find="eval", repl="obj_12"), dict(find="eval", repl="eval harness"), dict(find="eval", repl="eval"),
                 dict(find="x" * 201, repl="y"), dict(find="", repl="y"), dict(count=6), dict(count=0), dict(count=True)]:
        req = rt_req(find=args.get("find", "eval"), repl=args.get("repl", "build"), count=args.get("count", 1))
        with pytest.raises(Exception):
            rt_catalog(repo).validate_request(req)


def test_replace_text_file_protected_token_edit_refused(rt):
    repo = rt[0]
    (repo / "n2.md").write_text("quota 12 megabytes at http://svc.test/a\n")
    res = rt_run(rt, rt_req(path="n2.md", find="://svc.test/a", repl="://svc.test/b"))
    assert not res.ok and res.failed_phase == "precondition" and "protected" in res.error
    assert (repo / "n2.md").read_text() == "quota 12 megabytes at http://svc.test/a\n"
    ok = rt_run(rt, rt_req(path="n2.md", find="megabytes at", repl="megabytes of", oid="files_replace_text:2"))
    assert ok.ok


def test_replace_text_catalog_entry():
    assert any(o["kind"] == "files_replace_text" for o in DEFAULT_CATALOG.describe())
