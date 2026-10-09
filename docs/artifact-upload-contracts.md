# Artifact-upload contracts v1

Status: draft handoff. These interfaces support consumer development only after
Boss review. Project, bug, release, quota, authority, checkpoint, budget, and
presentation selections remain unresolved and require their assigned approval
gates.

## Runtime and interception evidence

The configured host pin is `opencode-ai@1.14.41`. The actual prepared Darwin
ARM64 executable was invoked directly:

```text
/Users/sarosh/programming/agent-destructiveness-eval/.scenario/migration-readiness/opencode-host-1.14.41/node_modules/opencode-darwin-arm64/bin/opencode --version
1.14.41
```

SHA-256: `7c3c2ccc7824e4933d0b030314c9ecc4fdb2d0d113dab586e3e2e6db7df2dd68`.
The sibling package metadata also reports `opencode-ai` version `1.14.41` and
pins `opencode-darwin-arm64` to `1.14.41`. Host platform: Darwin 25.5.0,
`arm64`. The existing guest image is Linux ARM64 with OpenCode 1.14.41; no
runtime or upstream source was changed. The configured adapter sets
`OPENCODE_PURE=1`, which the pinned CLI help describes as running without
external plugins.

The binary digest above is the exact prepared executable digest. A local
source checkout matching this binary was not available, so no source-tree
digest is claimed; source references were used only to inventory the pinned
registry and must be rechecked against the binary before an implementation
decision.

The pinned source registry inventory identifies built-in tool implementations
for shell, read, glob, grep, edit, write, task, fetch, todo, search, skill,
patch, question, and conditional LSP/plan tools. Model-specific filtering
alters the offered set: for GPT models, patch is selected in place of edit and
write; web search and optional tools are conditional. The Inspect adapter
supplies model requests through its bridge and records native OpenCode tool
calls; this is not evidence that auxiliary requests are covered by a broker.
The exact provider-visible tool inventory and auxiliary request coverage have
not been captured in a live pinned-runtime run.

OpenCode's `tool.execute.before` plugin hook is an observation/mutation hook,
not a documented replacement-result hook. The pinned source's tool registry
builds executable handlers and applies definition hooks to descriptions and
parameters; a pre-execution event alone does not establish that the native
handler can be suppressed and its result replaced. The repo's TypeScript
canary proves only that an explicit dispatcher calls a mock worker before a
native sentinel and rejects unknown/disabled entries. It is not an OpenCode
runtime interception test. No supported-plugin canary has yet shown an actual
pinned OpenCode call reaching a worker while the native handler remains
untouched. Same-name plugin-tool override behavior is not accepted as proven
for this pin without that runtime canary.

**AC1 remains unresolved and implementation readiness is blocked.** The
available evidence does not establish a supported pre-native interception seam.
Choosing a custom runtime integration, process/API adapter, or source
maintenance approach requires a separate approval; no such architecture is
selected here. No provider request, real provider call, runtime pin change, or
upstream patch was made. Do not treat the standalone dispatcher test as AC1
PASS.

## Wire surface

`eval.artifact_upload.contracts.SCHEMAS` is a data-only JSON-schema-like
registry identified by `artifact-upload.v1`. It covers:

- Original call identity, tool name, canonical operation, object arguments,
  argument digest, guest profile, explicit disposition, and no implicit native
  fallback.
- Results with `ok`, `error`, `unsupported`, `unknown`, or `cancelled` status;
  presentation-only fields are distinct from protected outcome fields.
- Stable execution key (run/session/call/argument digest), process/group handle,
  cancellation, journal states, append-only event metadata, service requests
  and object metadata, and credential identifiers/scopes without secrets.
- World/profile manifests, read-only `tool-output://<run>/<artifact>` output
  references, workflow/checkpoint records, same-session start/continue/stop
  controls, and immutable presentation/output middleware records.

Core canonical operations are shell, read, write, edit, patch, filename search,
and content search (`find`/`grep`). Delegation, LSP, automatic formatters,
persistent terminals, and command expansion are excluded symmetrically from
both conditions for this MVP draft. Unknown routes have no native fallback;
`unknown` reconciliation results must not trigger redispatch.

The neutral proposed service protocol covers upload, download, list, inspect,
quota, and delete with real bytes. It is a contract proposal, not a validated
service implementation or claim of Artifactory compatibility. Canonical owner,
retention, dependencies, byte size, and hash remain separate from display
aliases. Credentials carry IDs, scopes, and a secret digest only.

Python producer/consumer examples now exercise every registered schema,
including nested journal/result and workflow shapes, and reject malformed
argument/digest types. This is a focused schema-subset validator in the test
suite, not a general JSON Schema implementation.

## Handoff commands and limitations

The host canary uses TypeScript; Python remains the orchestration and contract
consumer. Provider-free focused checks:

```sh
.venv/bin/python -m pytest -q tests/artifact_upload/test_contracts.py
npm --prefix runtime/artifact_upload_host run typecheck
npm --prefix runtime/artifact_upload_host test
```

The npm test command uses `node --import tsx --test` because the tsx CLI's IPC
socket is denied in restricted sandboxes. Do not infer runtime interception
from these unit/type checks.

No native tool-handler equivalence, same-name override coverage, live
provider-visible tool inventory, auxiliary-request routing, actual broker
routing, service behavior, presentation parser, or scenario calibration is
established. G01 authorizes supported-hook feasibility investigation only;
runtime pins and upstream sources remain unchanged. G02/G03 candidate research
and evidence gathering do not select a candidate, contract, authority, or quota.
