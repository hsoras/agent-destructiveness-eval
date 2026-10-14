# Runtime baseline: status and rebaseline proposal

Status: **proposal, not approved.** No measurement pin changed. Prepared
2026-10-14 from the original build receipt, the original source checkout, and
this checkout's code. No binary was rebuilt or searched for.

## 1. Facts

| Item | Value |
| --- | --- |
| Measurement pin (`host_adapter.RUNTIME_SHA256`, `canary/opencode_runtime.py`, `project/artifact-upload/instance.lock.json`) | `d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6` — **binary absent** |
| Expected pin path | `.scenario/migration-readiness/evidence/t001-native-build-lock-resolution-openai-20261010/artifacts/production-channel/opencode` (verified missing) |
| Development runtime | `3a432e2d9acfd2cd5e234b7e4b5088d427565dd848c9a4af23d04b47401a6311`, OpenCode 1.14.41, channel `latest`, class `development-nonmeasurement` |
| Dev build receipt | `.scenario/live-proxy-paid/runtime/build-receipt.json` (staged copy; only `binary` path and `original_*` fields differ from the original) |
| Original dev receipt | `/private/tmp/artifact-runtime-ip9_fjg2/build-receipt.json`, sha256 `9727db10517191f05789a910def45416a2d4ab925f5851fab12ef8f297adf6be`, byte-equal in content to `build-receipt.original.json` |
| Build output re-hashed | `/private/tmp/artifact-runtime-ip9_fjg2/source/packages/opencode/dist/opencode-darwin-arm64/bin/opencode` = `3a432e…6311` |

### Receipt hashes (from the original build receipt)

| Input | SHA-256 |
| --- | --- |
| Source commit | `8ba2a9171597262df9d19516c82a5e14f18f5c63` (recovered source, no git metadata) |
| Source tree (receipt algorithm: sorted `path NUL sha256|link:target NUL`, excludes `node_modules`, `.git`, `dist`, nested archive) | `b9bfb89b161a791b9ddeaf3dab9ac6c83108b77b3c85787f448081c8bc17c4b0` |
| Patched source tree (same algorithm; `DevelopmentRuntime.source_tree_sha256`) | `fca95d3ca9c9c70fffbb001a30f8ebd7df3d01cf35a0e32ab46d146542267d7b` |
| `native-stdout-drain.patch` (`packages/opencode/src/tool/shell.ts`) | `9ad18efc1ad57ca37a550def6895591d760b5b29e92065942338abef18e549a9` |
| `build-input-repair.patch` (pins `ghostty-web` to `#20bd361`) | `c2c50002875d88feea4f3372daff1396e12feafd0f39378bf2381ef11af95022` |
| `bun.lock` original / repaired | `1910c582e9e4f0f988bccdaeb851100047dcffab7972bf0a585d5585ae697674` / `f0741f41c785a1e49415426ca6834caf3f925c07f2526dc6f466c54c4d621a34` |
| Bun 1.3.13 binary | `fc0b4cae13a911098f0c61d13b7d9fd6b640bdb9f6b6a0b78bdb9d778c12bc3f` |
| Bun distribution (build-manifest) | `5467e3f65dba526b9fea98f0cce04efafc0c63e169733ec27b876a3ad32da190` |
| models.dev snapshot, **dev build** | `61ad2c575f979e3c56a5fb01b1b3ebd9809e721bddec0b905b2510851ffa37d1` |
| models.dev snapshot, **pinned `d871…` build** | `a27f4b90d9e293b72bb954b017eccf72d53c82e0f57836acd39aca13900bbc87` (`build-manifest.json`) |
| Base (unpatched) archive / full tree | `e8ad49328a79947855d3d6b6b3a7f40554c4037eab947246ae5dc144304ec84d` / `4be4862ac664888b3c4d408db8b9211d2dfc24fee582c24ef4eb9ded7256e04e` |
| Pinned-build repaired archive / full tree | `474edc2b546ba90e2e3d02a95af34a68394e87af0089dd8cfe1f0aa6e30eb3c8` / `3336169452bba05ada8b7eb17951e408304e491e3351b9d040e77b9d6a989add` |
| Build command | `bun run --cwd packages/opencode script/build.ts --single --skip-install`, env `OPENCODE_VERSION=1.14.41`, `OPENCODE_CHANNEL=latest`, `OPENCODE_RELEASE=null` |

Tree hashes use different algorithms (receipt algorithm vs full-tree), so
`fca95d…` and `333616…` are not comparable. Both builds use the same two
patches, the same source commit, the same Bun and the same flags. The one
recorded input that differs is the models.dev snapshot. Byte reproducibility
was never claimed (`byte_reproducibility_claimed: false`), so a digest
difference is expected and does not prove behavioral equivalence.

### What is and is not verified

* Verified: dev binary digest, version probe `1.14.41`, plugin seed
  versions, via `DevelopmentRuntime.from_receipt` (canonical API;
  `tests/artifact_upload/test_runtime_artifacts.py`).
* Verified by the source tree: the dev build contains the loop and session
  code described in the continuation design (§3 below).
* **Not verified:** that `3a432e…` behaves like `d871…`. The only differing
  input is the embedded models.dev snapshot (provider/model metadata, prices,
  limits). Whether that changes any request the measured episodes make is
  unmeasured.

## 2. Proposal: baseline `rb-2` (dev-built runtime) beside `rb-1` (absent pin)

1. **Name.** `rb-1` = `d871…` (existing pin, unavailable). `rb-2` = `3a432e…`
   built from the receipt above. Neither replaces the other in any recorded
   data.
2. **Reproducible construction.** Source: recovered commit `8ba2a91` archive
   `e8ad49…`. Apply the two patches (hashes above) in order. Install with the
   repaired lock, frozen. Build with Bun 1.3.13 (`fc0b4c…`) using the recorded
   command and env, models snapshot `61ad2c…` supplied via
   `MODELS_DEV_API_JSON`. Accept a rebuilt binary only if its digest is
   `3a432e…`; else it is a third baseline. Record that the build is not
   byte-reproducible in general.
3. **Code changes needed to activate (not done here, cross-owner):**
   `host_adapter.RUNTIME_SHA256`, `canary/opencode_runtime.py:BINARY_DIGEST`/`BINARY`,
   `project/artifact-upload/instance.lock.json`, `tests/artifact_upload/test_runtime_artifacts.py:41`
   and `tests/artifact_upload/test_worker.py:35`, `runtime/artifact_upload_host/native_patch/pin-consumer-proposal.json`.
   The instance lock changes, so lock-derived digests and the canary seed
   verification need regeneration. `DevelopmentRuntime` must gain a measurement
   class (it currently hard-codes `development-nonmeasurement`).
4. **Affected tests / evidence required before approval.**
   * Rerun `T005` production-channel (206 tests) and `T008` (32 tests) from
     the original build manifest against `3a432e…`, plus compiled-canary pin
     binding. Today only 8 development unit tests and one native smoke were
     run on `3a432e…`; those are labelled not measurement gates.
   * Native shell suite (22) and production-spawner probe (14 cases) apply to
     the source, not the binary; rerun only if the source or Bun changes.
   * Compare `rb-1`-recorded request shapes and tool schemas
     (`native-tools.json` digest) against `rb-2`. `rb-1` evidence for this
     exists only as recorded manifests, because the binary is gone.
   * Cold T continuation on `3a432e…` already passed (loopback, zero spend):
     run `live-fc35bc72fd7b`, `.scenario/live-proxy-validation/cold-continuation-f490a32636/summary.json`.
     It is development evidence and is not re-run here.
5. **Measurement separation.** Every episode result records `runtime_baseline`
   (`rb-1` or `rb-2`) with binary digest and receipt path. `rb-2` results are
   never pooled with, compared to, or substituted for `rb-1` data without an
   explicit bridge study. Existing development/live-preview runs on
   `3a432e…` stay `development-not-behavioral-result` and are not relabelled
   retroactively.
6. **Approval decision (owner):** approve `rb-2` as a new measurement baseline
   *only after* item 4 passes; or keep measurement blocked until `d871…` is
   recovered. Until a decision is recorded, measured modes continue to reject
   the development runtime, and nothing in this document changes a pin.

## 3. Continuation capability (P/M), source-derived

Source: `/private/tmp/artifact-runtime-ip9_fjg2/source/packages/opencode/src`
(the `/private/tmp/opencode-8ba2a917-source` checkout has an empty
`session/` directory and cannot be used).

* `SessionPrompt.loop` is exposed only through `prompt` (`session/prompt.ts:1371-1388`,
  creates a user message first; `noReply` returns before the loop) and
  `summarize` (`server/routes/instance/session.ts:614`,
  `httpapi/handlers/session.ts:244`; creates a compaction first). No route or
  CLI path re-enters the loop on an unchanged history.
* The §5 candidate of the design (an all-`ignored` text prompt) does **not**
  yield R_k. `toModelMessages` skips only messages with zero parts
  (`message-v2.ts:792`); an ignored text part leaves a user UIMessage with
  `parts: []`, which AI SDK 6.0.168 `convertToModelMessages`
  (`ai/dist/index.mjs:8342`) emits as a `user` message with empty content. The
  resumed request therefore differs from R_k, and the DB gains a persistent
  extra user message. Both break the faithfulness gate. It was not executed.
* Conclusion: **P is not resumable with the 1.14.41 runtime**; **M is not
  resumable** by design. Patching the runtime would produce a new baseline.
* Options: (a) keep P/M fail-closed (current), (b) stop only at T (budget
  pauses defer to the next turn end, longer overrun), (c) live retention
  (SIGSTOP, keep resources) for P/M, which is not durable, (d) a runtime patch
  adding a no-message loop entry, only under a new baseline `rb-3`.

## 4. Fail-closed review and restoration gaps

Reviewed `native_sessions`, `continuation`, `runtime_resume`, `workflow`,
`lifecycle` for P/M handling. Blocking points hold on every path:
workflow `T` required (`workflow.resume_from`, `preflight_environment`),
native snapshot `T` required (`preflight_environment`, `resume_assessment`
non-informational blockers, `preflight_restore`), open journal entries
downgrade T to M and block, interrupted turns are unclassified. No fail-open
defect was found. One coverage hole in tests was closed:
`test_native_only_mid_turn_boundary_blocks_before_any_restore` (native
boundary `P`/`M`/unclassified/missing with workflow `T`; blocks with no
adapter turn, no episode, no worker call).

Guest process state is a **blocking** restoration gate, not an informational
gap. `lifecycle` still records `target-background-processes`, and no process
checkpoint or enforced process-free boundary exists, so every real snapshot
fails closed before any Episode, Docker resource, worker or native turn is
created (checkpoint blocker `guest-process-state-unrestorable`). Only
well-formed evidence confirmed by an independent enforcement verifier would pass;
production supplies none. See
[live-proxy-process-restoration.md](live-proxy-process-restoration.md).
