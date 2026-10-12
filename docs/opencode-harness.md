# OpenCode harness contract

The artifact-upload experiment is the repository's only experiment. This page
summarizes its OpenCode runtime contract; the [artifact researcher
handoff](artifact-upload.md) holds exact commands, platform, evidence and limits.

## Runtime pins

| Component | Pin | Role |
| --- | --- | --- |
| OpenCode | `1.14.41` (patched Darwin ARM64 host binary) | native coding agent on the host |
| Docker guest | Linux, CPython 3.11.15 / SQLite 3.40.1 | executes every intercepted coding tool |
| Python | `>=3.11` | harness, service, verifier, launcher |

## Runtime contract

This describes `eval.artifact_upload`, the sole experiment in this repository. See the
[artifact researcher handoff](artifact-upload.md) for exact commands/platform,
source freeze, evidence and open limits at accepted integration ccc0671.

Pinned OpenCode 1.14.41 runs natively on the Darwin ARM64 host, while every
supported coding operation runs through the pre-execution broker in its Linux
guest. Host session bookkeeping is limited to todowrite/invalid; unknown and
uncovered tools fail closed. Optional delegation, web, skill, question, LSP and
persistent/background interfaces are unsupported. Owned shell descendants are
cleaned up under guest kernel ownership; this is separate from interface support.

Each run has a private service volume and internal network distinct from guest
workspace storage. Canonical records retain raw output, presented output, exact
model request bytes and authoritative service audits. Immutable `tool-output://`
blobs are run-owned and mediated through read/search. Researcher redaction cannot
replace exact private replay. Source-at-rest tool digests and realized guest/
service/image witnesses have different scopes; `tool_digest`/`service_digest`
lock zeros are unused legacy placeholders, with identity carried by the launcher's
`source_identities` and the frozen 41/43-module source manifest. Frozen presentation records zero auxiliary calls and parser
coverage. Unknown cosmetic output passes through; execution paths fail closed.

The accepted worker-freeze receipts establish actual BOTH-condition quota
rejection and separate larger-quota functional publication with capture and
cleanup. The T016 security gate and final checkpoint are accepted and the
validator passes locally (43/0 fail/missing/unknown). Local
scripted controls and synthetic diagnostics certify engineering mechanics only;
no provider behavior or research readiness is claimed.
