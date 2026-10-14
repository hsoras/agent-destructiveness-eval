# Guest import repair

Root cause: `eval/artifact_upload/profiles.py` read `project/artifact-upload/instance.lock.json` at import; the guest image does not copy it (intentionally), so `guest_session` crashed before its JSON loop.

Fix (only `profiles.py` changed): the lock is read lazily by `_guest_by_condition()` (`lru_cache`) on first `ProfileBundle.preset()`. Host behaviour, presets, prompts and fixture secrecy are unchanged. The Dockerfile is unchanged and the host lock is **not** copied into the guest.

Regression: `tests/artifact_upload/test_guest_imports.py` copies `eval/artifact_upload` and `sandbox/artifact_upload/worker` into a tmp dir with no `project/`, then imports `guest_session`. Against the old `profiles.py` it fails with `FileNotFoundError ... instance.lock.json`. With the fix it passes.

Results:
- `test_guest_imports.py` + `test_profile_contracts.py`: 115 passed.
- `tests/artifact_upload/test_proxy_control.py`: 3 passed.
- Real Docker guest (image built from the unmodified Dockerfile), `test_worker.py -k "guest_container or guest_handlers or guest_patch or guest_hardlink or guest_identity or guest_process or worker_session_loss or descendants_created or late_orphans or live_shell or recovery_isolated or cancel_and_timeout or lost_start"`, with `--deselect ...::test_native_differential_in_guest_container` (needs the native pin): 28 passed (parametrized), including the session fixture teardown. `MIGRATION_ARTIFACT_DIR=/tmp/gir-art`.
- Not run: the full suite and the native-pin tests (pin still missing).

SHA-256:
- eval/artifact_upload/profiles.py 8dd0d7716ccc9f3ca14b96745059f5937ca54951ade8f0140ef6583e4ba4cb46
- tests/artifact_upload/test_guest_imports.py e6d87d16605a182001d47432a96639d89a686f9836d2493d6c8c0c5b1db3565c

Note: running containers named `artifact-t007-live-*` existed before this work and were left alone.
