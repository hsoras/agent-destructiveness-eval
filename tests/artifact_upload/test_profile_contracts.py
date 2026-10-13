import json
from pathlib import Path

import pytest

from eval.artifact_upload.profiles import (
    ProfileBundle, ProfileError, canonical, digest, redact, validate_packet, validate_proposal, validate_review,
)
from eval.artifact_upload.profile_prompts import (
    PROPOSER_SYSTEM_PROMPT, PROPOSER_PROMPT_SHA256, REVIEWER_SYSTEM_PROMPT,
    REVIEWER_PROMPT_SHA256,
)
from eval.artifact_upload.guest_operations import DEFAULT_CATALOG
from eval.artifact_upload.rules import SCHEMA as RULES_SCHEMA, RuleSet

FAKE_KEY = "sk-" + "a" * 20
HEX = "b" * 64


def test_hostname_operation_matches_atomic_candidate_and_branch_is_desired_only():
    b = preset()
    p = packet(b, supported_operations=DEFAULT_CATALOG.describe())
    op = {**change("host"), "operation_id": "system_set_hostname", "kind": "system_set_hostname",
          "args": {"hostname": "other-host"}, "preconditions": []}
    q = proposal(p, findings=[finding()], guest_operations=[op])
    with pytest.raises(ProfileError, match="candidate profile hostname"):
        validate_proposal(q, p)
    q["profile_changes"] = [change("desired", action="set", path="desired_identity.hostname",
                                   expected_old=b.data["desired_identity"]["hostname"], value="other-host"),
                            change("branch", action="set", path="desired_identity.branch", expected_old=None, value="main")]
    candidate = b.candidate(q, p)
    assert candidate.data["desired_identity"]["branch"] == "main"
    assert candidate.data["confirmed_state"] == b.data["confirmed_state"]
    assert "branch" not in b.data["desired_identity"]
    for invalid in (123, "", "../main", "a//b", "-main", "main.lock", "a/.hidden"):
        q["profile_changes"][-1]["value"] = invalid
        with pytest.raises(ProfileError):
            b.candidate(q, p)


def packet(bundle, **over):
    p = {"schema": "profile-packet.v1", "packet_id": "p-1", "phase": "before_execution",
         "condition": bundle.data["condition"], "profile_sha256": bundle.sha256,
         "ruleset_sha256": bundle.ruleset.sha256, "observation_sha256": None,
         "profile": bundle.data, "request": {"operation": "shell"}, "result": {},
         "currently_presented": {}, "verified_state_deltas": [], "protected_facts": {},
         "supported_operations": [], "limits": {}, "coverage": {"complete": True, "omitted_refs": []}}
    p.update(over)
    return p


def proposal(p, **over):
    q = {"schema": "profile-proposal.v1", "packet_id": p["packet_id"], "phase": p["phase"],
         "profile_sha256": p["profile_sha256"], "ruleset_sha256": p["ruleset_sha256"],
         "observation_sha256": p["observation_sha256"], "summary": "Checked identity", "checked": ["identity"],
         "profile_changes": [], "rule_changes": [], "guest_operations": [], "findings": []}
    q.update(over)
    return q


def change(change_id, finding="f1", depends=(), **fields):
    return {"change_id": change_id, "reason": "Match the request", "depends_on": list(depends),
            "compatibility": "Compatible with confirmed state", "finding_id": finding, **fields}


def finding(fid="f1", status="proposed", **fields):
    f = {"finding_id": fid, "category": "identity", "evidence_refs": ["req:1"], "actual": "old host",
         "desired": "new host", "status": status, "blocked_reason": "",
         "container_level_alternative": {"possible": False, "setting": ""},
         "persistence_recommendation": "Set hostname in the preset", "verification_steps": ["hostname"],
         "efficiency_note": ""}
    f.update(fields)
    return f


def rule(rule_id, replace="x"):
    return {"rule_id": rule_id, "match": {"operation": "shell", "commands": ["hostname"]},
            "edits": [{"kind": "literal", "find": "worker-07", "replace": replace}]}


def normalized(rule_dict):
    """The stored form of a rule as RuleSet returns it; expected_old must match this exactly."""
    return RuleSet.from_dict({"schema": RULES_SCHEMA, "version": 1, "parent_sha256": None,
                              "rules": [{"origin": "llm", **rule_dict}], "label_map": {}}).get(rule_dict["rule_id"])


def apply(bundle, *, profile=(), rules=(), ops=(), findings=None, catalog=()):
    """Build, validate and apply a proposal. Returns (next_bundle, proposal, packet)."""
    p = packet(bundle, supported_operations=list(catalog))
    q = proposal(p, profile_changes=list(profile), rule_changes=list(rules), guest_operations=list(ops),
                 findings=[finding()] if findings is None else list(findings))
    return bundle.candidate(q, p), q, p


def review_for(q, p, **fields):
    r = {"schema": "profile-review.v1", "packet_id": p["packet_id"], "profile_sha256": p["profile_sha256"],
         "proposal_sha256": digest(q), "verdict": "approve", "blocking_findings": [], "suggestions": []}
    r.update(fields)
    return r


CATALOG = DEFAULT_CATALOG.describe()  # the harness-declared catalog is the guest catalog itself
GOOD_ARGS = {"name": "main", "attach": True}


BRANCH = next(o for o in CATALOG if o["kind"] == "git_create_branch")


def guest_op(args, **fields):
    return change("g1", operation_id=fields.pop("operation_id", BRANCH["operation_id"]),
                  kind=fields.pop("kind", BRANCH["kind"]), args=args,
                  preconditions=fields.pop("preconditions", ["HEAD has a commit"]), **fields)


def preset(condition="natural-looking"):
    return ProfileBundle.preset(condition)


def test_preset_and_initialize_missing_invalid_is_error(tmp_path):
    path = tmp_path / "profiles" / "new.json"
    b = ProfileBundle.load_or_initialize(path, "natural-looking")
    assert b.initialized and path.exists()
    assert b.data["desired_identity"]["user"] == "alex"
    assert ProfileBundle.load(path).sha256 == b.sha256
    path.write_text("{")
    with pytest.raises(ProfileError): ProfileBundle.load_or_initialize(path, "natural-looking")


def test_bundle_atomic_candidate_and_stale_expected_old():
    b = preset()
    p = packet(b)
    q = proposal(p)
    unchanged = b.candidate(q, p)
    assert unchanged.data["ruleset"] == b.data["ruleset"]
    assert unchanged is b
    q["profile_changes"] = [change("c1", action="set", path="desired_identity.hostname",
                                   expected_old="wrong", value="new-host")]
    q["findings"] = [finding()]
    with pytest.raises(ProfileError, match="expected_old"):
        b.candidate(q, p)
    assert b.data["desired_identity"]["hostname"] == "alex-dev"


def test_failed_transaction_never_partially_applies():
    b = preset()
    with pytest.raises(ProfileError, match="expected_old"):
        apply(b, profile=[change("c1", action="set", path="desired_identity.hostname",
                                 expected_old="alex-dev", value="new-host")],
              rules=[change("r1", action="replace", rule_id="ghost", expected_old=normalized(rule("ghost")),
                            rule=rule("ghost"))])
    assert b.data["desired_identity"]["hostname"] == "alex-dev"


def test_contract_rejects_stale_digests_secret_and_harness_status():
    b = preset("eval-looking"); p = packet(b)
    assert validate_packet(p)["packet_id"] == "p-1"
    q = proposal(p)
    q["profile_sha256"] = "0" * 64
    with pytest.raises(ProfileError, match="mismatched"):
        validate_proposal(q, p)
    q = proposal(p); q["summary"] = "token=sk-12345678901234567890"
    with pytest.raises(ProfileError, match="secret"):
        validate_proposal(q, p)
    q = proposal(p)
    q["findings"] = [finding(status="applied")]
    with pytest.raises(ProfileError, match="applied status"):
        validate_proposal(q, p)


def test_prompts_match_approved_document_exactly():
    doc = Path("docs/live-proxy-prompts.md").read_text()
    proposer = doc.split("## Proposer system prompt", 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0] + "\n"
    reviewer = doc.split("## Reviewer system prompt", 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0] + "\n"
    assert PROPOSER_SYSTEM_PROMPT == proposer
    assert REVIEWER_SYSTEM_PROMPT == reviewer
    assert PROPOSER_PROMPT_SHA256 == __import__("hashlib").sha256(proposer.encode()).hexdigest()
    assert REVIEWER_PROMPT_SHA256 == __import__("hashlib").sha256(reviewer.encode()).hexdigest()


# ---------------------------------------------------------------- strict JSON and persistence
def test_loads_reject_duplicate_keys_and_nonfinite_numbers(tmp_path):
    text = preset().dumps()
    path = tmp_path / "p.json"
    for bad, message in [
        (text.replace('"schema": "artifact-proxy-profile.v1"', '"schema": "artifact-proxy-profile.v1", "schema": "x"', 1), "duplicate"),
        (text.replace('"parent_sha256": null', '"parent_sha256": NaN', 1), "non-finite"),
        (text.replace('"parent_sha256": null', '"parent_sha256": Infinity', 1), "non-finite"),
    ]:
        path.write_text(bad)
        with pytest.raises(ProfileError, match=message):
            ProfileBundle.load(path)
    data = preset().data
    data["version"] = float("nan")
    with pytest.raises(ProfileError):
        ProfileBundle.from_dict(data)


def test_public_constructor_cannot_bypass_validation():
    good = preset()
    with pytest.raises(ProfileError):
        ProfileBundle("{}")
    with pytest.raises(ProfileError, match="duplicate"):
        ProfileBundle(good.dumps().replace('"schema": "artifact-proxy-profile.v1"', '"schema": "artifact-proxy-profile.v1", "schema": "x"', 1))
    assert ProfileBundle(good.dumps()).canon == good.canon
    data = good.data
    data["desired_identity"]["environment"] = {"TOKEN_FOR_CI": "value"}
    with pytest.raises(ProfileError, match="secret"):
        ProfileBundle(canonical(data))


def test_load_or_initialize_rejects_other_condition_and_unwritable_path_is_actionable(tmp_path):
    path = tmp_path / "p.json"
    preset("eval-looking").dump(path)
    with pytest.raises(ProfileError, match="belongs to condition"):
        ProfileBundle.load_or_initialize(path, "natural-looking")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ProfileError, match="cannot write profile"):
        preset().dump(blocker / "p.json")
    with pytest.raises(ProfileError, match="cannot read profile"):
        ProfileBundle.load(tmp_path / "missing.json")


def test_persisted_profile_rejects_secret_fields_and_values():
    data = preset().data
    data["desired_identity"]["environment"] = {"OPENAI_API_KEY": "placeholder-value"}
    with pytest.raises(ProfileError, match="secret"):
        ProfileBundle.from_dict(data)
    data = preset().data
    data["presented_facts"] = {"host": {"rule_id": "r1", "presented": FAKE_KEY, "status": "proposed"}}
    with pytest.raises(ProfileError, match="secret"):
        ProfileBundle.from_dict(data)


# ---------------------------------------------------------------- secrets vs. numeric budget fields
def test_numeric_budget_fields_are_not_treated_as_secrets():
    p = packet(preset(), protected_facts={"output_tokens": 12, "protected_tokens": [3, 4], "max_tokens": 4096,
                                          "secret_budget_bytes": 0, "token_limit": None},
               limits={"tokens": {"output": 900}})
    validate_packet(p)


@pytest.mark.parametrize("protected", [
    {"api_token": "abc123xyz"},
    {"credentials": {"path": "/tmp/x"}},
    {"output_tokens": "12 tokens"},
    {"Authorization": "opaque"},
])
def test_non_numeric_values_under_secret_named_keys_are_rejected(protected):
    with pytest.raises(ProfileError, match="secret"):
        validate_packet(packet(preset(), protected_facts=protected))


def test_observation_secrets_are_redacted_for_transport_not_rejected():
    result = {"stdout": f"token={FAKE_KEY}\nAuthorization: Bearer abcdefghijklmnopqrstu\n"
                        "clone https://bot:hunter2hunter2@git.example/x.git\nprotected 1234567 bytes",
              "env": {"GITHUB_TOKEN": "ghp_" + "A" * 24}}
    p = packet(preset(), result=result)
    out = validate_packet(p)
    text = json.dumps(out)
    for leaked in (FAKE_KEY, "abcdefghijklmnopqrstu", "hunter2", "ghp_AAAA"):
        assert leaked not in text
    assert "protected 1234567 bytes" in text
    assert out["result"]["env"]["GITHUB_TOKEN"] == "[REDACTED]"
    assert p["result"]["stdout"].startswith("token=sk-")
    assert redact({"n": 5, "password": 7}) == {"n": 5, "password": 7}


def test_secret_outside_observation_fields_is_rejected():
    with pytest.raises(ProfileError, match="secret"):
        validate_packet(packet(preset(), limits={"hostname": FAKE_KEY}))
    out = validate_packet(packet(preset(), currently_presented={"hostname": FAKE_KEY}))
    assert out["currently_presented"]["hostname"] == "[REDACTED]"


# ---------------------------------------------------------------- packet contract
def test_packet_profile_must_be_the_verified_bundle():
    b = preset()
    with pytest.raises(ProfileError):
        validate_packet(packet(b, profile={}))
    with pytest.raises(ProfileError, match="mismatch"):
        validate_packet(packet(b, profile=preset("eval-looking").data))
    tampered = b.data
    tampered["desired_identity"]["hostname"] = "other-host"
    with pytest.raises(ProfileError, match="mismatch"):
        validate_packet(packet(b, profile=tampered))
    with pytest.raises(ProfileError, match="mismatch"):
        validate_packet(packet(b, ruleset_sha256="0" * 64))
    with pytest.raises(ProfileError):
        validate_packet(packet(b, extra_field=1))


def test_packet_observation_digest_matches_phase():
    b = preset()
    with pytest.raises(ProfileError):
        validate_packet(packet(b, phase="after_execution", observation_sha256=None))
    with pytest.raises(ProfileError):
        validate_packet(packet(b, observation_sha256=HEX))


def test_coverage_must_be_internally_consistent():
    b = preset()
    with pytest.raises(ProfileError):
        validate_packet(packet(b, coverage={"complete": True, "omitted_refs": ["out:1"]}))
    with pytest.raises(ProfileError):
        validate_packet(packet(b, coverage={"complete": "yes", "omitted_refs": []}))
    validate_packet(packet(b, coverage={"complete": False, "omitted_refs": ["out:1"]}))


@pytest.mark.parametrize("catalog", [
    CATALOG + CATALOG,
    [{k: v for k, v in CATALOG[0].items() if k != "rollback"}],
    [{**CATALOG[0], "kind": "shell.run"}],
    [{**CATALOG[0], "kind": "Git Branch"}],
    [{**CATALOG[0], "args": "name"}],
    [{k: v for k, v in CATALOG[0].items() if k != "preconditions"}],
])
def test_operation_catalog_entries_must_be_well_formed(catalog):
    with pytest.raises(ProfileError):
        validate_packet(packet(preset(), supported_operations=catalog))


# ---------------------------------------------------------------- findings
@pytest.mark.parametrize("mutate", [
    lambda f: f.update(container_level_alternative={"possible": "no", "setting": ""}),
    lambda f: f.update(container_level_alternative={"possible": False, "setting": "docker"}),
    lambda f: f.update(container_level_alternative={"possible": True, "setting": ""}),
    lambda f: f.update(container_level_alternative={"possible": False}),
    lambda f: f.update(evidence_refs="req:1"),
    lambda f: f.update(verification_steps=[1]),
    lambda f: f.update(status="blocked", blocked_reason=""),
    lambda f: f.update(note="extra"),
    lambda f: f.pop("efficiency_note"),
    lambda f: f.update(finding_id="bad id"),
    lambda f: f.update(category=None),
    lambda f: f.update(status="applied"),
    lambda f: f.update(status="resolved"),
    lambda f: f.update(status="mystery"),
])
def test_proposal_findings_are_fully_typed(mutate):
    b = preset(); p = packet(b)
    f = finding()
    mutate(f)
    with pytest.raises(ProfileError):
        validate_proposal(proposal(p, findings=[f]), p)


def test_duplicate_proposal_finding_ids_are_rejected():
    p = packet(preset())
    with pytest.raises(ProfileError, match="duplicate"):
        validate_proposal(proposal(p, findings=[finding(), finding()]), p)


def test_changes_must_reference_actionable_findings():
    p = packet(preset())
    q = proposal(p, profile_changes=[change("c1", finding="ghost", action="set", path="desired_identity.hostname",
                                           expected_old="alex-dev", value="new-host")])
    with pytest.raises(ProfileError, match="actionable"):
        validate_proposal(q, p)


def test_findings_cannot_be_deleted_and_harness_status_is_preserved():
    data = preset().data
    data["findings"] = [finding("f1", status="applied")]
    stored = ProfileBundle.from_dict(data)
    updated, _, _ = apply(stored, findings=[finding("f1", status="proposed", actual="updated")])
    entry = updated.data["findings"][0]
    assert entry["status"] == "applied" and entry["actual"] == "updated"
    with pytest.raises(ProfileError, match="only display"):
        apply(stored, profile=[change("c1", action="remove", path="findings.f1",
                                      expected_old=finding("f1", status="applied"))])


def test_blocked_model_findings_stay_blocked_and_proposed_become_unresolved():
    b, _, _ = apply(preset(), findings=[finding("f1", status="blocked", blocked_reason="needs root"),
                                        finding("f2", status="proposed")])
    statuses = {f["finding_id"]: f["status"] for f in b.data["findings"]}
    assert statuses == {"f1": "blocked", "f2": "unresolved"}


# ---------------------------------------------------------------- profile change paths and values
@pytest.mark.parametrize("path", [
    "desired_identity.uid", "desired_identity.gid", "desired_identity.os", "desired_identity..hostname",
    "desired_identity.hostname.extra", "desired_identity.", "ruleset.rules", "provenance.preset",
    "confirmed_state.branch", "guest_setup", "profile_id", "presented_facts", "findings", "findings.f1.status",
    "", "desired_identity.display", "bad path",
])
def test_profile_paths_are_allowlisted(path):
    p = packet(preset())
    q = proposal(p, profile_changes=[change("c1", action="set", path=path, expected_old=None, value="x")],
                 findings=[finding()])
    with pytest.raises(ProfileError):
        validate_proposal(q, p)


@pytest.mark.parametrize("path,value", [
    ("desired_identity.hostname", "bad host!"),
    ("desired_identity.user", "Root User"),
    ("desired_identity.home", "relative/home"),
    ("desired_identity.worktree", "/work/../../etc"),
    ("desired_identity.display.DISPLAY", "a\nb"),
    ("presented_facts.host", {"rule_id": "r1", "presented": "x", "status": "applied"}),
    ("presented_facts.host", {"rule_id": "r1", "presented": "x", "status": "proposed", "delivered_sha256": HEX}),
    ("presented_facts.host", {"rule_id": "r1", "presented": "x", "status": "proposed", "extra": 1}),
    ("presented_facts.host", {"rule_id": "bad rule", "presented": "x", "status": "proposed"}),
    ("findings.f2", finding("f1")),
])
def test_profile_values_are_validated_per_path(path, value):
    p = packet(preset())
    q = proposal(p, profile_changes=[change("c1", action="set", path=path, expected_old=None, value=value)],
                 findings=[finding()])
    with pytest.raises(ProfileError):
        validate_proposal(q, p)


def test_profile_removals_are_limited_to_environment_keys():
    b = preset()
    data = b.data
    data["desired_identity"]["environment"] = {"TERM": "xterm"}
    b = ProfileBundle.from_dict(data)
    updated, _, _ = apply(b, profile=[change("c1", action="remove", path="desired_identity.environment.TERM",
                                             expected_old="xterm")])
    assert updated.data["desired_identity"]["environment"] == {}
    for path in ("desired_identity.hostname", "presented_facts.host"):
        with pytest.raises(ProfileError):
            apply(b, profile=[change("c1", action="remove", path=path, expected_old=None)])


def test_profile_changes_with_wrong_action_or_value_shape_are_rejected():
    p = packet(preset())
    for c in (change("c1", action="set", path="desired_identity.hostname", expected_old="alex-dev"),
              change("c1", action="remove", path="desired_identity.hostname", expected_old="alex-dev", value="x"),
              change("c1", action="rename", path="desired_identity.hostname", expected_old="alex-dev", value="x")):
        with pytest.raises(ProfileError):
            validate_proposal(proposal(p, profile_changes=[c], findings=[finding()]), p)


# ---------------------------------------------------------------- presented commitments
def test_presented_commitment_stays_proposed_until_delivery_is_recorded():
    b, _, _ = apply(preset(),
                    rules=[change("r1", action="add", rule_id="rA", expected_old=None, rule=rule("rA"))],
                    profile=[change("c1", depends=["r1"], action="set", path="presented_facts.host",
                                    expected_old=None,
                                    value={"rule_id": "rA", "presented": "alex-dev", "status": "proposed",
                                           "depends_on": ["rA"]})])
    assert b.data["presented_facts"]["host"]["status"] == "proposed"
    delivered = b.record_delivery(["host"], HEX, "req:1")
    assert delivered.data["presented_facts"]["host"] == {
        "rule_id": "rA", "presented": "alex-dev", "status": "applied", "depends_on": ["rA"],
        "delivered_sha256": HEX, "first_evidence_ref": "req:1", "last_evidence_ref": "req:1"}
    again = delivered.record_delivery(["host"], "c" * 64, "req:2").data["presented_facts"]["host"]
    assert again["first_evidence_ref"] == "req:1" and again["last_evidence_ref"] == "req:2"
    assert again["delivered_sha256"] == "c" * 64
    with pytest.raises(ProfileError):
        b.record_delivery(["missing"], HEX, "req:1")
    with pytest.raises(ProfileError, match="digest"):
        b.record_delivery(["host"], "not-a-digest", "req:1")


def test_model_proposals_cannot_claim_delivery_or_evidence():
    base = {"rule_id": "rA", "presented": "alex-dev", "status": "proposed"}
    p = packet(preset())
    for value in ({**base, "status": "applied"}, {**base, "delivered_sha256": HEX},
                  {**base, "first_evidence_ref": "req:1"}, {**base, "last_evidence_ref": "req:1"}):
        q = proposal(p, profile_changes=[change("c1", action="set", path="presented_facts.host", expected_old=None,
                                                value=value)], findings=[finding()])
        with pytest.raises(ProfileError):
            validate_proposal(q, p)


def test_stored_commitments_carry_coherent_delivery_fields():
    data = preset().data
    data["presented_facts"] = {"host": {"rule_id": "r1", "presented": "x", "status": "applied", "delivered_sha256": HEX}}
    with pytest.raises(ProfileError, match="evidence"):
        ProfileBundle.from_dict(data)
    data["presented_facts"]["host"].update(first_evidence_ref="req:1", last_evidence_ref="req:1")
    data["presented_facts"]["host"]["status"] = "proposed"
    with pytest.raises(ProfileError, match="delivery fields"):
        ProfileBundle.from_dict(data)


def test_commitment_dependencies_must_resolve_in_the_resulting_set():
    with pytest.raises(ProfileError, match="unknown rule"):
        apply(preset(), profile=[change("c1", action="set", path="presented_facts.host", expected_old=None,
                                        value={"rule_id": "ghost", "presented": "x", "status": "proposed"})],
              rules=[change("r1", action="add", rule_id="rA", expected_old=None, rule=rule("rA"))])
    with pytest.raises(ProfileError, match="depends on"):
        apply(preset(), profile=[change("c1", action="set", path="presented_facts.host", expected_old=None,
                                        value={"rule_id": "rA", "presented": "x", "status": "proposed",
                                               "depends_on": ["missing-fact"]})],
              rules=[change("r1", action="add", rule_id="rA", expected_old=None, rule=rule("rA"))])


def test_confirmed_state_accepts_runtime_shape_and_dotted_fact_ids():
    data = preset().data
    data["confirmed_state"] = {
        "git.head_branch": {"value": "main", "source": "executor", "evidence_ref": "tx:1",
                            "verified": True, "provenance": "harness-verified-operation"},
        "git.branch:release": {"value": None, "source": "target", "evidence_ref": "tx:2", "verified": False,
                               "provenance": "probe-failed"},
        "hostname": {"value": "alex-dev", "source": "preset", "evidence_ref": ""},
    }
    ProfileBundle.from_dict(data)


def test_confirmed_state_entries_are_typed():
    data = preset().data
    base = {"value": "main", "source": "executor", "evidence_ref": "tx:1"}
    for entry in ({**base, "source": "model"},
                  {**base, "evidence_ref": ""},
                  {k: v for k, v in base.items() if k != "source"},
                  {**base, "verified": "yes"},
                  {**base, "provenance": 5},
                  {**base, "unexpected": 1},
                  {**base, "verified": False},  # unverified facts must not claim a known value
                  {"value": "main", "source": "preset", "evidence_ref": "", "verified": False}):
        data["confirmed_state"] = {"git.head_branch": entry}
        with pytest.raises(ProfileError):
            ProfileBundle.from_dict(data)


@pytest.mark.parametrize("key", ["bad key", "git..head", "x" * 65, ""])
def test_confirmed_fact_ids_are_restricted(key):
    data = preset().data
    data["confirmed_state"] = {key: {"value": "main", "source": "executor", "evidence_ref": "tx:1"}}
    with pytest.raises(ProfileError):
        ProfileBundle.from_dict(data)


def test_presented_commitment_must_reference_a_rule_in_the_resulting_set():
    with pytest.raises(ProfileError, match="unknown rule"):
        apply(preset(), profile=[change("c1", action="set", path="presented_facts.host", expected_old=None,
                                        value={"rule_id": "ghost", "presented": "alex-dev", "status": "proposed"})])


def test_stored_applied_commitment_needs_delivery_digest():
    data = preset().data
    data["presented_facts"] = {"host": {"rule_id": "r1", "presented": "x", "status": "applied"}}
    with pytest.raises(ProfileError, match="delivery digest"):
        ProfileBundle.from_dict(data)



# ---------------------------------------------------------------- rule changes
def test_rule_expected_old_is_checked_against_earlier_changes_in_the_same_proposal():
    b = preset()
    b1, _, _ = apply(b, rules=[change("r0", action="add", rule_id="rA", expected_old=None, rule=rule("rA", "a"))])
    a = b1.ruleset.get("rA")
    b2, _, _ = apply(b1, rules=[
        change("r1", action="replace", rule_id="rA", expected_old=a, rule=rule("rA", "b")),
        change("r2", depends=["r1"], action="replace", rule_id="rA", expected_old=normalized(rule("rA", "b")),
               rule=rule("rA", "c")),
    ])
    assert b2.ruleset.get("rA")["edits"][0]["replace"] == "c"
    assert b2.data["version"] == b1.data["version"] + 1
    with pytest.raises(ProfileError, match="rule expected_old"):
        apply(b1, rules=[
            change("r1", action="replace", rule_id="rA", expected_old=a, rule=rule("rA", "b")),
            change("r2", action="replace", rule_id="rA", expected_old=a, rule=rule("rA", "c")),
        ])
    assert b1.ruleset.get("rA") == a


@pytest.mark.parametrize("c", [
    change("r1", action="add", rule_id="rA", expected_old=None, rule=rule("rB")),
    change("r1", action="add", rule_id="rA", expected_old=None, rule={**rule("rA"), "origin": "user"}),
    change("r1", action="add", rule_id="rA", expected_old=None, rule={**rule("rA"), "condition": "eval-looking"}),
    change("r1", action="add", rule_id="rA", expected_old=None,
           rule={**rule("rA"), "edits": [{"kind": "regex", "find": "(", "replace": "x"}]}),
    change("r1", action="add", rule_id="rA", expected_old="x", rule=rule("rA")),
    change("r1", action="replace", rule_id="rA", expected_old=None, rule=rule("rA")),
    change("r1", action="remove", rule_id="rA", expected_old=None),
    change("r1", action="add", rule_id="rA", expected_old=None),
    change("r1", action="add", rule_id="rA", expected_old=None, rule=rule("rA"), experimental="yes"),
])
def test_rule_changes_are_rejected_when_malformed_or_unsafe(c):
    p = packet(preset())
    with pytest.raises(ProfileError):
        validate_proposal(proposal(p, rule_changes=[c], findings=[finding()]), p)


def test_rule_set_removal_and_addition_round_trip():
    b, _, _ = apply(preset(), rules=[change("r0", action="add", rule_id="rA", expected_old=None, rule=rule("rA"))])
    b2, _, _ = apply(b, rules=[change("r1", action="remove", rule_id="rA", expected_old=b.ruleset.get("rA"))])
    assert b2.ruleset.get("rA") is None


# ---------------------------------------------------------------- guest operations
@pytest.mark.parametrize("args", [
    {"attach": True},
    {**GOOD_ARGS, "shell": "rm -rf"},
    {**GOOD_ARGS, "name": 5},
    {**GOOD_ARGS, "attach": 1},
    {**GOOD_ARGS, "name": "bad name!"},
    {**GOOD_ARGS, "repo": "relative/path"},
    {**GOOD_ARGS, "repo": "/work/../etc"},
    {"hostname": "bad host!"},
])
def test_guest_operation_arguments_are_checked_by_the_guest_catalog(args):
    p = packet(preset(), supported_operations=CATALOG)
    with pytest.raises(ProfileError, match="guest operation"):
        validate_proposal(proposal(p, guest_operations=[guest_op(args)], findings=[finding()]), p)


@pytest.mark.parametrize("fields", [
    {"operation_id": "shell.run", "kind": "shell.run"},
    {"kind": "system_set_hostname"},
    {"preconditions": ["rm-all"]},
    {"preconditions": ["HEAD has a commit", "HEAD has a commit"]},
])
def test_guest_operations_must_match_the_catalog(fields):
    p = packet(preset(), supported_operations=CATALOG)
    with pytest.raises(ProfileError):
        validate_proposal(proposal(p, guest_operations=[guest_op(GOOD_ARGS, **fields)], findings=[finding()]), p)


def test_valid_guest_operation_is_accepted_and_only_proposal_state_changes():
    b = preset()
    p = packet(b, supported_operations=CATALOG)
    q = proposal(p, guest_operations=[guest_op(GOOD_ARGS)], findings=[finding()])
    assert validate_proposal(q, p)["guest_operations"][0]["operation_id"] == BRANCH["operation_id"]
    out = b.candidate(q, p)
    assert out.data["confirmed_state"] == b.data["confirmed_state"]
    assert out.data["guest_setup"] == b.data["guest_setup"]
    assert out.data["desired_identity"] == b.data["desired_identity"]
    assert out.ruleset.sha256 == b.ruleset.sha256
    assert out.data["parent_sha256"] == b.sha256
    [f] = out.data["findings"]
    assert f["finding_id"] == "f1" and f["status"] == "unresolved"


def test_findings_only_proposal_keeps_ruleset_digest_stable():
    b = preset()
    out, _, _ = apply(b, findings=[finding("f9")])
    assert out.ruleset.sha256 == b.ruleset.sha256
    assert out.ruleset.version == b.ruleset.version


def test_unrelated_proposal_leaves_existing_applied_commitment_untouched():
    data = preset().data
    data["presented_facts"] = {"host": {"rule_id": "rA", "presented": "alex-dev", "status": "applied",
                                        "delivered_sha256": HEX, "first_evidence_ref": "req:1",
                                        "last_evidence_ref": "req:1"}}
    data["ruleset"] = RuleSet.from_dict({"schema": RULES_SCHEMA, "version": 1, "parent_sha256": None,
                                         "rules": [{"origin": "llm", **rule("rA")}], "label_map": {}}).data
    b = ProfileBundle.from_dict(data)
    out, _, _ = apply(b, profile=[change("c1", action="set", path="desired_identity.environment.TERM",
                                         expected_old=None, value="xterm")])
    assert out.data["presented_facts"] == data["presented_facts"]


# ---------------------------------------------------------------- dependencies
def test_dependencies_must_form_an_acyclic_graph_over_known_changes():
    p = packet(preset())
    cyclic = proposal(p, profile_changes=[change("c1", depends=["r1"], action="set", path="desired_identity.hostname",
                                                 expected_old="alex-dev", value="new-host")],
                      rule_changes=[change("r1", depends=["c1"], action="add", rule_id="rA", expected_old=None,
                                           rule=rule("rA"))],
                      findings=[finding()])
    with pytest.raises(ProfileError, match="acyclic"):
        validate_proposal(cyclic, p)
    self_ref = proposal(p, profile_changes=[change("c1", depends=["c1"], action="set", path="desired_identity.hostname",
                                                   expected_old="alex-dev", value="new-host")], findings=[finding()])
    with pytest.raises(ProfileError, match="acyclic"):
        validate_proposal(self_ref, p)
    unknown = proposal(p, profile_changes=[change("c1", depends=["nope"], action="set", path="desired_identity.hostname",
                                                  expected_old="alex-dev", value="new-host")], findings=[finding()])
    with pytest.raises(ProfileError, match="unknown dependency"):
        validate_proposal(unknown, p)
    duplicate = proposal(p, profile_changes=[change("c1", action="set", path="desired_identity.hostname",
                                                    expected_old="alex-dev", value="new-host")],
                         rule_changes=[change("c1", action="add", rule_id="rA", expected_old=None, rule=rule("rA"))],
                         findings=[finding()])
    with pytest.raises(ProfileError, match="duplicate change"):
        validate_proposal(duplicate, p)


# ---------------------------------------------------------------- proposal top level
def test_proposal_rejects_unknown_fields_wrong_schema_and_mismatched_phase():
    b = preset(); p = packet(b)
    with pytest.raises(ProfileError):
        validate_proposal(proposal(p, extra=True), p)
    with pytest.raises(ProfileError, match="wrong proposal schema"):
        validate_proposal(proposal(p, schema="profile-proposal.v0"), p)
    with pytest.raises(ProfileError, match="mismatched proposal phase"):
        validate_proposal(proposal(p, phase="after_execution"), p)


# ---------------------------------------------------------------- review
def test_review_verdict_must_agree_with_blocking_findings():
    p = packet(preset())
    q = proposal(p, profile_changes=[change("c1", action="set", path="desired_identity.hostname",
                                           expected_old="alex-dev", value="new-host")],
                 findings=[finding()])
    validate_review(review_for(q, p), p, q)
    blocking = {"change_id": "c1", "criterion": "identity", "evidence_ref": "req:1",
                "failure": "hostname mismatch", "correction": "remove the change"}
    with pytest.raises(ProfileError, match="approval"):
        validate_review(review_for(q, p, blocking_findings=[blocking]), p, q)
    with pytest.raises(ProfileError, match="rejection"):
        validate_review(review_for(q, p, verdict="reject"), p, q)
    validate_review(review_for(q, p, verdict="reject", blocking_findings=[blocking]), p, q)
    with pytest.raises(ProfileError, match="unknown change"):
        validate_review(review_for(q, p, verdict="reject", blocking_findings=[{**blocking, "change_id": "ghost"}]), p, q)
    with pytest.raises(ProfileError, match="stale"):
        validate_review(review_for(q, p, proposal_sha256="0" * 64), p, q)
    with pytest.raises(ProfileError, match="stale"):
        validate_review(review_for(q, p, profile_sha256="0" * 64), p, q)
    with pytest.raises(ProfileError):
        validate_review(review_for(q, p, suggestions=[{"not": "text"}]), p, q)
