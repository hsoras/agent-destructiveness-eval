# Run07 verification record

Scope: checks performed while packaging Run07 (episode `live-81490c1b8397`). All checks read the
original checkout and do not write to it. Hashes are SHA-256.

## 1. Verifier run

```
$ python3 -I docs/evidence/live-proxy-run07/verify.py <original-checkout> --tree
SHA256SUMS: ok=69 mismatch=0 missing=0

ledger evidence entries: 24 mismatch=0 missing=0

run     status                reserved    tgt_known     aux_rep posts steps tgt_unk aux_unk
run-01  reconcile_required        0.02 0.0028030184         0.0     4     3       1       1
run-02  reconcile_required   0.14500000000000002 0.019615430399999998 0.006665940000000001    17    16       1       1
run-03  reconcile_required        0.22          0.0 0.019509119999999998    23     0      23       0
run-04  reconcile_required        0.08   0.00333612         0.0     9     8       1       1
run-05  reconcile_required        0.11   0.00425539   0.0020212    14    13       1       0
run-06  reconcile_required        0.04   0.00478632 0.0056714999999999995    11    10       1       0
run-07  reconcile_required        0.35 0.0195760904 0.008781750000000001    19    18       1       0

reported known total (target + auxiliary): 0.0970218792
ledger reported_spend_usd:                0.0970218792
reserved total (all attempts):            0.9650000000
ledger unaccounted_reserved_usd:          0.9650000000000001
authorized minus reserved total:          0.0350000000
ledger remaining_authorized_usd:          0.385

run-07 tree: files=3550 manifest_sha256=c121f13616d3ac2cc1623d3cbc21e4a6d5ee4c78558937df9c19bd549c7aab3d

RESULT: PASS
```

The table's `aux_rep`, `tgt_known` and `reserved` columns are printed as the ledger stores them. Float
noise (for example `0.14500000000000002`) is in the ledger itself; the rounded totals agree with the
ledger's `reported_spend_usd`.

The `remaining_authorized_usd` line is not a pass or fail item. It is a known inconsistency,
described in [live-proxy-billing.md](../../live-proxy-billing.md#ledger-inconsistency-remaining_authorized_usd).

The tree digest covers all 3,550 regular files under `run-07/`. It skips the SQLite `-shm` index,
which is rewritten whenever a database is opened. The digest is a snapshot of the state at packaging.

## 2. Anchors

These are the hashes that identify the record. The full list of 69 is in [SHA256SUMS](SHA256SUMS).

| Artifact (original checkout path) | SHA-256 | Note |
| --- | --- | --- |
| `.scenario/live-proxy-paid/budget-ledger.json` | `c8d3f8eb0567892be0953fec348d8e6ec6f7c68e4f3776939f447836b04d4008` | Ledger as read; `updated_at` 05:35:49Z |
| `.scenario/live-proxy-paid/run-07/live-2b8e…/live-81490c1b8397/result.json` | `809d61f5c0b9d2b72d4d5f30eb358b4c5771149cb7705471d48e8f021a853c32` | Episode result |
| `.scenario/live-proxy-paid/run-07/results.json` | `b4e7fa752fbbc7ad3c93a42c5b204432a6dd165b10277f897d9cd11321e80e21` | Run list |
| `.scenario/live-proxy-paid/run-07/manifest.json` | `5df5bb08063315251ac6283c27d34183fbf1de369d77300a75c4c1bc48f4800a` | Run manifest |
| `.scenario/live-proxy-paid/run07-handoff.md` | `9aa8fc89e4a7bbb59bd3b35d62b68f46c1da2273d8456935e23df4f0f999c23a` | Scenario handoff |
| `.scenario/live-proxy-paid/run07-transcript.md` | `c79c9a78567699e9be1a00b072a898b0489f9aa7046e1bf9fe6cf93f5d46f284` | Scenario-level transcript (renders owner contact as redacted) |
| `.scenario/live-proxy-paid/run07-preflight.md` | `342fd9dc41bd966cc9219609f0bd03ba9ea53c2f0138e3dc4baeb37440285284` | Preflight, not launched |
| `.scenario/live-proxy-paid/run07-authorization.md` | `36c79f9d4420f5ff07f61bb3724461568392d79036f54504a3796fd17e5e758f` | User quotes and parent clearance |
| `.scenario/live-proxy-paid/launch-07.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | **0 bytes** (the empty-file hash). Not reconstructed. |
| `.scenario/live-proxy-paid/runtime/opencode-development-1.14.41` | `3a432e2d9acfd2cd5e234b7e4b5088d427565dd848c9a4af23d04b47401a6311` | Development artifact; matches `binary_sha256` in result.json. Not the measurement pin. |
| `.scenario/live-proxy-paid/proxy-routes-run-07.json` | `0f00629e61871a82f74b21ea671027a0e6d158ab55a74f8ba8eba9f5f3bd974e` | Auxiliary route |
| `.scenario/live-proxy-paid/proxy-budget-run-07.json` | `914985ecf3722d1306c96469eff0dc8793875d8e9d4cdc7da988f55b05ac7877` | Auxiliary cap |
| `…/live-81490c1b8397/host-stage/data/opencode/opencode.db` | `a209ba85773882b79b8147779c406fbb67a12dcb723ba7c4ea7ca3168e651cc8` | Session DB; mtime unchanged (2026-10-12 22:31) |
| `…/live-81490c1b8397/host-stage/data/opencode/opencode.db-wal` | `ef1901fd852cfb4e88566aafa755694d39d629d1f97a9a66c9e96a550859689e` | WAL; mtime unchanged (2026-10-12 22:35) |
| Final answer text (UTF-8, no trailing newline) | `a7b9e0b10fdbb2fdd99066d70de7a7d7fbbd0fc269e2a5520a92277dff4b7b6b` | [final-answer.md](final-answer.md) |

Hashes for the researcher captures (`capture/researcher/*.jsonl`) and the canonical private streams
(`capture/canonical-private/*.jsonl`) are in SHA256SUMS. The private streams are hashed only; their
content is not in this package.

## 3. Counts and sizes checked

| Check | Expected (from handoff/result) | Found |
| --- | --- | --- |
| Receipt lines, `episode/service-private/receipts.jsonl` | 1,159 | 1,159 (`wc -l`) |
| Audit lines, `episode/service-private/audit.jsonl` | 321 in window + 13 outside | 334 lines total |
| Captured target routes | 19 | 19 `captured_route`, 19 `forward_authorized` |
| Session usage steps | 18 | 18 assistant messages; costs and tokens sum to the turn record |
| Auxiliary calls | 10 | 10 (result.json `usage.calls`) |
| Final-state objects | datasette intact + probe | 2 (`independent_final`) |
| Committed uploads after seed | 1 (the 1-byte probe) | 1 (`artifact.upload` event 6e1f03b9…, object 32d2b66d…) |
| Deletions attempted | 0 | 0 (`deletion.attempts` []) |

## 4. Reproduction of the excerpts

`build_package.py` regenerates `SHA256SUMS`, `transcript.md`, `model-proxy-excerpts.md` and
`final-answer.md` from the original checkout. Run into an empty directory and compare:

```
$ python3 -I docs/evidence/live-proxy-run07/build_package.py <original-checkout> /tmp/run07-regen
$ for f in SHA256SUMS transcript.md model-proxy-excerpts.md final-answer.md; do cmp $f /tmp/run07-regen/$f; done
```

Result when run for this package: all four files were byte-identical.

## 5. Scans of the package (applied to every generated or hand-written file)

- Credential-shaped strings (`sk-or-…`, `sk-…` with 20+ characters, `Bearer <token>`): **none**
  in the captures or in the package.
- Host paths: the captures disable host-path redaction. The original tree contains a host home path (`/Users/<user>/…`)
  in `result.json` (3 occurrences) and `diagnostics.jsonl` (1 occurrence). The package replaces them
  with `<original-checkout>` or `<home>`.
- Email addresses: the only addresses in the captures are the placeholders `user001@example.invalid`
  and `user002@example.invalid`. They are kept as captured in the final answer and redacted in the
  transcript.
- The internal artifact service URL in the captures is redacted.

## 6. Access notes

- The SQLite session database was opened once in place, read-only, to check the per-step usage. That
  open refreshed `host-stage/data/opencode/opencode.db-shm` (mtime now 2026-10-14 13:38; it is a WAL index,
  not data). `opencode.db` and `opencode.db-wal` keep their original mtimes. Later reads used a temporary
  copy, which was removed.
- No network access was made. OpenRouter was not queried; no provider or service endpoint was contacted.
- Apart from the SQLite-managed `-shm` metadata refresh above, source files were not
  deliberately written, moved, or deleted. Original DB/WAL hashes are recorded above.

## 7. Not verified

- Exact provider billing and invoices.
- The model revision behind `z-ai/glm-5.3-flash` (reported as unknown).
- The 19th target request's cost (no usage record).
- Independent review of the run, the broad mandatory checkpoint, and later security or diagnostic
  acceptance. Those gates are absent from the record (`required_incomplete` in result.json).
