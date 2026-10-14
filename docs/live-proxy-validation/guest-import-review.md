# Guest import repair review

Reviewed against base `7c239005465b4c37304ad37aad9bf2f7be00563e`.

## Result

No blocking defect found in the bounded change. The module no longer reads `project/artifact-upload/instance.lock.json` during import. `ProfileBundle.preset()` still reads the same `guest_profiles` entries on first preset construction, maps by `condition`, and uses the same identity fields and profile construction path. The lazy cache retains host behavior after that first read. The guest image can therefore import the package without receiving the host lock; the Dockerfile and lock contents are outside this diff.

The missing/malformed lock still fails when preset construction actually needs it: the uncaught file/JSON/key errors propagate from `_guest_by_condition()`, as the eager version also failed during import. The focused profile contract suite passed with the host lock present, exercising the existing public profile interface and preset contracts. No schema or preset construction code changed apart from when the lookup occurs.

## Evidence

- `pytest -q tests/artifact_upload/test_guest_imports.py tests/artifact_upload/test_profile_contracts.py` — **115 passed**.
- The new isolated guest test copies the worker and `eval/artifact_upload` package into a temporary tree with no `project` directory, then launches a fresh Python process to import `guest_session`; it asserts exit code 0.
- `git diff --check -- eval/artifact_upload/profiles.py tests/artifact_upload/test_guest_imports.py` — clean.
- Reviewed blob hashes: `eval/artifact_upload/profiles.py` `282929e1dcd44e07211870beda12e2a4d4ef2a23`; `tests/artifact_upload/test_guest_imports.py` `a77e5bf721c4a0ac2bc551d0d0f7fb32126bb9a8`.

## Non-blocking test note

The isolated import test proves the guest import no longer requires a host project/lock, but does not assert the guest process's public JSON protocol or explicitly exercise preset failure with a missing/malformed lock. The existing profile contract tests cover host preset/schema behavior. These are coverage opportunities only; the reviewed code path is straightforward, and I found no concrete failure requiring a change.
