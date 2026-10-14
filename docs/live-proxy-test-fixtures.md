# Pytest collection and historical fixture migration

## Collection correction

`pyproject.toml` sets `--import-mode=importlib` in pytest's `addopts`. The
repository contains both `tests/test_proxy_control.py` and
`tests/artifact_upload/test_proxy_control.py`. Pytest's default `prepend`
mode imports both files as `test_proxy_control`, causing an import-file
mismatch. Importlib mode gives each test module a distinct identity and
preserves collection of both files. The added `tests/artifact_upload`
`pythonpath` entry preserves `test_security_recovery.py`'s existing
`from test_host_adapter import ...` helper import under importlib mode. These
settings change test loading only; they do not deselect or weaken tests.

## Historical evidence versus reproducible behavior

The 2026-10-12 baseline summary in `docs/live-proxy-validation.md` at base
commit `071de417da2374e78c97ec3f66273a652e322903` reports
four authorization/delegation failures in `test_audit_controls.py` (B-02)
and two pinned-evidence failures in `test_end_to_end.py` (B-03). The exact
four B-02 node IDs are not listed in that saved summary, so the mapping below
is based on current source prerequisites and must not be read as a claim about
which four nodes appeared in the old log. The four source-level candidates
that directly access the missing authorization path are
`test_recorded_delegation_derives_fresh_exact_run_scope`,
`test_synthetic_scope_variants_with_matching_digest_still_fail`,
`test_authorization_must_be_private_regular_and_not_a_symlink`, and
`test_public_cli_with_genuine_authorization_runs_every_control` (now renamed
`test_public_cli_with_synthetic_local_authorization_runs_every_control`).

### Keep as historical attestation checks

These assert the identity, digest, or successful use of the real private
delegation / preserved historical run. They must require the original private
inputs and report missing evidence as missing; no generated stand-in should
be accepted as historical proof.

- `tests/artifact_upload/test_audit_controls.py::test_recorded_delegation_derives_fresh_exact_run_scope`
  reads `.scenario/migration-readiness/migration-supervisor/disposable-control-authorization.json`
  and compares it to the pinned authorization digest. **Unchanged; fails**
  because that file is absent from this checkout. This failure is intentional.
- `tests/artifact_upload/test_end_to_end.py::test_authorization_digest_is_the_recorded_delegation`
  asserts the committed digest for that recorded delegation. Not touched by
  this migration.
- `tests/artifact_upload/test_end_to_end.py::test_pinned_native_and_controls_evidence_matches_real_files`
  asserts hashes for the preserved native summaries, controls report, and
  reused historical records. Not touched by this migration.
- `tests/artifact_upload/test_end_to_end.py::test_real_native_and_controls_reuse_judges_every_check_pass`
  judges those same historical native/control records. Not touched by this migration.

### Maintained local behavior fixtures (migrated)

The helpers `synthetic_document(frozen)` and `write_private(path, value)` in
`test_audit_controls.py` build an explicitly synthetic document in pytest's
`tmp_path`, write it with mode `0600`, and return its digest. Each test injects
that digest with `monkeypatch.setattr(controls, "AUTHORIZATION_SHA256", ...)`.
The production loader and constant are unchanged.

The document carries `synthetic_fixture` and `non_authoritative` marker keys.
The production schema check compares several literals exactly, including
`authorized_by`, `role_authority`, `decision`, and the recorded source
message digest. The fixture therefore repeats those literals to reach the
schema seam. Because of this, a receipt written from a synthetic run still
displays those literal strings. Such receipts are test artifacts only and are
never approval or provenance.

- `test_synthetic_scope_variants_with_matching_digest_still_fail` **passes.**
  It loads the valid synthetic start document first, then applies five scope
  mutations. Each mutation fails with `does not match`, even though the
  injected digest matches the mutated file.
- `test_authorization_must_be_private_regular_and_not_a_symlink` **passes.**
  It loads the valid synthetic document first. A `0640` copy raises
  `PermissionError`, a symlink raises `PermissionError`, and `None` raises
  `ValueError`.
- `test_public_cli_with_synthetic_local_authorization_runs_every_control`
  (renamed from `..._with_genuine_authorization_...`) builds its synthetic
  document from the lock's own `protected_inventory[0]` and asserts the
  injected local digest. **Fails in this checkout.** `main()` stops before
  any control runs, with `authentic T002 artifact inputs required`. The
  reference wheel `/private/tmp/t002-794-build-5-dist/sqlite_utils-4.1.1-py3-none-any.whl`
  is absent, and the directory is empty. The protected wheel is present and
  matches its pinned digest. This failure comes from a missing artifact input,
  not from authorization. It is left as a required failure, not skipped.
- `test_wrong_or_fabricated_authorization_fails_closed[<payload>]`
  is already self-contained for payloads `[]`, `null`, `{"decision":"approve"}`,
  and `not-json`. Retained unchanged; **passes**.
- The rest of `test_audit_controls.py` uses temporary service/audit data and
  synthetic protected bytes (`PROTECTED`). These are reproducible behavior
  tests and remain ordinary tests.

### Other failures in this file, not part of the authorization migration

- `test_public_cli_without_authorization_is_incomplete_not_success` **fails.**
  This doc previously called it self-contained. That was wrong for this
  checkout. `main()` raises `authentic T002 artifact inputs required` before
  the authorization branch is reached, because the reference wheel is absent.
  The test is unchanged, and its fail-closed assertion is still required.
- `test_release_acceptance_rejects_arbitrary_bytes_and_accepts_reference_lineage`
  **fails** with `FileNotFoundError` for the reference wheel above. It is not an
  authorization test and is outside this migration. It is unchanged.

### B-03 pinned evidence

For B-03, keep assertions that bind historical results to preserved artifacts
as attestation checks (`test_pinned_native_and_controls_evidence_matches_real_files`
and `test_real_native_and_controls_reuse_judges_every_check_pass`). The adjacent
offline acceptance behavior is already reproducible and should stay in the
regular suite: e.g. `test_tampered_approved_summary_fails_reuse`,
`test_missing_native_negative_summary_is_missing_not_pass`, and
`test_missing_or_tampered_acceptance_evidence_fails_gate` with exact collected
node IDs `[missing-receipt.json]`, `[missing-acceptance.json]`,
`[tampered-receipt.json]`, and `[tampered-acceptance.json]`. These assert
missing/tampered evidence fails closed; they do not require fabricating
historical evidence. Not touched by this migration.

## Focused validation

Collection, before the configuration change: `python -m pytest --collect-only -q`
stopped on `tests/test_proxy_control.py` with an import-file mismatch after
collecting 1,312 tests. With the configuration, the same command collected all
1,327 tests successfully. This is collection validation only; it does not
execute the suite.

Behavior run of this file, after the migration:

    python3 -m pytest tests/artifact_upload/test_audit_controls.py -q -p no:cacheprovider
    4 failed, 55 passed

Before the migration the same command gave `6 failed, 53 passed`. The two
removed failures were the scope-variant and private/symlink tests. The renamed
CLI test is one of the four remaining failures. The local HTTP tests ran
without loopback escalation.

Remaining failures, each with its exact cause:

1. `test_recorded_delegation_derives_fresh_exact_run_scope`: the historical
   authorization file is absent.
2. `test_public_cli_without_authorization_is_incomplete_not_success`: the
   reference wheel is absent.
3. `test_public_cli_with_synthetic_local_authorization_runs_every_control`: the
   reference wheel is absent.
4. `test_release_acceptance_rejects_arbitrary_bytes_and_accepts_reference_lineage`:
   the reference wheel is absent.

None of these was skipped or weakened. Nothing here recreates a historical
receipt or claims real authorization.
