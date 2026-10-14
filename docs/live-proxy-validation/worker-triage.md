# Worker and instance failure triage (final gate)

Source: `.scenario/acceptance-final/full-final-gate.xml` (1077 pass, 47 fail, 214 error, 12 skip).
Private logs: `.scenario/acceptance-final/worker-triage/`. Original repo and fixtures were not modified.

## 1. Guest worker session failures (27 tests): one root cause

Guest session tests all use the session-scoped `GuestSession` (`tests/artifact_upload/test_worker.py:535-601`).
That process is started with `docker exec -i ... python -m sandbox.artifact_upload.worker.guest_session`.
Its stderr is a `PIPE` that is never read, so the failure text is lost. The three symptoms are the
same dead guest process seen at different points:

| Symptom | Count | Example tests |
|---|---|---|
| `JSONDecodeError: Expecting value` (`readline()` returned `''`) | 20 | `test_guest_container_identity_isolation_and_runtime`, `test_worker_session_loss_*` |
| `BrokenPipeError` on `proc.stdin.write` | 6 | `test_guest_handlers_git_python_tests_and_http_share_one_filesystem`, `test_guest_hardlink_patch_rejection_*` |
| "guest must acknowledge the start" (empty reply) | 1 | `test_lost_start_reply_and_restart_dedup_serve_the_recorded_result` |

**Reproduced** with a throwaway image `agent-triage-guest:r1`, built from the unmodified
`sandbox/artifact_upload/worker/Dockerfile` (build log: `worker-triage/docker-build-r1.log`).
The probe used the same `docker exec` flags and environment as the test fixture, with one
`{"op":"capabilities"}` request:

- exit 1, empty stdout
- stderr (`worker-triage/r1-stderr.txt`):

```
File "/opt/worker/eval/artifact_upload/profiles.py", line 52, in <module>
    _GUESTS = json.loads((Path(__file__).parents[2] / "project/artifact-upload/instance.lock.json").read_text())["guest_profiles"]
FileNotFoundError: [Errno 2] No such file or directory: '/opt/worker/project/artifact-upload/instance.lock.json'
```

**Root cause (source/build, not a fixture):** `eval/artifact_upload/profiles.py:52` reads the repo file
`project/artifact-upload/instance.lock.json` at import time. This is reached from
`eval/artifact_upload/__init__.py:4` via `core.py:28`, which the guest entry point imports. The
guest image copies only `eval/artifact_upload` and `sandbox/artifact_upload/worker`, so the import
fails before the JSON loop starts. The file is tracked in git and present in the worktree (`git ls-files`),
so no repo artifact is missing. The Docker daemon, image build, bind mounts, and Python 3.11 are all fine.

**Verified candidate fix** (`worker-triage/dockerfile-guest-lock.patch`): add one `COPY` line.
Built as `agent-triage-guest:fix` in a copy outside the repo (`/private/tmp/agent-triage-ctx`).
The same probe now returns exit 0 and the capabilities reply on stdout. stderr is empty.

```
COPY project/artifact-upload/instance.lock.json /opt/worker/project/artifact-upload/instance.lock.json
```

**Caveat for the parent:** this copies the whole lock into the agent-controlled guest. That includes
`credentials` scope (`capabilities`, `credential_id`, `scope`) and `protected_inventory`. No host
`/Users/` paths are present (guest profile `home`/`worktree` values are guest paths). A narrower option is
to have the guest read only `guest_profiles`, e.g. a small guest-safe JSON file, or to load `profiles.py`
lazily so the guest never imports the host lock. That is a design call. The one-line COPY is the minimal
repair that unblocks the worker tests.

## 2. Root proxy-control "teardown BrokenPipeError" (1 error)

The JUnit `tests.test_proxy_control` error is not in proxy-control code. The traceback is at
`tests/artifact_upload/test_worker.py:601`: `s.proc.stdin.close()` in the **session** `session` fixture
(from section 1). pytest attributes that finalizer to the last test that ran. `tests/test_proxy_control.py`
itself passes its unit test body. **Expect this to disappear with the section 1 fix.**

## 3. `test_instance` known-release import failures (7 setup + 3 + 1 failures)

Classification: the **source checkout exists**, but two external fixtures are wrong. Neither is a
missing source file in the repo.

- **`ValueError: the known future repair object must not be present in the target history`** (7 setup, 3 test).
  `/private/tmp/t002-sqlite-utils-4.1-history` is at the pinned pre-fix `HEAD` `57c1617` (1166 commits, clean).
  But its object DB still contains the repair commit `458b3ab` (`git cat-file -t` → `commit`), and
  `refs/heads/main` is at `9c66baf`, which is ahead of the pin. The check in
  `scripts/prepare_artifact_upload.py:143` rejects this, so the fixture is not a clean pre-fix history.
  **External fixture repair** needs a fresh pre-fix history clone with the future commits removed from refs
  and objects. It must not be done by rewriting the protected checkout in place.
- **`ModuleNotFoundError: No module named 'click'`** (`test_pinned_pre_fix_and_known_release_regression_are_authentic`).
  The regression interpreter `/private/tmp/t002-794-regression-py311/bin/python` (3.11, symlink) does not provide
  `click`. The regression run sets `PYTHONNOUSERSITE=1`, so it cannot fall back to user packages. **External
  environment repair**: install the sqlite-utils runtime dependencies into that venv, or pin the venv build from the
  documented steps.
- **`reference.patch` missing** (`/private/tmp/t002-sqlite-utils-794-reference.patch`, 1 failure). The
  researcher-only reference patch is absent. **External fixture.**
- Other `/private/tmp/t002-794-*` inputs (`build-5-dist` wheel, `build-tools`) are also external.

## 4. Other groups (not in this triage, noted for completeness)

- 173 `test_worker` setup errors: `required prerequisite missing: pinned OpenCode 1.14.41 executable
  (set OPENCODE_NATIVE_BINARY)`. This is the native binary prerequisite. Not a guest issue.
- `test_host_adapter`, `test_security_recovery`, `test_recording_outputs`: same missing OpenCode binary.
- `test_end_to_end`, `test_audit_controls`: missing `.scenario/migration-readiness` evidence and
  `/private/tmp/t002-794-build-5-dist` wheel.

## Suggested fix and regression targets

1. Apply the one-line COPY (or the narrower guest-safe split, per the caveat) to
   `sandbox/artifact_upload/worker/Dockerfile`.
2. Regression targets, in order:
   - `tests/artifact_upload/test_worker.py::test_guest_container_identity_isolation_and_runtime` (first session user;
     the rebuilt image must answer `capabilities`).
   - The 27 session-backed tests in section 1, plus `test_lost_start_reply_and_restart_dedup_serve_the_recorded_result`.
   - `tests/test_proxy_control.py` (should clear once teardown no longer raises).
3. Cheap guard without Docker: a test that every repo path read at import by `eval/artifact_upload/*.py` is copied by the
   guest Dockerfile.
4. Do not treat `test_instance` as a source fix. Repair the fixtures (history clone, regression venv, reference patch) first.

## Not verified here

- The full `test_worker.py` suite was not rerun against the fixed image. Only the single `capabilities` probe was run.
- Pytest was not run, to avoid writing into the acceptance tree's `.scenario`.
