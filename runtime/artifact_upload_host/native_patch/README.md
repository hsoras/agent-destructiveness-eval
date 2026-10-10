# Native stdout drain candidate

The native patch changes only `packages/opencode/src/tool/shell.ts`: join the
collector in the process-exit branch of the existing exit/abort/timeout race,
and propagate collector errors with `raceAllFirst`. Abort and timeout remain
active while draining. There is no unconditional join after the race.

The macOS ARM64 executable has been built with the complete embedded web UI.
`build-manifest.json` identifies the derived executable, repaired source archive,
models snapshot, exact Bun toolchain, and validation receipts. Original OpenCode
1.14.41 source/archive/lock/executable identities are preserved separately.
`candidate-manifest.json` retains the preceding shell-only source candidate;
its patched archive/tree hashes do not identify the repaired build input archive.
No archives, executables, caches or dependency directories are Git artifacts.

## Exact frozen-lock cause and minimum repair

Both the archive and installed toolchain require Bun 1.3.13. Every original
workspace manifest and catalog matches the lock semantically; all dependency
patch files are present. The shell-only archive differs in exactly one member.
Bun re-resolves the app's `ghostty-web` GitHub `#main` dependency: the original
lock resolves `20bd361`, while current main resolves `6e24d04`. An isolated
lock-only attempt changes precisely that Git resolution and integrity hash.
That moving-main candidate is retained as evidence and is not used for the build.

`build-input-repair.patch` instead pins the manifest and corresponding lock
workspace entry to the **original** `20bd361`. These are the only two repaired
lines. All package resolutions, versions, catalog entries and integrity hashes
remain byte-for-byte identical within the parsed graph. Frozen installation
passes and leaves the repaired lock unchanged. No broad upgrade or global
installation is involved.

## Reproduction

Use a fresh isolated extraction of the verified original archive. Apply:

```sh
patch -p1 < native-stdout-drain.patch
patch -p1 < build-input-repair.patch
BUN_INSTALL_CACHE_DIR="$isolated_cache" "$exact_bun" install --frozen-lockfile
```

Run native validation from `packages/opencode`:

```sh
"$exact_bun" typecheck
"$exact_bun" test test/tool/shell.test.ts --timeout 30000
```

Run `extract.py` on the original and repaired shell sources, writing
`original.ts` and `patched.ts` into `packages/opencode`; place
`production-spawner-probe.mjs` there too. From that package:

```sh
"$exact_bun" run production-spawner-probe.mjs
```

This uses actual `CrossSpawnSpawner`, its pipe transport, kill and finalizers,
and extracted complete native run/preview/tail functions. Queue gating,
truncation storage and injected collector/metadata failures are explicit fixture
adapters. Fourteen assertions cover original queued loss, repaired queued
preservation, rapid exit, stderr/nonzero/empty, timeout/abort including drain,
held descriptors, truncation/full spool/multiple chunks, and collector errors.
It supplements compiled-runtime validation. The earlier ten controlled Node
fixture assertions were not rerun before repair.

Build from the repaired source root with the retained models snapshot:

```sh
export MODELS_DEV_API_JSON="$retained_models_snapshot"
export OPENCODE_VERSION=1.14.41
export OPENCODE_CHANNEL=latest
unset OPENCODE_RELEASE
PATH="$exact_bun_directory:$PATH" "$exact_bun" run --cwd packages/opencode script/build.ts --single --skip-install
```

Use `latest` for the production channel: a custom channel changes native session
storage to `opencode-<channel>.db`. The first custom-channel build and authentic
T008 persistence failure are retained; the corrected build has its own binary
identity and fresh gate receipts. No test expectation, environment DB override,
normalization, delay, or automatic retry was used to hide the failure.
The public models snapshot is separately hashed; byte reproducibility of the
original published executable is not claimed.

## Validation and owner activation

See `build-manifest.json` for final gate results and the evidence root. Every
command, exit, full raw log and failed attempt is retained there. Initial shell
tests failed starting FSEvents in the sandbox; normal watcher access passed all
22 assertions. Typecheck passes. The real spawner passes all 14 assertions.

T005 uses its existing `OPENCODE_NATIVE_BINARY` override. T008 uses the existing
`OPENCODE_PINNED_BINARY` path and an evidence-only candidate SHA binding to the
unchanged production verifier. All routing, digest verification, guest image,
startup, cancellation, session and router TypeScript assertions remain active.
The compiled canary similarly binds the exact candidate path/SHA and retains
independent original source verification. These bindings are explicit candidate
validation, not changes to repository active pins. Provider traffic is loopback
mock traffic only.

`pin-consumer-proposal.json` lists exact cross-owner activation changes. Keep the
original base archive verifier and put derived patch/archive/tree/snapshot/binary
identities in the accompanying build manifest. Do not present the base upstream
commit/tree or published binary hash as the derived runtime identity. T005's
historical base-archive receipt is not a derived-source receipt; the build
manifest supplies that association explicitly. No peer test, shared integration,
specification or bookkeeping file is edited by this implementation task.
Independent review and coordinated owner integration remain required.
