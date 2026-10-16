# Independent review: live proxy fixture migration

Scope: reviewed only the diffs for `pyproject.toml`,
`tests/artifact_upload/test_audit_controls.py`, and
`docs/live-proxy-test-fixtures.md` at HEAD `071de417da2374e78c97ec3f66273a652e322903`.
No edits were made to those files.

## Findings

No reproducible blocking defect found in the scoped changes.

- Pytest configuration collected all **1,327 tests**, including both
  `test_proxy_control.py` modules and the helper import path described in the
  fixture note.
- Both self-contained migrated behavior tests passed:
  `test_synthetic_scope_variants_with_matching_digest_still_fail` and
  `test_authorization_must_be_private_regular_and_not_a_symlink` (**2 passed**).
- The historical `test_recorded_delegation_derives_fresh_exact_run_scope`
  remains unchanged and requires its genuine private artifact. The synthetic
  fixtures do not replace or satisfy that attestation.
- Synthetic authorization is confined to test `tmp_path`; tests replace the
  module digest with the generated file's digest via pytest monkeypatch. The
  production authorization digest and loader are outside the diff. With the
  fixed production digest, these synthetic files cannot pass the loader's
  digest check. Exact schema/scope comparisons remain exercised, including
  five negative mutations.
- The documented four focused failures remain attributable to unavailable
  historical/private authorization or the absent pinned reference wheel; the
  synthetic migration does not mask those prerequisites.

Nonblocking documentation precision: the note says marker keys *and* the
monkeypatched digest separate the synthetic document from the recorded
 delegation. The production loader ignores extra marker keys; the effective
separation is the test-only digest monkeypatch and temporary path. The markers
are explanatory labels, not an enforced safeguard. This does not broaden
production authorization because neither the constant nor loader changed.

## Evidence

- `pytest --collect-only -q`: **1,327 tests collected**.
- Focused migrated tests: **2 passed in 0.03s**.
- Reviewed diff file SHA-256 values:
  - `pyproject.toml`: `3132b30902a113f66e7397cfa42e47ea02b691627fafccd87cef93d822e4c67b`
  - `tests/artifact_upload/test_audit_controls.py`: `39c33c3f568c977d7ecaae608e8b388dbfac499918fb3bef236b14c31733dc13`
  - `docs/live-proxy-test-fixtures.md`: `bc919968b87e00f02cd8570e9e014e2c9fe15c703ef2432b94324ec5dddc0de6`
