# Pre-execution setup verification

Pre-execution infrastructure is ready with one explicitly accepted baseline exception. **Execution is NOT READY because root `README.md` has unrelated uncommitted changes discovered in the final checkout check.** Those changes were preserved untouched. Actual migration execution also still requires explicit user authorization. No actual migration task, integration branch or real coding worker was started. T001/T002 final decision ratification remains required later. No runtime dependency versions or OpenCode pin changed; no upstream source was patched.

The source baseline is `973e9d57d6666abdf52ba63e534608632a73de91` (`migration scripts`). The user already committed the initial orchestration infrastructure. The test-only baseline fixes and exception documentation are committed separately at `67643f4`; approved follow-up orchestration/setup changes are committed with this readiness report. Both approved specs remain byte-identical with SHA-256 `882a38ec987f83da4415485d266eabba6ddc0329252e712dc89ad796314701a9`; `docs/migration/STATUS.md` is unchanged and all 18 tasks are unassigned.

## Prepared environments and results

| Check | Actual result | Local evidence |
| --- | --- | --- |
| OrbStack/Docker | Linux `aarch64` / ARM64; Docker 29.4.0, Compose v5.1.2 | `.scenario/migration-readiness/evidence/opencode-linux.json` |
| Existing dev extra | `uv sync --locked --extra dev --inexact` installed pytest 8.4.2 and its missing support packages | Current `.venv`; locked version comparison in `evidence/readiness.json` |
| Patched sandbox tools | Exact vendored 1.2.1 source plus the existing six-file overlay; patch ID `inspect-sandbox-tools-1.2.1-remote-exec-retry-trace-v2`; **3 passed, no skips** | `evidence/sandbox-tools-tests-unsandboxed.log` and its JSON receipt |
| Orchestrator safety | **29 passed** in 43.448 seconds; simulated Codex only, including exact-exception rejection tests | `evidence/final-orchestrator-safety.log`; final receipt-path checks in `evidence/final-gate-receipts-safety.log` |
| Generated-dependency policy | Root and nested backend `node_modules` ignored; tracked `uv.lock` owned by T001 and excluded from T008; targeted policy test passed after final ignore-pattern refinement | `.gitignore`, configuration, `test_dependency_lock_ownership_and_generated_modules_ignore` |
| Pinned OpenCode host | Isolated Darwin ARM64 binary and package both **1.14.41**, exit 0 | `evidence/opencode-host.json` |
| Pinned OpenCode guest | Existing immutable Linux ARM64 image; binary and package both **1.14.41**, exit 0; no network or host volumes | `evidence/opencode-linux.json` |
| Targeted SQLite and notes tests | **26 passed**, no skips, 1.73 seconds; original recovery/interruption/tamper assertions preserved | `evidence/baseline-repair-targeted.xml` |
| Whole existing regression suite | **208 passed, 1 failed**, no skips, 17.97 seconds; sole failure is the approved exception | `evidence/final-regression.log` and `.xml` |
| Actual configured regression gate | **Accepted: 208 passed, 1 exact baseline exception**; raw pytest exit 1 retained | `evidence/final-regression-gate.json` and referenced log/XML/receipt |
| Baseline reproduction | The same **13 failures** reproduce against an untouched `git archive` of committed baseline `973e9d5` | `evidence/committed-baseline-failures.log` and JSON receipt |
| Approved T001 cleanup | No tracked, untracked or ignored changes, stash, unique commits, process working directories or open files; normal worktree removal and `git branch -d` succeeded without force | `evidence/worktree-cleanup.json` |
| Graph/configuration | 18 tasks, 10 waves; disjoint paired scopes; only T001 initially eligible; configuration matches task briefs; manual gates preserved | Read-only graph/configuration checks and safety suite |

Evidence paths above are relative to ignored `.scenario/migration-readiness/`. Aggregate receipt: `.scenario/migration-readiness/evidence/readiness.json`. These caches/logs, virtualenv contents and prepared binary packages must not be committed.

The two previous real concurrent Codex documentation smoke runs remain passing in their retained temporary repositories, as documented in RESULTS.md. No new real Codex worker was launched for this setup.

## Repeatable development setup

```sh
uv sync --locked --extra dev --group migration-regression --inexact
.venv/bin/python scripts/migration/prepare_regression.py
.venv/bin/python -m pytest -q tests/test_inspect_sandbox_tools_patch.py
.venv/bin/python -m unittest discover -s scripts/migration -p 'test_*.py' -v
```

The `migration-regression` development group exactly matches the existing `sandbox/inspect-sandbox-tools-requirements.txt`. Relative to the committed lockfile, the only new packages are `jsonrpcserver==5.0.9`, `oslash==0.6.3` and `returns==0.27.0`; all existing package versions and `[project].dependencies` are unchanged. `--inexact` retains unrelated installed packages. The project has an existing warning about the non-version `.python-version` value `streamstats-inspect`; it did not prevent setup and was not changed.

`prepare_regression.py` copies the vendored source into a digest-named ignored directory, overlays only the already-existing patch files, verifies source hashes, and registers that copy in the selected project virtualenv using `migration_sandbox_tools.pth`. It verifies that the interpreter imports the staged 1.2.1 package. Preparation was run twice successfully with the same source digest `f44f69c18cc07eae`; tracked vendored/patch sources are untouched. All worktree checks use the configured source-repository `.venv` interpreter, which includes this local regression setup.

The enclosing managed sandbox prevents Docker socket access and macOS process enumeration. Docker verification and the final process-aware regression runs used permitted execution outside that sandbox. The initial sandbox-tools run failed with `psutil`/`sysctl` permission errors (2 failed, 1 passed); the unchanged tests passed outside it. The initial read-only Docker version check lacked writable XDG state directories; a retry with disposable tmpfs XDG directories passed. Neither failure was counted as success or bypassed by skipping a check.

The verified host executable is:

```text
.scenario/migration-readiness/opencode-host-1.14.41/node_modules/opencode-darwin-arm64/bin/opencode
```

Its SHA-256 is `7c3c2ccc7824e4933d0b030314c9ecc4fdb2d0d113dab586e3e2e6db7df2dd68`. Its package and native package are pinned in that isolated installation's local npm lockfile. Installation used `--ignore-scripts`; the native executable was invoked directly with isolated XDG paths and model fetching/autoupdate disabled. The globally installed Homebrew OpenCode 1.18.3 was not replaced or selected as the pinned environment. Runtime version checks establish launch compatibility, **not** successful interception; G01's supported-hook canary remains future T001 work.

## Ownership and approval boundaries

Generated dependencies are ignored only under the new backend's host runtime and guest-worker directories, including nested packages. Python/npm manifests and lockfiles remain visible. T001 owns `pyproject.toml` and matching `uv.lock` changes; the brief, extraction code, configuration and exclusion list agree. No other worker may change them. No baseline test is removed or disabled. The single accepted exception is documented in `docs/migration/BASELINE_EXCEPTIONS.md` and recorded explicitly in configuration and check receipts.

The provisional decisions are recorded in `docs/migration/DECISIONS.md` and passed in worker instructions: supported hooks first, no runtime pin changes/upstream patches without separate approval, spec-compliant neutral API, symmetric spec-permitted optional-tool exclusions, and T002 candidate research only until final user selection approval. These choices do not authorize migration execution.

The `agent/T001` test worktree and branch were removed after the user's explicit conditional approval and immediate safety rechecks. Tip `bd99b7e` is retained in the main checkout's ancestry. No force flag, reset, user-data deletion or conflict discarding was used. Only the primary checkout remains. A new migration integration branch is intentionally absent until authorized execution from a committed baseline.

## Final baseline disposition and readiness

The original thirteen failures were reproduced on untouched committed baseline `973e9d5`. Twelve are now repaired with test-only changes: the SQLite test double preserves/restores `legacy_alter_table` during its view-safe rename, and the notes assertion recognizes the existing pre-commit recovery and minimum shell timeout requirements. Four added fixture cases verify ON/OFF preservation after successful and rejected renames. All original interruption, recovery, integrity and seven tamper-detection cases remain enabled. Production application sources and the migration notes themselves are unchanged.

The only remaining failure is `tests/test_scenarios.py::test_blocker_runtime_has_no_guest_visible_harness_names`, caused by `sandbox-tools` in legacy Docker installation source. It remains enabled and visibly fails in the raw suite. The user approved an exact baseline exception, not a claim that guest visibility was tested successfully. The full regression command alone may accept its pinned JUnit message/traceback signature and hashes of the test plus all five scanned source files. Changed signatures/sources, another failure, collection errors, skips, missing reports and abnormal exits block integration. A clean pass is allowed. The exception requires explicit review/approval to refresh; no automatic re-baselining exists.

The coordinator's own full-suite check was exercised without initializing execution state or launching workers. Its durable receipt retains raw exit 1, 208 passing tests, one failing test and the accepted exception identity. Safety tests exercise both helper rejection paths and actual pytest subprocesses, including a different command and an additional failing test. Normal integration-failure tests ensure status is not prematurely marked merged.

Final read-only readiness checks verified the 18-task/10-wave graph, matching task/config scopes and commands, disjoint paired ownership, T001 as the only initially eligible task, T001/T002 commit-specific ratification gates, retained documentation-smoke reports, unchanged approved spec hashes, and absence of execution state/actual integration branch. Safety tests create isolated Git worktrees and verify sequential integration and interruption recovery. The approved unused `agent/T001` worktree and branch are already gone; no additional worktree removal was performed.

The offline lockfile check passed; `[project].dependencies` and all previously locked package versions are unchanged. Final Docker verification reports Linux aarch64 / ARM64 and Docker 29.4.0; the immutable guest image still launches OpenCode 1.14.41. Host OpenCode 1.14.41 was relaunched and its recorded binary hash checked. Final environment receipts are in `evidence/final-environment.json`. The initial offline uv check inside the enclosing sandbox could not access the existing cache; retry outside it passed. The existing `.python-version` warning remains non-blocking and its file was not changed.

**NOT READY to execute until the unrelated root `README.md` changes are reviewed and committed or otherwise preserved outside the required clean checkout, and the user separately authorizes execution.** The final clean-checkout assertion failed on that file; no execution state or branch was created. No infrastructure/regression blocker remains under the accepted exception policy. Later T001 supported-hook evidence and T001/T002 final architectural/candidate ratification remain required before their gated merges; they are migration work, not pre-execution checks. Destructive schema/data operations, production actions and merging into main remain prohibited. Local evidence, caches, staged packages and `.venv` are ignored and excluded from commits.
