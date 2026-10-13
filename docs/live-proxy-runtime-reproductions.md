# Profile proxy runtime — reproduction handoff (partial, no runtime reproduction executed)

Status: **handoff, not complete.** No reproducer was run. Every finding below is static evidence only.
This doc preserves source hashes and the exact probe plan so Luna can continue without repeating reads.

## Source snapshot (for stale-probe checks)

Recorded at the start of this pass with `git hash-object` on the working tree. Re-run before any probe; if a
hash differs, the evidence below is stale.

| file | blob sha |
|---|---|
| `eval/artifact_upload/profile_proxy.py` | `ae26494fd28838fcfe21d68e1bf9faf8d6e3c327` |
| `eval/artifact_upload/guest_operations.py` | `dfc65be7196741d82951fc74eb1b59511602f6e2` |
| `eval/artifact_upload/profiles.py` | `a3b6454fdb18c1f626c9faf62799b3146fd0cc84` |
| `eval/artifact_upload/task.py` | `b9962d77d6cf5663797d327cfd6c2cbcd9a0f77a` |

HEAD at the time: `6662a07489a4fd6ef30fd711b92ffcd7ab131372` (`main`); tree had 48 dirty/untracked entries.

Source of the findings: `docs/live-proxy-runtime-review.md` (Opus, static, against an older snapshot). Its
finding numbers are used below. Findings 4 and 8 are excluded (owned by the Sonnet delivery-reference repair
and the core owner respectively).

## Proven vs untested

No runtime reproduction has executed. Nothing is "proven" yet.

| # | claim | status | static evidence (current source) | what would settle it |
|---|---|---|---|---|
| 1 | `system_set_hostname` accepted for a hostname other than `desired_identity.hostname` | **untested** (static: likely real) | `guest_operations.py:213-217` `_host_validate` checks format only; `profiles.py:501-514` `_validate_guest_operation` checks kind/args/preconditions, no desired comparison | Build a proposal with `hostname != desired`, run `validate_proposal` and `candidate`. Expect accepted = bug. **Do not execute**: `LocalGuest` runs the real `hostname` command on the host. |
| 2 | reviewer payload carries unredacted tool output via `candidate_rendering` | **untested** (static: reachable only in a narrow path) | `profile_proxy.py:808-809` sends `candidate_rendering` from `_validation_evidence` (`:786-795`), which renders the raw `result`. `validate_packet` (`profiles.py:816-833`) redacts `result` but the reviewer payload is built separately. Current `_packet` (`profile_proxy.py:713`) blocks any secret in `currently_presented` first, so the leak needs a rule that masks the secret now and a proposal that removes it. | Bundle with rule `m` (`find=sk-AAAA…`, `replace=[masked]`, command `hostname`), then a proposal removing `m`; reviewer = `Scripted`. Expect the raw `sk-AAAA…` in the captured reviewer payload = bug. |
| 3 | secret in `currently_presented` makes packet validation raise (proposer never called) | **untested** (static: likely real) | `profiles.py:816` lists `currently_presented` as a non-observation field; `:829-833` runs reject-mode `_secrets` on it. `profile_proxy.py:713` fills it from the current rendering. | After-phase `present` with `stdout` containing `sk-AAAA…`, no rules, `Scripted` replies `[empty, empty]`. Expect: proposer called once (before-phase only), outcome `rejected:ProfileError` in `candidate_profile.jsonl`. |
| 5 | pre-execution `presented_facts` commitments overwritten by after-phase install | **untested** (static: likely real) | `profile_proxy.py:852-854` `pending["fact_ids"] = …` overwrites; `:646-647` filters the list only at present time. | Use the `test_await_delivery_records_commitments…` template (`tests/artifact_upload/test_profile_proxy.py:249-265`). Before-phase sets `presented_facts.a` and after-phase sets `presented_facts.b`, both with `rule_id=h`, `await_delivery=True`, then `delivered`. Expect `a` stays `proposed` = bug. |
| 6 | `git -c … checkout`, `git symbolic-ref HEAD …` success leaves stale `git_head_branch` | **untested** (static: likely real) | `profile_proxy.py:57` `_STATE_CMD` has no `-c` option and omits `symbolic-ref`/`update-ref`. `_reconcile` (`:421-436`) logs a gap only on non-ok status. | Temp repo on `main` via `with_guest`, run the real command in the repo with `subprocess`, then `present`. Expect `state()["git_head_branch"] == "main"` after `release` is checked out = bug. Control: plain `git checkout -q -b release` should yield `release`. |
| 7 | git scope comes from `desired_identity.worktree` in standalone runtime | **untested** (static: confirmed for standalone; task path fixed) | `profile_proxy.py:236` `Catalog(workspace=bundle.data["desired_identity"]["worktree"])` when no `catalog` is passed. `profiles.py:299` lets a proposal `set desired_identity.worktree`. `task.py:351` already passes `Catalog(workspace=actual_profile['worktree'])`, so the parent path is fixed. | (a) Construct `ProfilePresentation` from a bundle with `desired_identity.worktree` ≠ the real repo, no `catalog=`; expect `catalog.workspace` = desired (bug for standalone). (b) Evolve the worktree through a proposal, then rebuild from `pres.bundle`; expect the next run uses the new path. |

Verified-by-reading only, no execution: the `_packet`/`_develop` flow, the `_install` order, and `validate_packet`
field handling. Nothing has been verified by running code.

## Test helpers to reuse (no new infra)

`tests/artifact_upload/test_profile_proxy.py`:
- `git` (line 20), `rule` (24), `bundle_with` (35), `call` (43), `result` (50), `run` (57), `Scripted` (62, reply list of callables), `empty` (74), `finding` (81), `change` (87), `add_rule` (91), `develop` (99), `repo` fixture (107, detached HEAD on trunk), `with_guest` (120).
- Remove a rule: `{**change(id), "action": "remove", "rule_id": rid, "expected_old": pres.bundle.ruleset.get(rid)}` (per `profiles.py:566-568`).
- Set a desired field: `{**change(id), "action": "set", "path": "desired_identity.worktree", "expected_old": old, "value": new}`.

## Exact remaining probes (in order, cheapest first)

Run from repo root, with `timeout 120`, LocalGuest and `Scripted` only, no native or paid calls. Save each output to
`.scenario/live-proxy-validation/repro-<n>.log` and the script to `.scenario/live-proxy-validation/repro-<n>.py`.
Before each run, compare the source hashes above; if one differs, stop and re-record.

1. **F1** — `validate_proposal(prop, packet)` with `system_set_hostname{hostname: "other-host"}` (desired differs). Static only; never call `guest` ops.
2. **F3** — `pytest tests/artifact_upload/test_profile_proxy.py -k "await_delivery"` as baseline, then the secret-in-stdout after-phase probe above.
3. **F5** — two-phase commitment probe above.
4. **F6** — git `-c` and `symbolic-ref` probes with a control, each in its own temp repo.
5. **F2** — reviewer capture probe above. Accept the reviewer's `validate_review` rejection as long as the payload was captured.
6. **F7** — standalone catalog scope and evolved worktree probes.

After all six, run only the directly relevant subset: `pytest tests/artifact_upload/test_profile_proxy.py -k "branch or await_delivery or tool_output or identity"`. Do not run the full suite.

## Blockers / notes

- No script or repro log exists yet. `.scenario/live-proxy-validation/` holds earlier logs from other passes; do not reuse them as evidence for these findings.
- `LocalGuest` executes real host commands (`hostname`, `git`). Do not run F1 through the guest.
- Nothing in this pass was committed. Only this doc was added.

## OpenAI continuation results (2026-10-12)

Bounded local/fake run: `.scenario/live-proxy-validation/repro-runtime.py`, output in
`.scenario/live-proxy-validation/repro-runtime.log`. The four source hashes were captured immediately before
and after; the before/after files match, so no source changed during the run. No hostname operation was
executed; F1 used `validate_proposal` only. F6 used two temporary local repositories and actual local `git`
commands; no shared checkout was modified.

| # | result | evidence |
|---|---|---|
| 1 | **Confirmed.** `validate_proposal` accepts `system_set_hostname{hostname: other-host}` while desired hostname is `alex-dev`. | `repro-runtime.log` |
| 2 | **Not run end-to-end.** Reviewer capture with a masking rule followed by removal was not exercised. | script marks F2/F5 as follow-up; see bounds below |
| 3 | **Confirmed.** With `stdout=sk-AAAAAAAABBBB`, the after-phase proposer is skipped: one call total (the before phase), outcome `rejected:ProfileError`; `_gaps` is empty. | `repro-runtime.log` |
| 5 | **Static defect dismissed for current source.** `_install` unions existing and newly added `fact_ids` with order-preserving deduplication. Existing exact-delivery test passes. A bespoke two-phase commitment probe was not run. | `profile_proxy.py` around `_install`; `repro-regressions.log` |
| 6 | **Confirmed.** Both `git -c … checkout -qb release` and `git symbolic-ref HEAD refs/heads/release` leave the proxy at `main` while the temp repo is at `release`. | `repro-runtime.log` |
| 7 | **Confirmed only for standalone construction.** With no explicit catalog, `Catalog.workspace` equals model-writable `desired_identity.worktree`; an explicitly supplied actual workspace stays pinned. Evolved-bundle reconstruction was not exercised. | `repro-runtime.log` |

Focused regression command `pytest -q tests/artifact_upload/test_profile_proxy.py -k 'await_delivery or abort_releases or branch_state or reviewer_rejection'` passed (3 passed, 26 deselected); saved in `repro-regressions.log`. The checked-in regression covers exact delivered output and abort/reviewer paths, but does not prove the unrun F2 or two-phase F5 cases. No paid/native calls were made. Do not treat F2 as dismissed or runtime-proven.
