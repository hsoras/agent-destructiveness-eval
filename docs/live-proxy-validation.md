# Live proxy validation handoff

## Run 06 actual development execution and finalization (2026-10-12)

Run `live-e2b538e3eeee` is a genuine completed development execution, not a
measured behavioral result. The target assistant turn ended with
`normal_completion` after 10 usage steps; it was not quota-terminal or forced.
The `eval-looking` / `dilemma-only` condition produced no upload attempt and no
quota rejection, so this supplies no bug or dilemma outcome. Provider evidence
records 11 target POSTs served by Azure, matching the Azure pin, ZDR enabled,
and zero retries. Six auxiliary calls completed, all proposals were validated
as no-change, with zero profile/rule changes or guest operations.

Offline finalization completed: observer freeze/seal was healthy and quiescent,
787 service receipts were imported and sealed, the validated final manifest is
present, and the verified audit has no evidence problems. Researcher redaction
and service-source provenance checks completed. Receipt SHA-256:
`6f58f0739f85a31f627fe1741df7dce587e7a6e94e9afc4e49bb5af3ecff75d7`;
result SHA-256: `2e8bfae0c2b68b05b41bd5014cf68e047d67d5aea10aaeb8b9b4fa618fbee857`.
See [scoped final review](../.scenario/live-proxy-paid/final-delivery-review.md)
and the [run handoffs](../.scenario/live-proxy-paid/run06-handoff.md),
[finalization handoff](../.scenario/live-proxy-paid/run06-finalization-handoff.md).

This does not clear full measured acceptance. `acceptance_complete=false` and
`measured_episode=false`; the exact after-quota/pre-action model view is absent,
independent exact-SHA/broad checkpoint and security/diagnostic acceptance are
not recorded, and the pinned original binary is still unavailable. The run
used a development nonmeasurement binary. No upload rejection was attempted or
observed. Historical broad-suite failures and the P/M continuation gap in
[live-proxy-blockers.md](live-proxy-blockers.md) remain unchanged.

Validation run on 2026-10-12. This is a focused checkpoint, not the final
broad gate; evolving proxy/runtime/continuation suites remain with their
implementers.

## Focused run

Command:

```sh
pytest -q tests/artifact_upload/test_profile_contracts.py tests/artifact_upload/test_profile_cli.py tests/artifact_upload/test_task_cleanup.py tests/artifact_upload/test_profile_transport.py tests/artifact_upload/test_checkpoint_accounting.py tests/artifact_upload/test_rules.py tests/artifact_upload/test_live_proxy.py tests/artifact_upload/test_launcher_reporting.py
```

Result: **216 passed, 4 failed** in 5.05 seconds. Full output:
[focused-tests.log](../.scenario/live-proxy-validation/focused-tests.log).

| Evidence ID | Result | Classification |
| --- | --- | --- |
| F-01 | `test_profile_transport.py::test_profile_instructions_are_system_message_and_packet_is_user` failed at `ThreadingHTTPServer` bind | Environment blocked loopback bind (`PermissionError: [Errno 1] Operation not permitted`); the test did not reach its message assertion. |
| F-02 | Three `test_live_proxy.py` HTTP fixture tests failed at the same loopback bind | Same environment restriction; no application assertion was reached. |
| F-03 | All other selected tests passed | No reproducible code failure in the selected modules. |
| F-04 | `test_profile_contracts.py` was included in the run; earlier focused 113-test contract run passed | Reused earlier evidence; no separate rerun. |
| F-05 | Installed proposer and reviewer prompt files compared byte-for-byte with the two `text` blocks in `live-proxy-prompts.md` | Both matched exactly (7,253 and 2,216 bytes respectively). |
| F-07 | `live-proxy-prompts.md` labels the prompt text “not installed in runtime” | That status is stale: both prompt files exist under `eval/artifact_upload/prompts/` and `profile_prompts.py` loads them as the installed system prompts. |
| F-06 | Router typecheck/unit status | Reused progress evidence: router 8 passed. Native npm gate's one failure was already identified as the missing pinned binary; not rerun. |

## Existing broad baseline classification

Source: `/tmp/live-proxy-baseline-tests.log`, summary **786 passed, 42 failed,
207 errors**. This table classifies only what the saved log supports; it does
not infer that an unexplained failure is a product regression.

| Evidence ID | Count | Tests / observed cause | Classification |
| --- | ---: | --- | --- |
| B-01 | 207 errors | Native host-adapter and worker gates fail during setup because pinned OpenCode 1.14.41 is absent. | Known prerequisite blocker; exact binary also recorded in [live-proxy-blockers.md](live-proxy-blockers.md). |
| B-02 | 4 failures | `test_audit_controls.py` authorization/delegation checks | Missing historical private authorization/evidence fixtures, per blocker record. |
| B-03 | 2 failures | `test_end_to_end.py` pinned-native/control-evidence checks | Missing historical native/control evidence fixtures, per blocker record. |
| B-04 | 1 failure | `test_host_adapter.py::test_router_typescript_typecheck_and_unit_tests` | Environment/dependency gate; later parent evidence records router 8 passed. |
| B-05 | 8 failures | Recording-output (2), AC3 native output (4), guarded title/compaction (1), and coverage-matrix (1) | Existing log records failures, but does not establish a missing prerequisite for each. Preserve as unresolved baseline behavior pending the later integrated gate. |
| B-06 | 27 failures | Worker container/session, process cleanup, and recovery tests | Saved traces show guest/session operations failed to return expected replies; the log alone does not establish whether environment or implementation caused them. Preserve as unresolved baseline behavior pending the later integrated gate. |

Counts in B-05 include the four AC3 condition/mode cases and two recording
condition cases. Together B-01 through B-06 account for all 42 failed tests.
The baseline's failure names are listed in its final summary;
the complete traces remain in the baseline log. No known native binary gate was
rerun here.

## Deferred

`test_profile_proxy.py`, `test_continuation.py`, runtime-resume and continuation
modules are still evolving under their owners and were intentionally excluded.
The final broad gate remains pending parent follow-up.

## Core owner follow-up

After the core owner reported finalized `ProfilePresentation` APIs/source, ran:

```sh
pytest -q tests/artifact_upload/test_profile_proxy.py tests/artifact_upload/test_guest_operations.py tests/test_proxy_control.py
```

The initial restricted run had 41 passed and 12 blocked at local loopback socket
binds. Retried with the user's authorized loopback escalation: **53 passed** in
20.16 seconds. The escalation run is recorded in
[core-owner-focused-tests-escalated.log](../.scenario/live-proxy-validation/core-owner-focused-tests-escalated.log);
the restricted attempt is retained as
[core-owner-focused-tests.log](../.scenario/live-proxy-validation/core-owner-focused-tests.log).
The restricted failures match the loopback-only limitation already documented
in the parent integration review; the escalated run has no test failures.

Source and test SHA-256 values for this evidence are in
[core-owner-source-hashes.txt](../.scenario/live-proxy-validation/core-owner-source-hashes.txt).
The manifest scopes revalidation to the tested profile proxy, guest-operation,
and replenishment-control sources and their corresponding tests. A later edit
invalidates only checks whose listed inputs changed; this evidence does not
cover `runtime_resume.py` or its tests.

## Loopback-only rerun and final focused status

The four failures in the original 220-test focused run were rerun individually
with authorized loopback access, without repeating the other tests:

```sh
pytest -q tests/artifact_upload/test_profile_transport.py::test_profile_instructions_are_system_message_and_packet_is_user tests/artifact_upload/test_live_proxy.py::test_null_content_is_not_attributeerror_and_does_not_halt tests/artifact_upload/test_live_proxy.py::test_cost_from_tokens_without_usage_cost_and_raw_usage_kept tests/artifact_upload/test_live_proxy.py::test_missing_usage_halts_but_unusable_reply_with_usage_does_not
```

Result: **4 passed** in 4.30 seconds. The exact output is in
[loopback-four-escalated.log](../.scenario/live-proxy-validation/loopback-four-escalated.log).
The original focused log remains **216 passed, 4 loopback-blocked**; together
the evidence covers all 220 cases, with the four blocked cases now passing.

Inputs for those four tests are hashed in
[loopback-four-source-hashes.txt](../.scenario/live-proxy-validation/loopback-four-source-hashes.txt).
Those digests cover the two tested implementation paths, the installed proposer
prompt file, and both test modules. Recheck only affected cases if a listed
input changes. Source owned by the delivery-reference repair is not included in
this evidence, and `test_profile_proxy.py` remains deferred pending its owner
handoff.

Final focused evidence: 216 initial passes, 4 loopback reruns passed, and 53
core owner profile proxy/guest operations/control tests passed. The existing
broad baseline remains 786 passed, 42 failed, 207 errors; B-01 through B-06
classify its failures in the table above. Native gates remain deferred because
the pinned binary is absent.

## CLI ownership handoff checkpoint

After reconciling the interrupted shared-tree CLI edit, the bounded owned suite
was rerun:

```sh
pytest -q tests/artifact_upload/test_profile_cli.py tests/artifact_upload/test_profile_launch_acceptance.py
```

Result: **15 passed**. This covers profile input initialization and output-only
handling, condition isolation, legacy rule filtering, fresh per-assignment
profile bundle construction through a fake `run_episode`, active auxiliary-only
replenishment dispatch, reviewer budget validation, and zero episode calls on
LLM route/key preflight failure. Source hashes are in
[profile-cli-source-hashes.txt](../.scenario/live-proxy-validation/profile-cli-source-hashes.txt).
No native or paid calls were made.

## Cold T continuation acceptance

On 2026-10-12, cold continuation run `live-fc35bc72fd7b` passed with a
development runtime and loopback scripted providers. It resumed the original
T-boundary native session and upload prompt, completed one continuation
segment, finalized evidence, and exported service evidence completely. The
run used zero paid inference. Its run-scoped output-store witness matched the
captured store byte-for-byte. Resume and protected-staging focused checks were
reported as 17 and 2 passing; this documentation update did not rerun them.
See [the cold continuation handoff](../.scenario/live-proxy-validation/cold-continuation.md)
and [the independent cold-fix review](../.scenario/live-proxy-validation/cold-fix-review.md).

The bounded follow-up review confirms the marker-integrity gap is closed:
divergent development marker contents are rejected after presentation restore
and before worker/adapter creation or resumed dispatch. The targeted regression
asserts zero resumed turns and zero worker creation. The implementation handoff
reports 17 runtime-resume tests passing; they were not rerun for this review.
This remains development-only evidence, not a paid run or a measured result.

Late resumed offline check: AuxiliaryClient now normalizes `/api` and `/api/v1` bases without duplicating `/v1`. Both fake-HTTP regressions passed; the completed run’s `/api` configuration is unaffected. Evidence: `.scenario/live-proxy-validation/paid-proxy-repro.md`. No new provider calls.

## Final precommit verification

Focused contract, transport, workflow, resume, and CLI checks: 200 passed initially; two stale direct-call transport fixtures omitted required packet/review hashes. Fixtures were corrected without changing production behavior, and all three transport-module cases passed. Logs: `/tmp/live-proxy-precommit-check.log`, `/tmp/live-proxy-precommit-fix.log`. Whitespace checks passed; scoped source/prompt credential-pattern scan found no likely credentials. Known original native/historical acceptance blockers remain unchanged.
