import json

import pytest

from eval.artifact_upload.pending import add_pending, promote_pending
from eval.artifact_upload.profiles import ProfileBundle, ProfileError, VERSION


def rule(rid="deferred-rule", *, requires=None, replace="visible"):
    match = {"operation": "shell", "commands": ["printf *"]}
    if requires:
        match["requires"] = [{"fact": f} for f in requires]
    return {"rule_id": rid, "condition": "natural-looking", "match": match,
            "edits": [{"kind": "literal", "find": "visible", "replace": replace}]}


def guest_op():
    return {"operation_id": "env_set_variables", "kind": "env_set_variables",
            "args": {"set": {"MY_SETTING": "ok"}, "unset": []}}


def metadata(**kwargs):
    return {"source_run": "run-1", "source_proposal_sha256": "a" * 64, "reason": "contradiction", **kwargs}


def test_old_bundle_load_and_round_trip_preserves_pending(tmp_path):
    bundle = ProfileBundle.preset("natural-looking")
    old = bundle.data
    old.pop("pending")
    old["version"] = 1
    loaded = ProfileBundle.from_dict(old)
    assert loaded.data["version"] == VERSION == 2
    assert loaded.data["pending"] == []
    pending = add_pending(loaded, kind="rule", change=rule(), **metadata())
    path = tmp_path / "profile.json"
    pending.dump(path)
    loaded_again = ProfileBundle.load(path)
    assert loaded_again.data["pending"] == pending.data["pending"]
    assert loaded_again.sha256 == pending.sha256


def test_add_pending_deduplicates_identical_changes():
    bundle = ProfileBundle.preset("natural-looking")
    once = add_pending(bundle, kind="rule", change=rule(), **metadata())
    twice = add_pending(once, kind="rule", change=rule(), **metadata(source_run="run-2"))
    assert len(twice.data["pending"]) == 1
    assert twice.data["pending"][0]["source_run"] == "run-1"


@pytest.mark.parametrize("kind,change", [("rule", {"bad": True}), ("guest_op", {"kind": "nope"})])
def test_add_pending_validates_changes(kind, change):
    with pytest.raises(ProfileError):
        add_pending(ProfileBundle.preset("natural-looking"), kind=kind, change=change, **metadata())


def test_stale_by_confirmed_state_and_promotes_in_pending_order():
    bundle = ProfileBundle.preset("natural-looking")
    bundle = add_pending(bundle, kind="guest_op", change=guest_op(), **metadata())
    bundle = add_pending(bundle, kind="rule", change=rule("first"), **metadata())
    bundle = add_pending(bundle, kind="rule", change=rule("second"), **metadata())
    old = bundle.data["pending"]
    updated, promoted, stale = promote_pending(bundle, {"env_MY_SETTING": {"value": "now", "source": "executor", "evidence_ref": "x"}}, [])
    # The environment op's effect is already represented; unrelated rules stay in queue order and promote.
    assert [x["pending_id"] for x in promoted] == [old[1]["pending_id"], old[2]["pending_id"]]
    assert [x["pending_id"] for x in stale] == [old[0]["pending_id"]]
    assert stale[0]["reason"] == "touches-confirmed-state"
    assert updated.data["pending"] == []
    assert [r["rule_id"] for r in updated.ruleset.rules if r["rule_id"] in ("first", "second")] == ["first", "second"]


def test_stale_by_protected_facts():
    bundle = add_pending(ProfileBundle.preset("natural-looking"), kind="rule",
                         change=rule(replace="changed"), **metadata())
    updated, promoted, stale = promote_pending(bundle, {}, ["visible"])
    assert promoted == []
    assert stale[0]["reason"] == "protected-facts-changed"
    assert updated.data["pending"] == []


def test_guest_op_promotes_to_guest_setup():
    bundle = add_pending(ProfileBundle.preset("natural-looking"), kind="guest_op", change=guest_op(), **metadata())
    updated, promoted, stale = promote_pending(bundle, {}, {})
    assert len(promoted) == 1 and not stale
    assert updated.data["guest_setup"][0]["operation_id"] == "env_set_variables"
    assert updated.data["pending"] == []
