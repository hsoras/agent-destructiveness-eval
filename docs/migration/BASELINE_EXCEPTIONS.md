# Accepted regression baseline exception

The user approved this single exception before migration execution. It does not authorize execution, waive guest isolation requirements, or mark the test as passing.

| Test | Known failure | Reason |
| --- | --- | --- |
| `tests/test_scenarios.py::test_blocker_runtime_has_no_guest_visible_harness_names` | `AssertionError` for `sandbox-tools` in concatenated legacy runtime source | The assertion scans Dockerfile installation paths and host source text rather than measuring guest visibility. NEW_SPEC guest isolation must still be validated independently. |

The test remains enabled. Only the full regression command `.venv/bin/python -m pytest -q tests` may accept its exact recorded failure. The orchestrator records the nonzero exit, JUnit failure and exception separately from passing tests. Any additional failure, changed failure message/traceback, changed exception test or scanned source file, collection error, skip, missing report or abnormal process exit blocks integration. A repaired test may pass normally; the exception need not occur.

The approved signature and SHA-256 hashes of the test file and all five scanned source files are committed in `scripts/migration/config.json`. A source change while this failure persists requires investigation and explicit approval of a refreshed baseline; it must never be accepted automatically. This prevents a new leak hidden behind the assertion's first failing token from inheriting the exception.

The other twelve baseline failures are repaired through test-only changes: the SQLite transform fixture performs a view-safe rename while preserving/restoring `legacy_alter_table`, and the notes test checks pre-commit recovery and a timeout of at least 600000 milliseconds without requiring the old sentence wording. All existing recovery, interruption, integrity and tamper-detection assertions remain enabled.

Historical evidence: `.scenario/migration-readiness/evidence/committed-baseline-failures.log` reproduced all thirteen failures against committed baseline `973e9d57d6666abdf52ba63e534608632a73de91`. Local evidence remains ignored; it is not committed as application data.
