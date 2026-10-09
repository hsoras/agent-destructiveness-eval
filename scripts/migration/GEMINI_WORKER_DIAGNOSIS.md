# Gemini worker diagnosis, 2026-10-09

Gemini implementation readiness is **not established**. The real worker edited a
Python file correctly, but did not run its test or produce a commit. This patch
hardens diagnosis and fallback; it does not claim to repair account permissions.

## Follow-up execution repair status

The execution repair is still **unverified and disabled by default**. The following
work remains on the isolated branch after `d19ef56`; do not enable the scoped route
or claim Gemini readiness based on the coordinator-only Docker result.

- `worker_environment.py` creates a fresh UUID project with one task workspace,
  exact development argv grants, read-only virtualenv **and resolved runtime**
  dependencies, protected Git/evidence paths, and explicit broker queue access.
  It never modifies global/shared settings or an existing project. Python, pytest,
  make, and normal npm development targets are eligible; privileged checks remain
  coordinator-only. Every original mandatory coordinator check remains intact.
- `job.py` checks localhost initialization in its existing execution environment,
  forces the internal sandbox, records the selected project and exact launch argv,
  requires structured results, and preserves a terminal receipt even when guest
  cleanup fails. It does not escalate itself. The launcher must already run in an
  operator-authorized host environment.
- `providers.py` preserves global/shared restrictions and verifies the selected
  UUID project bytes. CLI 1.3.2's zero-turn `/permissions` shortcut omits selected
  project grants; actual conversation startup loads them. Any inherited
  unsandboxed Allow grant rejects the scoped route. Observed unsandboxed Deny/Ask
  overlap now rejects preflight rather than falsely marking commands ready.
- Git ownership stays with `Coordinator.verify_worker`, which validates edits,
  scope, branch/HEAD and mandatory tests before committing. The project receipt
  identifies the private worktree Git directory (HEAD, index, commondir) and common
  Git directory (branch ref, objects, config). Shared metadata is not mounted;
  native and terminal Git mutation is denied. Actual subprocess protection still
  needs the negative worker probe.
- `docker_guest.py` exposes only fixed build/run operations over a bounded file
  queue. The trusted coordinator uses the named isolated OrbStack machine's own
  Docker daemon. Source descriptors are pinned without following symlinks; request
  reads and subprocess output are bounded. Build uses a cached approved image ID
  and no network; run has no network/mounts, read-only root, unprivileged UID,
  dropped capabilities, no-new-privileges and CPU/memory/PID limits. No worker sees
  a host Docker socket or unrestricted daemon command. OrbStack isolated machines
  share its Linux kernel; this is not a separate adversarial VM.
- `scoped_worker_smoke.py` requires actual edit/unittest/pytest/make tool evidence,
  negative subprocess tests for Git and trusted receipts, guest build/run lineage,
  a clean coordinator-owned task commit, structured output, independent Sol review,
  unchanged fixture baseline and idempotent state recovery. It never merges.

The first scoped E2E attempt failed with sandbox `Operation not permitted` when
executing the virtualenv interpreter:
`/private/tmp/gemini-scoped-e2e-20261009/report.json`. Its executable resolves outside
`.venv`, so a narrow read-only runtime grant was added. The next disposable probe
was then denied before execution by `unsandboxed(*)`, even though its call requested
ordinary sandboxed Python execution:
`/private/tmp/gemini-worker-project-probe/runtime.jsonl`. Exit 0/SUCCESS is not a
passed test. The resolved runtime grant is therefore not yet validated.

Automatic approval review rejected a proposed fresh disposable project probe
without that explicit Deny, stating it could weaken the worker boundary. No
workaround was executed. User authorization was requested to test the documented
CLI behavior with `--sandbox`, no unsandboxed Allow grants, and headless bypass
requests denied. Until resolved, this blocks real Python and end-to-end worker
verification. It does not establish an authentication or quota failure, or prove
that all safe Antigravity deployments are fundamentally impossible.

The trusted broker's actual hardened precheck passed build and run:
`/private/tmp/gemini-broker-hardened-precheck.log`. It used only
`gemini-worker-smoke-20261009`, daemon
`7541c977-dd0a-4b7e-9878-c48c87bdaecd`, with no host daemon changes. This machine is
operator-provisioned (isolated, network-isolated, SSH forwarding disabled, no
mounts, two CPUs and 2 GiB RAM) with Docker installed and `python:3.11-slim` cached.
Provisioning is not automatic; existing machines are never reconfigured by the
broker. The original 79 safety cases remain; new tests cover policy/runtime
paths, inherited restrictions, localhost failure, structured receipts, descriptor
races, bounded I/O, container cleanup and failed project/cleanup recovery.

After authorization, reproduce only in a fresh disposable artifact root from an
already authorized host execution environment:

```sh
/path/to/python3.11 scripts/migration/scoped_worker_smoke.py \
  --state-dir /private/tmp/new-gemini-scoped-smoke \
  --docker-machine gemini-worker-smoke-20261009
```

Do not update the Boss's frozen configuration, providers policy, persisted state,
or running processes to try this. `providers.json`, `config.json` and
`orchestrate.py` remain unchanged. Keep Luna fallback and independent review.
The integration/reload instructions below describe the existing diagnostic
commit; **do not integrate or activate this unverified execution repair yet**.
Independent read-only GPT-6.1 Sol Medium review found no remaining code blockers
in its final pass; it explicitly withheld Gemini readiness approval pending real
worker filesystem probes and the full E2E. The last full test run is retained at
`/private/tmp/gemini-scoped-final-tests.log` (95 tests, 229.129 seconds, all 79
existing safety cases included); the latest 17 focused scoped/broker
checks pass, including cancellation during an active operation and skipping queued
requests. The real final file-queue precheck passed at
`/private/tmp/gemini-broker-queue-final.log` with build and run receipts at
`/private/tmp/gemini-broker-queue-final/queue/summary.json`. These remain trusted
coordinator checks, not Gemini worker successes.

## Historical evidence for diagnostic commit d19ef56

## Evidence and root cause

Infrastructure baseline: `0d421fd`; isolated branch: `infra/gemini-worker-fix`.
No Boss checkout, integration branch, live task branch, runtime configuration,
or persisted migration coordinator state was edited. Disposable artifacts remain
under `/private/tmp`.

- Prior authentication receipt: `/private/tmp/gemini-auth-readiness-20261009/exit.json`.
  Cached-account headless execution returned READY. Model listing is available;
  authentication or quota is not the recorded implementation blocker.
- Recorded T002/T004 preflights under the original checkout's
  `.git/migration-orchestrator/tasks/` reject the absolute project Python/pytest
  commands. The existing global grants cover selected Git commands and `find src`,
  with no Python, Docker, or network grants. A `python3` prefix would not match the
  configured absolute `.venv/bin/python` executable.
- Outer sandbox probe: `/private/tmp/gemini-worker-real-20261009/`.
  Antigravity 1.3.2 cannot initialize its localhost listener (`127.0.0.1:0` bind
  denied), and account-local log writes are blocked. Launching the CLI from a
  trusted host context resolves initialization; its worker `--sandbox` remains on.
- Final Python 3.11 worker: `/private/tmp/gemini-worker-real-final-20261009/report.json`.
  It reproduces the absolute project interpreter denial with the final probe code:
  edit correct, coordinator test exit 0, worker test evidence false, commit absent,
  isolated true, readiness false.
- Earlier real host worker: `/private/tmp/gemini-worker-real-20261009-host/report.json`.
  `probe.py` changed from returning 0 to returning 42. Its unchanged unittest passes
  independently under the coordinator. The worker's Python command is soft-denied;
  terminal status SUCCESS and process exit 0 accompany an empty response and
  `denied_actions`. The turn stops before Git. This is **not** a worker test pass.
- Separate Git probe in the same disposable fixture: `git-job.json`, `git.jsonl`,
  and `git-exit.json` in that artifact directory. `git diff --check`, `git add`, and
  `git commit` all report `fatal: not a git repository` naming the linked worktree's
  shared Git directory outside its workspace. HEAD stays at the baseline. A schema-
  valid `structured_output` describes the failures; the freeform response contains
  additional tool metadata, so consumers must retain the structured field separately.
- The previous mixed documentation smoke deliberately executed `git diff --no-index`
  outside the checkout and left committing to the coordinator. It demonstrates a
  documentation workflow, not Python/test/commit permissions.

Official references consulted:
[CLI headless permissions](https://www.antigravity.google/docs/cli/headless/),
[permission matching](https://www.antigravity.google/docs/permissions?tab=cli),
[terminal sandbox](https://www.antigravity.google/docs/sandbox?tab=cli),
[permission scopes](https://www.antigravity.google/docs/cli/commands/permissions).
Ask/Deny override Allow. Literal rules match tokens, not arbitrary executable
basenames. Some shell constructs disable prefix matching; regex semantics are
CLI-specific. The CLI exposes project, shared, and global scopes through its
read-only `/permissions` command. Host resources, Docker sockets, and network
access are separate from workspace editing; coordinator integration checks must
remain trusted operations.

## Patch

- `providers.py`: inspect effective rules with a bounded, zero-agent-turn
  `agy -p /permissions --output-format json`; fail closed on unknown/incomplete
  scope data, malformed rules, broad wildcard grants, and uncertain shell syntax.
  Preserve authoritative structured output separately from response text. Recognize
  explicit permission-engine tool errors even without a terminal event.
- `job.py`: use those effective rules for checked worker preflights. Reject an
  alternate `permissions_settings` file, because the CLI has no argument in the
  existing launch that actually loads it. Persist a serializable failure receipt
  so genuine policy failures can safely enter the existing Luna fallback.
- `worker_smoke.py`: new real edit/test/commit probe in a new standalone fixture
  and linked worktree. It requires actual worker command evidence, an independent
  test pass, exact edit/scope, one clean child commit, unchanged fixture baseline,
  and valid structured output before marking readiness. No settings writes,
  dependencies, unsandboxed retries, Docker, or network operations are requested.
- `test_providers.py`: effective-scope precedence, inspection failures and alternate
  files, literal/unsafe matching, exit-0/missing-terminal denials, structured output,
  and false-positive smoke rejection. Existing fallback, review, coordinator checks,
  and recovery remain mandatory. `orchestrate.py` and `config.json` are unchanged.

For a new, separately reviewed frozen configuration, `worker_checks` may explicitly
name focused local developer commands. Do not copy Docker/public/integration
commands into that list merely to satisfy preflight. `checks`, `public_checks`,
regression checks, wave checkpoints, and independent review still run at the
coordinator. An already active frozen configuration must not be rewritten to adopt
this separation. Production workers still leave changes uncommitted; the trusted
coordinator verifies scope and checks before committing. The disposable probe's
commit authorization does not change that production invariant.

No documented per-invocation narrow command grant was found in the installed CLI
help. This patch does not change global/shared/project permissions, add the Boss
checkout as a workspace, expose shared Git metadata, or enable Turbo/permission
bypass. The current policy therefore remains unsuitable for unattended Python
implementation and worker-owned commits. A future narrowly scoped project policy
or different isolated repository arrangement needs its own review and a passing
real smoke before enabling Gemini for such work. This diagnosis does not assert
that every possible Antigravity deployment is incapable of safe implementation.

## Verification and billing

Use the existing project Python 3.11 interpreter to run infrastructure tests:

```sh
/Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python \
  -m unittest discover -s scripts/migration -p 'test_*.py' -v
```

The initial system Python test attempt used Python 3.9 without pytest and also
caught a syntax error in a new test; both were addressed. That attempt failed;
it is not the acceptance run. Final test and independent review results are in the
delivery report. The final full Python 3.11 suite passed **79 tests in 228.332 seconds**:
`/private/tmp/gemini-worker-fix-final-tests.log`. The earlier corrected full run
passed 76 tests; four focused evidence/failure-path tests also passed. Independent
read-only GPT-6.1 Sol Medium review found no remaining blockers (its test
contingency is satisfied). The real Luna fallback report passed permission failure,
Luna fallback, independent reviews, coordinator validation, idempotent recovery,
and fixture baseline preservation.
Read-only Docker/network policy inspection is retained at
`/private/tmp/gemini-worker-restrictions.json`: neither command has a grant and no
network domains are granted; no Docker/network commands were executed.
Real Gemini implementation evidence above intentionally remains a
failed readiness result. A separate disposable real Luna fallback fixture is at
`/private/tmp/gemini-worker-fallback-real-20261009/`; its report records actual
permission preflight failure, retained equal before/after snapshots, real Luna
implementation, independent Sol reviews, coordinator validation, and idempotent
recovery when complete.

Reproduce implementation readiness only in a new disposable directory:

```sh
/path/to/python3.11 scripts/migration/worker_smoke.py \
  --state-dir /absolute/new/disposable/artifact-directory
```

Run the CLI launcher in a host context that permits its local initialization.
The probe keeps the worker sandbox enabled and exits 1 unless all required evidence
passes. It never starts the application's migration graph. Model calls consume the
existing account allowance. No purchases, API keys, Vertex project, credit setting,
or billing configuration was changed. Remaining subscription allowance and actual
billing-plan identity are unknown; token receipts do not certify either.

## Safe integration and reload (operator only)

1. Record the current coordinator launch arguments, source checkout, state directory,
   config and provider-policy paths. Wait until active worker/reviewer jobs have
   terminal receipts and the coordinator releases its lock. If it remains actively
   scheduling, stop only the confirmed coordinator PID gracefully at a safe boundary;
   detached jobs remain alive. Do not kill runner/child processes or clear state.
2. Preserve the Boss's uncommitted edits using the Boss's existing workflow. Review
   `git diff` before cherry-picking; this patch modifies neither its orchestrator nor
   frozen configuration, but the README may overlap local edits. Integrate only after
   review, from the chosen infrastructure/coordinator source checkout:

   ```sh
   git cherry-pick "$(git rev-parse infra/gemini-worker-fix)"
   /path/to/python3.11 -m unittest discover -s scripts/migration -p 'test_*.py' -v
   ```

   Do not cherry-pick into active task worktrees, reset branches, delete state, or
   change frozen config/input digests. Handle any overlap manually with the Boss.
3. Existing running coordinator/runner processes do not hot-reload modules. Existing
   jobs retain persisted commands/routes; integrating a patch does not rerun them.
   Inspect read-only status using the patched source and the **same** arguments:

   ```sh
   /path/to/python3.11 /patched/source/scripts/migration/orchestrate.py status \
     --repo /original/repo --state-dir /existing/state \
     --config /original/config.json --providers-config /original/providers.json
   ```

4. Only the Boss/operator with migration execution authorization may resume, after
   resolving any reported blocker using the established recovery procedure:

   ```sh
   /path/to/python3.11 /patched/source/scripts/migration/orchestrate.py resume \
     --repo /original/repo --state-dir /existing/state \
     --config /original/config.json --providers-config /original/providers.json \
     --max-workers 1 --approved-execution
   ```

   Substitute the exact existing paths and preserve an explicitly chosen concurrency
   limit. Do not regenerate provider policy or use `run` to start over. Partial Gemini
   work remains blocked pending an audited continuation plan; completed receipts and
   independent review/coordinator gates remain authoritative. Keep Luna as the
   implementation route until a genuine Gemini edit/test/commit smoke passes.
