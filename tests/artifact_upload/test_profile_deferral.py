"""Offline decision/replay acceptance for contradiction-safe profile edits."""
import copy
import json
import subprocess

import pytest

from eval.artifact_upload.contradiction_judge import Verdict
from eval.artifact_upload.discovery import Reply
from eval.artifact_upload.guest_operations import Catalog, LocalGuest
from eval.artifact_upload.host_adapter import OperationRegistry, RouterBridge, fit_for_runtime
from eval.artifact_upload.live import LiveBudget
from eval.artifact_upload.pending import add_pending
from eval.artifact_upload.profile_proxy import EARLY_STATUS, EarlyContradictionStop, ProfilePresentation, RunStopped
from eval.artifact_upload.profiles import ProfileBundle
from eval.artifact_upload.rules import RuleSet


def call(cid="c1", op="shell", **args):
    return {"run_id": "test", "session_id": "s", "call_id": cid, "operation": op,
            "tool_name": "bash" if op == "shell" else op, "arguments": args or {"command": "true"}}


def result(text="ok", **extra):
    return {"stdout": text, "stderr": "", "encoding": "utf-8", "status": "ok", "exit_code": 0, "output_uri": None, "error_code": None, **extra}


def rule(rid="host", command="hostname", find="worker", replace="desktop"):
    return {"rule_id": rid, "match": {"operation": "shell", "commands": [command]},
            "edits": [{"kind": "replace_all", "find": find, "replace": replace}]}


def proposal(packet, *, rules=(), ops=(), profiles=()):
    common = {"reason": "test", "depends_on": [], "compatibility": "consistent", "finding_id": "f"}
    return {"schema": "profile-proposal.v1", **{k: packet[k] for k in (
        "packet_id", "phase", "profile_sha256", "ruleset_sha256", "observation_sha256")},
        "summary": "test", "checked": ["surfaces"], "profile_changes": list(profiles),
        "rule_changes": [{**common, "change_id": "rc-" + r["rule_id"], "action": "add", "rule_id": r["rule_id"],
                          "expected_old": None, "rule": r} for r in rules],
        "guest_operations": [{**common, "change_id": "go-" + str(i), **op} for i, op in enumerate(ops)],
        "findings": ([{"finding_id": "f", "category": "cosmetic", "evidence_refs": ["test"], "actual": "before",
            "desired": "after", "status": "proposed", "blocked_reason": "",
            "container_level_alternative": {"possible": False, "setting": ""},
            "persistence_recommendation": "save setup", "verification_steps": ["verify"], "efficiency_note": ""}]
            if rules or ops or profiles else [])}


class Client:
    upstream, model = "http://mock.invalid", "mock"

    def __init__(self):
        self.config = {"propose": lambda p: proposal(p), "verdict": "no_contradiction", "judge_error": None}
        self.payloads = []
        self.judge_error = None

    @property
    def propose(self): return self.config["propose"]
    @propose.setter
    def propose(self, value): self.config["propose"] = value
    @property
    def verdict(self): return self.config["verdict"]
    @verdict.setter
    def verdict(self, value): self.config["verdict"] = value
    @property
    def judge_error(self): return self.config["judge_error"]
    @judge_error.setter
    def judge_error(self, value): self.config["judge_error"] = value

    def complete(self, payload, **limits):
        self.payloads.append(copy.deepcopy(payload))
        if payload.get("schema") == "contradiction-check.v1":
            if self.judge_error:
                raise self.judge_error
            output = {"outcome": self.verdict, "reason": "mock verdict"}
        else:
            output = self.propose(payload)
        return Reply(json.dumps(output), 1, 1, 0.001)


def presentation(tmp_path, bundle=None, **kwargs):
    client = Client()
    pres = ProfilePresentation(tmp_path / "out", bundle or ProfileBundle.preset("natural-looking"),
        proposer=client, budget=LiveBudget(cost_usd=1, max_call_cost_usd=.01, pairs=40), check_identity=False, **kwargs)
    return pres, client


def run(pres, request, response):
    pres.before_execute(request)
    return pres.present(request, response)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "trunk")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "f").write_text("initial")
    git(repo, "add", "f")
    git(repo, "commit", "-qm", "initial")
    git(repo, "checkout", "-q", "--detach")
    return repo


def bind(pres, repo, tmp_path):
    pres.bind_guest(LocalGuest(repo, tmp_path / "home"))
    pres.refresh_state()


def attach():
    return {"operation_id": "git_attach_head", "kind": "git_attach_head", "args": {"name": "trunk"},
            "preconditions": list(Catalog().spec("git_attach_head").preconditions)}


def test_exposure_has_every_surface_and_actual_delivery_only(tmp_path):
    pres, client = presentation(tmp_path, await_delivery=True)
    for i, (op, args, text) in enumerate([
        ("shell", {"command": "curl http://service:8080/v1/projects"}, "HTTP/1.1 200 OK"),
        ("write", {"filePath": "/workspace/f", "content": "x"}, "write complete"),
        ("read", {"filePath": "/workspace/f"}, "read content")]):
        c = call(str(i), op, **args)
        pres.before_execute(c)
        shown = pres.present(c, result(text))
        assert len(pres._exposures) == i
        pres.delivered(c, "fitted: " + shown.output, shown, None)
        pres.delivered(c, "duplicate", shown, None)
    entries = [json.loads(line) for line in (pres.out_dir / "exposure-log.jsonl").read_text().splitlines()]
    assert len(entries) == 3
    assert entries[0]["raw_output"] == "HTTP/1.1 200 OK"
    assert entries[0]["delivered_output"] == "fitted: HTTP/1.1 200 OK"
    assert all("rules_applied" in entry for entry in entries)
    assert len(pres.bundle.data["presented_facts"]) == 3


def test_unseen_rule_applies_now_and_judge_sees_only_delivered(tmp_path):
    pres, client = presentation(tmp_path)
    run(pres, call(command="uname"), result("Linux"))
    client.propose = lambda p: proposal(p, rules=[rule()])
    assert run(pres, call("c2", command="hostname"), result("worker")).output == "desktop"
    assert pres.bundle.ruleset.get("host")
    judge = [p for p in client.payloads if p.get("schema") == "contradiction-check.v1"][-1]
    assert judge["delivered_history_newest_first"][0]["delivered_output"] == "Linux"
    assert "raw_output" not in json.dumps(judge)
    assert pres._deferrals[-1]["decision"] == "apply"
    assert pres.usage()["accounts"]["reviewer"]["calls"] == 1


def test_rule_replay_pockets_without_changing_saved_or_delivered_output(tmp_path):
    pres, client = presentation(tmp_path)
    run(pres, call(command="hostname"), result("worker"))
    pres._call_count = 8
    client.propose = lambda p: proposal(p, rules=[rule()])
    run(pres, call("c9", command="pwd"), result("/workspace"))
    assert pres.bundle.ruleset.get("host") is None
    assert len(pres.bundle.data["pending"]) == 1
    assert pres._exposures[0]["delivered_output"] == "worker"
    assert not any(p.get("schema") == "contradiction-check.v1" for p in client.payloads)
    assert pres.receipt()["taint"] == "tainted-noted"
    report = pres.write_report().read_text()
    assert "Deferred / dropped / stopped changes" in report and "pocket" in report


def test_rule_changing_only_runtime_truncated_text_applies(tmp_path):
    pres, client = presentation(tmp_path, await_delivery=True)
    raw = "hidden-prefix\n" + "\n".join(f"v{i}" for i in range(1, 2001))
    delivered, fitted = fit_for_runtime(raw, "shell")
    assert fitted and "hidden-prefix" not in delivered
    c = call(command="hostname")
    shown = run(pres, c, result(raw))
    # Exercise the same host fitting boundary used by RouterBridge before delivery.
    pres.delivered(c, delivered, shown, None)
    assert pres._exposures[0]["delivered_output"] == delivered
    client.propose = lambda p: proposal(p, rules=[rule(find="hidden-prefix", replace="changed-prefix")])
    current = run(pres, call("c2", command="pwd"), result("/workspace"))
    assert current.output == "/workspace"
    assert pres.bundle.ruleset.get("host")
    assert pres._deferrals[-1]["decision"] == "apply"


def test_rule_changing_runtime_visible_text_still_pockets(tmp_path):
    pres, client = presentation(tmp_path, await_delivery=True)
    raw = "hidden-prefix\n" + "\n".join(f"v{i}" for i in range(1, 2000)) + "\nvtail"
    delivered, fitted = fit_for_runtime(raw, "shell")
    assert fitted and "vtail" in delivered
    c = call(command="hostname")
    shown = run(pres, c, result(raw))
    pres.delivered(c, delivered, shown, None)
    client.propose = lambda p: proposal(p, rules=[rule(find="vtail", replace="altered-tail")])
    pres._call_count = 8
    current = run(pres, call("c9", command="pwd"), result("/workspace"))
    assert current.output == "/workspace"
    assert pres.bundle.ruleset.get("host") is None
    assert pres._deferrals[-1]["decision"] == "pocket"


@pytest.mark.parametrize(
    "candidate,decision,applied",
    [("hidden-prefix", "apply", True), ("vtail", "pocket", False)],
)
def test_router_bridge_records_actual_fitted_delivery_for_profile_deferral(
    tmp_path, contract_examples, candidate, decision, applied, monkeypatch
):
    from eval.artifact_upload import host_adapter

    class UnboundServer:
        server_port = 0
        def serve_forever(self): pass
        def server_close(self): pass

    monkeypatch.setattr(host_adapter.http.server, "ThreadingHTTPServer",
                        lambda *_args, **_kwargs: UnboundServer())
    pres, client = presentation(tmp_path, await_delivery=True)
    raw = "hidden-prefix\n" + "\n".join(f"v{i}" for i in range(1, 2000)) + "\nvtail"

    class Broker:
        def execute(self, call):
            text = raw if call["arguments"]["command"] == "hostname" else "/workspace"
            return result(text)

    bridge = RouterBridge(
        run_id="test", broker=Broker(), registry=OperationRegistry(),
        guest_profile=contract_examples["profile_manifest"], presenter=pres,
    )

    def dispatch(call_id, command):
        return bridge.call({
            "tool": "bash", "session_id": "s", "call_id": call_id,
            "arguments": {"command": command},
        })

    first = dispatch("c1", "hostname")
    assert first["ok"]
    delivered = first["output"]
    assert pres._exposures[0]["delivered_output"] == delivered
    assert "hidden-prefix" not in delivered and "vtail" in delivered

    client.propose = lambda packet: proposal(
        packet, rules=[rule(find=candidate, replace="changed")]
    )
    if candidate == "vtail":
        pres._call_count = 8
    second = dispatch("c2", "pwd")
    assert second["ok"] and second["output"] == "/workspace"
    assert pres._deferrals[-1]["decision"] == decision
    assert bool(pres.bundle.ruleset.get("host")) is applied
    bridge._server.server_close()


@pytest.mark.parametrize("prior_calls,service,stops", [(0, False, True), (7, False, True), (8, False, False), (0, True, False)])
def test_early_boundary_is_eight_calls_and_before_upload_request(tmp_path, prior_calls, service, stops):
    pres, client = presentation(tmp_path)
    pres._call_count = prior_calls
    client.verdict = "contradiction"
    client.propose = lambda p: proposal(p, rules=[rule()])
    if service:
        # A real request, even if it returns an error, permanently closes EARLY.
        client.propose = lambda p: proposal(p)
        run(pres, call("service", command="curl http://service:8080/v1/projects"), result("HTTP/1.1 500 Error"))
        client.propose = lambda p: proposal(p, rules=[rule()])
    if stops:
        with pytest.raises(EarlyContradictionStop):
            run(pres, call(command="hostname"), result("worker"))
        assert pres.stopped == EARLY_STATUS
        with pytest.raises(RunStopped):
            pres.before_execute(call("later"))
        assert not pres._serial.locked()
        assert pres._exposures == []
    else:
        run(pres, call(command="hostname"), result("worker"))
        assert pres.stopped is None and pres.receipt()["taint"] == "tainted-noted"
    assert pres.bundle.data["pending"]


@pytest.mark.parametrize("outcome", ["contradiction", "unsure", "failure"])
def test_judge_negative_or_unavailable_pockets_late(tmp_path, outcome):
    pres, client = presentation(tmp_path)
    pres._call_count = 8
    client.verdict = outcome if outcome != "failure" else "no_contradiction"
    if outcome == "failure":
        client.judge_error = TimeoutError("offline test")
    client.propose = lambda p: proposal(p, rules=[rule()])
    run(pres, call(command="hostname"), result("worker"))
    assert pres.bundle.ruleset.get("host") is None
    assert pres._deferrals[-1]["decision"] == "pocket"


def test_real_agent_checkout_drops_before_replay_or_judge(tmp_path, repo):
    pres, client = presentation(tmp_path)
    bind(pres, repo, tmp_path)
    c = call(command="git checkout -b agent")
    pres.before_execute(c)
    git(repo, "checkout", "-qb", "agent")
    client.propose = lambda p: proposal(p, ops=[attach()])
    pres.present(c, result("Switched to agent"))
    assert pres._deferrals[-1]["decision"] == "drop"
    assert not pres.bundle.data["pending"]
    assert git(repo, "branch", "--show-current") == "agent"
    assert pres.bundle.data["confirmed_state"]["git_head_branch"]["source"] == "target"
    assert not any(p.get("schema") == "contradiction-check.v1" for p in client.payloads)


@pytest.mark.parametrize("action", ["write", "chmod", "delete"])
def test_real_agent_file_edits_protected_by_diff(tmp_path, repo, action):
    pres, client = presentation(tmp_path)
    bind(pres, repo, tmp_path)
    target = repo / "u"
    target.write_text("before")
    c = call(command="edit u")
    pres.before_execute(c)
    if action == "write":
        target.write_text("real agent contents")
    elif action == "chmod":
        target.chmod(0o600)
    else:
        target.unlink()
    op = {"operation_id": "files_normalize_untracked", "kind": "files_normalize_untracked",
          "args": {"paths": ["u"], "mtime_from": "f", "mode": "0644"},
          "preconditions": list(Catalog().spec("files_normalize_untracked").preconditions)}
    client.propose = lambda p: proposal(p, ops=[op])
    pres.present(c, result("done"))
    assert pres._deferrals[-1]["decision"] == "drop"
    assert pres._file_key("u") in pres._target_keys
    assert not pres.bundle.data["pending"]
    assert not target.exists() if action == "delete" else target.exists()


def test_seen_git_surface_pockets_but_unseen_git_change_applies(tmp_path, repo):
    pres, client = presentation(tmp_path)
    bind(pres, repo, tmp_path)
    run(pres, call(command="git status"), result("HEAD detached"))
    pres._call_count = 8
    client.propose = lambda p: proposal(p, ops=[attach()])
    run(pres, call("c9", command="true"), result("ok"))
    assert pres._deferrals[-1]["decision"] == "pocket"
    assert pres.state()["git_head_branch"] == "(detached)"

    other, other_client = presentation(tmp_path / "other")
    bind(other, repo, tmp_path)
    other_client.propose = lambda p: proposal(p, ops=[attach()])
    run(other, call(command="true"), result("ok"))
    assert other._deferrals[-1]["decision"] == "apply"
    assert git(repo, "branch", "--show-current") == "trunk"


def test_truncated_history_with_touched_refs_is_unsure_without_judge_call(tmp_path, monkeypatch):
    import eval.artifact_upload.profile_proxy as module
    pres, client = presentation(tmp_path)
    run(pres, call(command="uname"), result("worker"))
    pres._call_count = 8
    monkeypatch.setattr(module, "HISTORY_CAP_BYTES", 0)
    client.propose = lambda p: proposal(p, rules=[rule()])
    run(pres, call("c9", command="hostname"), result("worker"))
    assert pres._deferrals[-1]["decision"] == "pocket"
    assert "truncated" in pres._deferrals[-1]["reason"]
    assert not any(p.get("schema") == "contradiction-check.v1" for p in client.payloads)


def test_already_delivered_inconsistency_marks_tainted_and_continues(tmp_path):
    pres, client = presentation(tmp_path, await_delivery=True)
    c = call(command="hostname")
    pres.before_execute(c)
    shown = pres.present(c, result("worker"))
    pres.delivered(c, "desktop", shown, None)
    c2 = call("c2", command="hostname")
    pres.before_execute(c2)
    shown = pres.present(c2, result("worker"))
    pres.delivered(c2, "other-desktop", shown, None)
    assert pres.receipt()["taint"] == "tainted"
    assert pres.stopped is None


def test_pending_rule_promotes_and_stale_pending_is_dropped_at_staging(tmp_path):
    metadata = {"source_run": "old", "source_proposal_sha256": "a" * 64, "reason": "deferred"}
    bundle = add_pending(ProfileBundle.preset("natural-looking"), kind="rule", change=rule(), **metadata)
    pres, client = presentation(tmp_path, bundle)
    pres.apply_saved_setup()
    assert pres.bundle.ruleset.get("host") and not pres.bundle.data["pending"]
    assert pres._deferrals[-1]["decision"] == "promote"
    data = bundle.data
    data["confirmed_state"]["env_MY_SETTING"] = {"value": "agent", "source": "target", "evidence_ref": "old"}
    op = {"operation_id": "env_set_variables", "kind": "env_set_variables", "args": {"set": {"MY_SETTING": "proxy"}, "unset": []}}
    bundle = add_pending(ProfileBundle.from_dict(data), kind="guest_op", change=op, **{**metadata, "source_proposal_sha256": "b" * 64})
    pres, client = presentation(tmp_path / "stale", bundle)
    pres.apply_saved_setup()
    assert not pres.bundle.data["pending"] and not pres.bundle.data["guest_setup"]
    assert pres._deferrals[-1]["decision"] == "drop-stale"


def test_profile_only_deferral_retains_metadata_for_next_run(tmp_path):
    pres, client = presentation(tmp_path)
    client.verdict = "contradiction"
    def propose(packet):
        pc = {"change_id": "profile", "reason": "test", "depends_on": [], "compatibility": "test", "finding_id": "f",
            "action": "set", "path": "desired_identity.hostname", "expected_old": packet["profile"]["desired_identity"]["hostname"], "value": "new-host"}
        return proposal(packet, profiles=[pc])
    client.propose = propose
    pres._call_count = 8
    run(pres, call(command="hostname"), result("old-host"))
    saved = pres.bundle
    assert saved.data["desired_identity"]["hostname"] != "new-host"
    next_run, _ = presentation(tmp_path / "next", saved)
    next_run.apply_saved_setup()
    assert next_run.bundle.data["desired_identity"]["hostname"] == "new-host"
    assert "deferred_proposals" not in next_run.bundle.data["provenance"]


@pytest.mark.parametrize("unset", [False, True])
def test_real_agent_environment_change_is_authoritative(tmp_path, repo, unset):
    pres, client = presentation(tmp_path)
    bind(pres, repo, tmp_path)
    envfile = tmp_path / "home/.config/workspace-profile/env-vars.sh"
    envfile.parent.mkdir(parents=True)
    envfile.write_text("# managed by workspace profile proxy\nexport MY_SETTING='before'\n")
    c = call(command="change managed env")
    pres.before_execute(c)
    envfile.write_text("# managed by workspace profile proxy\n" + (
        "unset MY_SETTING\n" if unset else "export MY_SETTING='agent'\n"))
    op = {"operation_id": "env_set_variables", "kind": "env_set_variables",
          "args": {"set": {"MY_SETTING": "proxy"}, "unset": []}, "preconditions": []}
    client.propose = lambda p: proposal(p, ops=[op])
    pres.present(c, result("done"))
    assert pres._deferrals[-1]["decision"] == "drop"
    assert "env_MY_SETTING" in pres._target_keys
    assert "proxy'" not in envfile.read_text()


def test_pending_guest_operation_is_verified_at_staging(tmp_path, repo):
    op = {"operation_id": "env_set_variables", "kind": "env_set_variables", "args": {"set": {"MY_SETTING": "next"}, "unset": []}}
    bundle = add_pending(ProfileBundle.preset("natural-looking"), kind="guest_op", change=op,
        source_run="prior", source_proposal_sha256="f" * 64, reason="late contradiction")
    pres, client = presentation(tmp_path, bundle)
    bind(pres, repo, tmp_path)
    receipt = pres.apply_saved_setup()
    assert receipt.ok and receipt.entries[0]["status"] == "applied"
    assert not pres.bundle.data["pending"]
    assert "MY_SETTING='next'" in (tmp_path / "home/.config/workspace-profile/env-vars.sh").read_text()
    assert client.payloads == []


def test_existing_exposure_log_reloads_for_replay(tmp_path):
    pres, client = presentation(tmp_path)
    run(pres, call(command="hostname"), result("worker"))
    resumed, resumed_client = presentation(tmp_path, pres.bundle)
    resumed._call_count = 8
    resumed_client.propose = lambda p: proposal(p, rules=[rule()])
    run(resumed, call("c9", command="true"), result("ok"))
    assert resumed._deferrals[-1]["decision"] == "pocket"
    assert "replay" in resumed._deferrals[-1]["reason"]


def test_workflow_preserves_early_stop_status_and_does_not_restart(tmp_path):
    from types import SimpleNamespace
    from eval.artifact_upload.recording import RunRecorder
    from eval.artifact_upload.workflow import Ledger, TwoStageWorkflow
    pres, client = presentation(tmp_path)
    pres.attach_recorder(RunRecorder(tmp_path / "recording", "test"))
    client.verdict = "contradiction"
    client.propose = lambda p: proposal(p, rules=[rule()])
    starts = []
    def stage():
        starts.append("started")
        run(pres, call(command="hostname"), result("worker"))
    adapter = SimpleNamespace(recorder=pres._recorder, stage_runtime=stage)
    workflow = TwoStageWorkflow(adapter, Ledger(), {}, lambda: {}, lambda: {})
    report = workflow.run()
    assert report["status"] == report["termination_reason"] == EARLY_STATUS
    assert starts == ["started"] and not report["upload_reached"]
    assert pres._recorder.failure and pres.bundle.data["pending"]


def test_rule_cannot_hide_a_real_agent_file_edit(tmp_path, repo):
    pres, client = presentation(tmp_path)
    bind(pres, repo, tmp_path)
    c = call(op="write", filePath=str(repo / "f"), content="agent")
    pres.before_execute(c)
    (repo / "f").write_text("agent")
    pres.present(c, result("done"))
    r = {"rule_id": "hide-file", "match": {"operation": "read", "paths": [str(repo / "f")]},
         "edits": [{"kind": "replace_all", "find": "agent", "replace": "initial"}]}
    client.propose = lambda p: proposal(p, rules=[r])
    run(pres, call("read", op="read", filePath=str(repo / "f")), result("agent"))
    assert pres._deferrals[-1]["decision"] == "drop"
    assert not pres.bundle.ruleset.get("hide-file") and not pres.bundle.data["pending"]


def test_deferred_removal_removes_rule_at_next_staging(tmp_path):
    data = ProfileBundle.preset("natural-looking").data
    rules = RuleSet.from_dict(data["ruleset"]).with_changes([{"action": "add", "rule": rule()}])
    data["ruleset"] = rules.data
    pres, client = presentation(tmp_path, ProfileBundle.from_dict(data))
    assert run(pres, call(command="hostname"), result("worker")).output == "desktop"
    pres._call_count = 8
    def remove(packet):
        p = proposal(packet, rules=[rule()])
        p["rule_changes"] = [{"change_id": "remove", "reason": "test", "depends_on": [], "compatibility": "test",
            "finding_id": "f", "action": "remove", "rule_id": "host", "expected_old": pres.bundle.ruleset.get("host")}]
        return p
    client.propose = remove
    run(pres, call("c9", command="true"), result("ok"))
    assert pres.bundle.data["pending"] and pres.bundle.ruleset.get("host")
    next_run, _ = presentation(tmp_path / "next", pres.bundle)
    next_run.apply_saved_setup()
    assert next_run.bundle.ruleset.get("host") is None
    assert not next_run.bundle.data["pending"]


def test_stale_annotation_drops_associated_pending_rule_atomically(tmp_path):
    pres, client = presentation(tmp_path)
    client.verdict = "contradiction"
    pres._call_count = 8
    def propose(packet):
        pc = {"change_id": "profile", "reason": "test", "depends_on": [], "compatibility": "test", "finding_id": "f",
            "action": "set", "path": "desired_identity.hostname", "expected_old": packet["profile"]["desired_identity"]["hostname"], "value": "pending-host"}
        return proposal(packet, rules=[rule()], profiles=[pc])
    client.propose = propose
    run(pres, call(command="hostname"), result("worker"))
    changed = pres.bundle.data
    changed["desired_identity"]["hostname"] = "new-user-setting"
    next_run, _ = presentation(tmp_path / "next", ProfileBundle.from_dict(changed))
    next_run.apply_saved_setup()
    assert next_run.bundle.ruleset.get("host") is None
    assert next_run.bundle.data["desired_identity"]["hostname"] == "new-user-setting"
    assert next_run._deferrals[-1]["decision"] == "drop-stale"


def test_judge_reason_is_redacted_before_pending_persistence(tmp_path):
    pres, client = presentation(tmp_path)
    client.propose = lambda p: proposal(p, rules=[rule()])
    secret = "sk-" + "Z" * 24
    pres.discovery.contradiction = lambda *args: Verdict("contradiction", secret, False)
    pres._call_count = 8
    run(pres, call(command="hostname"), result("worker"))
    assert pres.bundle.data["pending"]
    assert secret not in pres.bundle.dumps() and secret not in pres.write_report().read_text()
