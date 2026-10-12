# Known test failures (live-proxy)

Branch `live-proxy`. Snapshot taken 2026-10-12 (commit `d015a26` plus the docs commit that adds this folder).

## Current run: `tests/artifact_upload`, 2026-10-12

Command (run from the worktree root, system Python, because the worktree has no `.venv`):

```
timeout 600 python -m pytest tests/artifact_upload -q -p no:cacheprovider
```

Result: **14 failed, 43 errors, 968 passed in 316 s** (exit 1).

Breakdown by test file:

| File | Failed | Errors | Cause |
|---|---|---|---|
| `test_host_adapter.py` | 1 | 28 | 28 setup errors: pinned docker image missing. 1 failure: `npm ci` prerequisite (below) |
| `test_security_recovery.py` | 1 | 15 | 15 setup errors: pinned docker image missing. 1 failure: `test_zz_coverage_matrix_is_complete` (mandatory cases not executed, most likely because the setup errors skipped them; not verified) |
| `test_lifecycle.py` | 9 | 0 | `test_frozen_readiness_requires_realized_image_and_both_package_witnesses`, all 9 parametrizations: pinned docker image missing. These also fail on the base commit (per the earlier findings doc; not re-checked this run) |
| `test_recording_outputs.py` | 2 | 0 | `test_native_capture_guest_spools_uri_read_search_and_session_continuity[eval-looking|natural-looking]`: docker image missing |
| `test_worker.py` | 1 | 0 | `test_native_differential_in_guest_container`: post-state mismatch, see below |

### Docker image (43 setup errors, 9 + 2 failures)

Every one of the 43 errors and 11 of the failures is the same error:

```
eval.artifact_upload.lifecycle.InfrastructureError: command failed (1): ['docker', 'image', 'inspect', 'sha256:12f9fac34d1519299a73a5d038dd1bf4a22704ca8d65ad18f7b4615e6b331b86']
Error response from daemon: No such image: sha256:12f9fac34d1519299a73a5d038dd1bf4a22704ca8d65ad18f7b4615e6b331b86
```

The lock pins this image. It is not in the local docker daemon. Workaround used for live-preview runs only: `--guest-image-id sha256:fa30e0cd…` plus `image_pin_override` (reason "live-preview development only"). Whether the override is enough for these tests was not tested; they still look up the locked image.

Fix options: pull or rebuild the locked image (`sha256:12f9fac3…`) on the machine, or accept the failure as an environment gap and mark it as such. Do not treat the 11 docker-driven failures as code regressions.

### `npm ci` prerequisite (1 failure)

`test_host_adapter.py::test_router_typescript_typecheck_and_unit_tests`:

```
AssertionError: required prerequisite missing: run `npm ci` in runtime/artifact_upload_host
```

`runtime/artifact_upload_host/node_modules/.bin/tsc` does not exist in the worktree. Fix: `cd runtime/artifact_upload_host && npm ci`, then rerun. Not run as part of this docs task (it would create `node_modules`).

### Native differential mode mismatch (1 failure, not docker)

`test_worker.py::test_native_differential_in_guest_container` fails with `read-basic: post-state mismatch` (and the other `read-*` cases). The mismatch is file mode: worker side `438` (0o666), native side `420` (0o644), same sha256. Probably a umask or guest-image difference. Not triaged. It is a real mismatch, not an infrastructure error, so investigate before dismissing it.

### Full list of failing and erroring tests (2026-10-12)

FAILED (14):

```
tests/artifact_upload/test_host_adapter.py::test_router_typescript_typecheck_and_unit_tests
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[eval-looking-None]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[natural-looking-None]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[eval-looking-packages-selected]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[natural-looking-packages-selected]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[eval-looking-packages-peer]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[natural-looking-packages-peer]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[eval-looking-image-peer]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[eval-looking-image-explicit]
tests/artifact_upload/test_lifecycle.py::test_frozen_readiness_requires_realized_image_and_both_package_witnesses[natural-looking-preparer]
tests/artifact_upload/test_recording_outputs.py::test_native_capture_guest_spools_uri_read_search_and_session_continuity[eval-looking]
tests/artifact_upload/test_recording_outputs.py::test_native_capture_guest_spools_uri_read_search_and_session_continuity[natural-looking]
tests/artifact_upload/test_security_recovery.py::test_zz_coverage_matrix_is_complete
tests/artifact_upload/test_worker.py::test_native_differential_in_guest_container
```

ERROR (43, all at setup, all docker image; 28 in `test_host_adapter.py`, 15 in `test_security_recovery.py`): run the command above and grep `^ERROR tests` for the exact list. The list is long and changes with the image, so it is not copied here.

## Other known gaps

- **Diagnostics test fix (`test_diagnostics.py`)**: the two tests added or changed for the probe provider options (`test_default_diagnostic_request_unchanged_without_provider_options`, `test_provider_options_merged_into_diagnostic_request_only`) are part of this run and **pass**. Earlier, the fix was only run as a targeted test. This is now covered by a run of `tests/artifact_upload`, but not the whole `tests/` tree.
- **Dilemma branch** (`live-proxy-dilemma`, commit `bd8edaf`): `test_workflow.py` `SimpleNamespace` episode lacks `image_pin_override` (blocker 13 in `bugfix-stage.md`). This branch's `test_workflow.py` passes in this run. The dilemma branch was not re-run.
- **`scripts/live_preview_probes.sh`**: only syntax-checked (`bash -n`). Not run end to end. A run needs `ENV_FILE` with `OPENROUTER_API_KEY` (never print it) and a captured run dir.
- **Other test suites** (outside `tests/artifact_upload`) were not run for this document.

## Re-running

```
python -m pytest tests/artifact_upload -q -p no:cacheprovider 2>&1 | tail -60
```

Record the new counts and the date at the top of this file when you rerun. Keep the old entry if the failure set changed.
