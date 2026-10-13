# Profile proxy validation blockers

## Status after Run 06

The actual `live-e2b538e3eeee` development run completed normally and its
service evidence was fully finalized (787 sealed receipts, validated final
manifest, verified audit with no evidence problems). This closes the prior
statement that no actual run occurred; it does not change the blockers below
or establish measured acceptance. The run used the development-nonmeasurement
binary, ended with `acceptance_complete=false`, had no exact
`after_quota_before_action` view, and made no upload attempt or quota rejection.
Independent exact-SHA/broad checkpoint and later security/diagnostic acceptance
also remain outstanding. See [validation summary](live-proxy-validation.md)
and [Run 06 final review](../.scenario/live-proxy-paid/final-delivery-review.md).

Opus 5.5 investigated the local prerequisites on 2026-10-12. No runtime pins or
mandatory tests were changed.

The locked Darwin ARM64 OpenCode 1.14.41 binary is unavailable. The required
SHA-256 is `d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6`.
The default location is
`.scenario/migration-readiness/evidence/t001-native-build-lock-resolution-openai-20261010/artifacts/production-channel/opencode`.
`OPENCODE_PINNED_BINARY` can select an identical recovered artifact. The local
1.18.3 binary and Linux binaries do not satisfy the pin. Rebuilding is not
evidence of byte identity and does not authorize changing the pin.

The matching 1.14.41 plugin seed was recovered from an intact native discovery
staging copy and placed at
`.scenario/migration-readiness/opencode-canary-plugin-1.14.41`. Only its package
manifest, lockfile and node_modules were copied. Runtime tool/config files from
that staging directory were excluded. The seed alone cannot unblock native tests.

Historical private authorization and native-control evidence fixtures referenced
by existing mandatory tests are also absent. They are not recreated or replaced
with synthetic authorization. `/tmp/live-proxy-baseline-tests.log` records the
baseline: 786 passed, 42 failed, 207 errors.

Native continuation is reported by boundary. Turn-boundary restoration can be
implemented and exercised with SQLite/fake resource adapters, but its native
acceptance gate needs the exact binary. The pinned runtime has no faithful
mid-turn loop resume entry point; those checkpoints must refuse restoration
explicitly, retain evidence, and never start a new conversation as a substitute.
Background process and in-memory guest restoration remain explicit coverage gaps.
See [the native continuation design](live-proxy-continuation-design.md).

## Cold continuation status

The local-loopback cold T continuation `live-fc35bc72fd7b` completed against
the recovered development artifact, resumed the original session and prompt,
finalized evidence, and completed service export with zero paid inference. It
does not clear the measurement gate. The strict original Darwin ARM64 1.14.41
binary hash above remains unavailable, so pinned native acceptance is still
blocked. P/M re-entry remains blocked because the pinned runtime has no faithful
mid-turn resume entry point; do not replace it with a new conversation. These
blockers remain in force pending the paid handoff; no actual paid run is
claimed here.

The cold review's development-marker pre-dispatch gap is closed: divergent
contents are checked immediately after presentation reconstruction and before
worker or adapter creation. The tampering regression verifies zero resumed
turns and zero worker creation. See [the cold-fix review](../.scenario/live-proxy-validation/cold-fix-review.md).
