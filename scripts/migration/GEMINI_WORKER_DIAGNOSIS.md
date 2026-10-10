# Gemini worker diagnosis and verified repair, 2026-10-09

Gemini 3.8 Flash Medium completed a genuine isolated source edit, unittest, pytest,
Makefile build, protected-write negative tests, Docker guest build/run, structured
handoff, trusted coordinator commit, independent Sol Medium approval, and
idempotent recovery. No migration state or Boss checkout was changed or merged.
The verified report is
`/private/tmp/gemini-scoped-verified-e2e-20261009/report.json`.

## Root causes and repair

The recorded failure was execution policy and filesystem scope, not authentication
or demonstrated quota exhaustion. The global rules did not allow the absolute
project Python/test commands. The virtualenv executable additionally resolves to
`/Users/sarosh/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11`,
outside the virtualenv. Exact command grants and narrow read-only mounts for both
the virtualenv and resolved runtime are necessary. A plain `python3` grant does
not match the absolute executable. Ask/Deny retain precedence over Allow.

`worker_environment.py` creates a new UUID project for each attempt, with only its
assigned worktree as a writable workspace. It selects approved Python unittest/
pytest, make and npm development commands from the existing worker check list;
other checks stay with the coordinator. Dependencies and trusted guest-client
code are mounted read-only. Existing global/shared/project settings are not edited.
`providers.py` preserves inherited restrictions and rejects every inherited
unsandboxed Allow grant. It validates the selected project's ID, workspace and
policy hash. CLI 1.3.2's zero-turn `/permissions` shortcut omits the selected
project; actual conversation initialization loads it, as the real tests confirm.

Adding `unsandboxed(*)` to Deny proved counterproductive in this CLI version: it
also denied ordinary sandboxed Python. After explicit user authorization, a fresh
disposable project without that new rule passed the real Python probe. The sandbox
remains mandatory, and there are no unsandboxed Allow grants. A separate genuine
bypass request was denied by the CLI permission engine despite terminal SUCCESS
and exit 0; `/private/tmp/gemini-worker-project-probe/bypass-negative.jsonl` preserves
that evidence. The denial parser treats this as failure, never successful work.

The outer coding-agent sandbox prevented CLI localhost initialization and account
log writes. `job.py` checks localhost readiness and runs agy in the already
operator-authorized coordinator execution environment. It never escalates itself;
`--sandbox` still encloses worker terminal commands. Normal Codex/Luna sandboxing
and mandatory coordinator checks remain intact.

Linked worktree Git metadata is outside the workspace. In the verified fixture,
`repo/.git/worktrees/T001/{HEAD,index,commondir}` points to the shared
`repo/.git/{config,objects,refs/heads/migration/T001-state}`. Full absolute paths are
recorded in `worker_project.git_boundary`. Granting unrestricted shared Git access
would expose other tasks. The worker therefore leaves edits uncommitted. Existing
`Coordinator.verify_worker` validates scope, HEAD/branch, mandatory checks and
JUnit evidence before creating a clean child commit. Both native and terminal
Git mutation are denied, and the common Git directory is not mounted. Actual
subprocess probes confirm protected Git and receipt writes are denied.

## Docker and evidence boundaries

`docker_guest.py` brokers only bounded build/run operations through a file queue.
The worker receives request-write and response-read access, never host Docker or
OrbStack IPC. The trusted runner addresses the explicitly named guest daemon.
It refuses machines without isolation, isolated networking, disabled SSH forwarding,
empty mounts, and CPU/memory limits. It uses only approved cached Python image
IDs, fixed recipes, explicit regular source files, and no build network. Run uses
no network/mounts, read-only root, unprivileged UID, dropped capabilities,
no-new-privileges, and CPU/memory/PID limits. Descriptor-relative no-follow opens
prevent source/request races; input/output limits and owned-process cancellation
bound resource use. Failed runs remove only the session's exact container name.

The prepared disposable machine is `gemini-worker-smoke-20261009`, two CPUs,
2 GiB RAM, no mounts, no SSH forwarding; Docker daemon ID is
`7541c977-dd0a-4b7e-9878-c48c87bdaecd`. No host daemon or unrelated machine was
modified. OrbStack isolated machines share its Linux kernel; this is not a separate
adversarial VM. The current broker deliberately supports the fixed cached Python
unittest probe recipe. Arbitrary Dockerfiles, compose, privileged Docker flags,
application integration checks and dependency/network installation are not worker
capabilities; those remain trusted coordinator operations. Provisioning and new
recipe/image authorization are explicit operator work, never automatic.

The initial independent workflow review correctly rejected a missing coordinator
receipt for the sandbox boundary test. That test cannot demonstrate denial in an
unrestricted host process. The final harness separately launches it through agy
in the same verified sandbox project, requires exactly the expected command and
working directory, compares policy/test hashes and snapshots before/after, and
retains a hashed coordinator control receipt. The independent Sol reviewer then
approved all four acceptance criteria. Original normal coordinator unittest,
pytest, make, scope, JUnit and commit checks were preserved.

## Verification and limitations

- Verified workflow report: `/private/tmp/gemini-scoped-verified-e2e-20261009/report.json`.
  Task commit: `28e80718f65393780e0dec41ef37147c427f1c13`, owner trusted coordinator.
  Only probe.py changed; fixture baseline stayed unchanged; recovery did not rewrite
  completed state; no merge occurred.
- Actual worker receipts: `state/tasks/T001/worker-1-exit.json` and `worker-1.jsonl`
  under that artifact root. The structured handoff records four passing development/
  boundary commands and both guest operations. Tool output proves execution.
- Independent sandbox control: `state/tasks/T001/worker-1-job.json.control/receipt.json`.
  Policy/test/log hashes and unchanged snapshots are retained; actual output is
  `Ran 1 test` and `OK`. The final strict working-directory validator accepts all
  six real worker commands and the one control command.
- Guest receipt summary: `state/tasks/T001/worker-1-job.json.guest/summary.json`.
  Build/run exit 0 with the same image, daemon and independently verified source
  hashes; guest unittest reports one test and OK.
- Independent workflow review: `state/tasks/T001/review-1-result.txt`.
  All AC1–AC4 approved. Independent read-only patch review is also retained through
  the Sol Medium review session; it found and verified fixes for races, cleanup,
  receipt recovery and command working-directory validation. Final independent CLI
  review approved the patch at
  `/private/tmp/gemini-verified-infrastructure-review/result.json`. It noted the
  failed intermediate suite at `/private/tmp/gemini-scoped-delivery-tests.log`,
  which used a stale control fixture while Cwd validation was updated. That run
  is retained as failed evidence, not counted as acceptance.
- All 79 original safety cases remain, including exit-0 denial detection, genuine
  provider-failure fallback, rejection of application-failure fallback and recovery.
  The 96-test suite passed in 229.358 seconds at
  `/private/tmp/gemini-scoped-authorized-tests.log`. The final expanded acceptance suite passed **97 tests in 239.937 seconds** at
  `/private/tmp/gemini-scoped-acceptance-tests.log`; final focused checks are at
  `/private/tmp/gemini-scoped-final-focused-tests.log` (18 passed).
- Prior genuine Luna fallback remains preserved at
  `/private/tmp/gemini-worker-fallback-real-20261009/report.json`. No fallback,
  review or coordinator validation gate was removed.

Reproduce only in a new disposable artifact root from an authorized host execution
environment (this consumes model allowance):

```sh
/Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python -B \
  scripts/migration/scoped_worker_smoke.py \
  --state-dir /private/tmp/new-gemini-scoped-smoke \
  --docker-machine gemini-worker-smoke-20261009
```

No purchases, API keys, billing settings or global permissions were changed.
A collaboration Sol review hit its usage limit; the final cached CLI Sol Medium
review completed and approved without purchases. Gemini calls completed successfully.
Remaining subscription allowance and billing-plan identity are unknown. Proven
readiness applies to this installed CLI/account/runtime and scoped workflow;
new dependencies, build tools or Docker recipes need their own narrow grants and
verification. Protected tests are immutable controls, not an assertion that arbitrary
repository code is safe for unrestricted trusted-host execution.

## Safe integration and coordinator reload — operator only

The patch keeps `providers.json`, `config.json` and `orchestrate.py` unchanged.
`providers-gemini-scoped.json` is a separate opt-in routing file with scoped
permissions enabled; it retains Luna fallback and Sol Medium review. It does not
silently enable a Docker guest for real migration tasks. Use the broker only for
an explicitly configured, reviewed disposable guest context.

1. Let current workers/reviewers reach terminal receipts and the coordinator release
   its lock. Preserve the Boss's local edits. If scheduling must be stopped, the
   operator stops only its confirmed coordinator PID gracefully at a safe boundary;
   never kill detached jobs, clear locks/state, or alter active task worktrees.
2. Review and cherry-pick the isolated infrastructure commits into the selected
   coordinator source checkout. Apply only commits not already integrated: diagnostic
   `d19ef56`, gated environment/broker `6eec0f1`, and the final verified commit listed
   in the delivery. Do not reset/rebase the Boss's branches or merge this branch
   automatically. Run the full suite from that patched source:

   ```sh
   /Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python -B \
     -m unittest discover -s scripts/migration -p 'test_*.py' -v
   ```

3. Preserve the original frozen config at
   `/Users/sarosh/programming/agent-destructiveness-eval/scripts/migration/config.json`.
   Its digest matches the state's recorded
   `8f94916d48bbed5437079b2ba5abcac61b89653bfac7f0fa2ae9522e3901361d`
   after the same `{repo}` Python-path expansion used by the coordinator. An initial
   raw-JSON comparison omitted that expansion and incorrectly suggested mismatch;
   the normalized read-only check corrected this. Reverify before restart:

   ```sh
   /Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python -B - /Users/sarosh/programming/agent-destructiveness-eval/scripts/migration/config.json <<'PYCONFIG'
   import hashlib, json, sys
   from pathlib import Path
   state = json.loads(Path('/Users/sarosh/programming/agent-destructiveness-eval/.git/migration-orchestrator/state.json').read_text())
   config = json.loads(Path(sys.argv[1]).read_text())
   config['python'] = config['python'].replace('{repo}', state['repo'])
   assert hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest() == state['config_digest'], 'Frozen config mismatch; do not resume'
   print('Frozen config verified')
   PYCONFIG
   ```

4. Processes do not hot-reload. After safe shutdown and integration, the operator
   performs read-only status with the patched source, the same repository/state/
   frozen config, and either the unchanged original providers policy or an explicit
   switch to the new scoped policy. Pending jobs retain their stored commands/routes.
   The activation command is:

   ```sh
   /Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python -B \
     /patched/coordinator/source/scripts/migration/orchestrate.py status \
     --repo /Users/sarosh/programming/agent-destructiveness-eval \
     --state-dir /Users/sarosh/programming/agent-destructiveness-eval/.git/migration-orchestrator \
     --config /Users/sarosh/programming/agent-destructiveness-eval/scripts/migration/config.json \
     --providers-config /patched/coordinator/source/scripts/migration/providers-gemini-scoped.json
   ```

   Preserve the recorded workspace root
   `/Users/sarosh/programming/.agent-destructiveness-eval-migration-worktrees`.
   Do not move/recreate worktrees, repair real tasks, overwrite original providers
   files, or rewrite frozen input/config digests merely to enable this patch.
5. Only the Boss/operator with migration execution authority resumes, from a host
   environment permitting localhost and account-local CLI initialization:

   ```sh
   /Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python -B \
     /patched/coordinator/source/scripts/migration/orchestrate.py resume \
     --repo /Users/sarosh/programming/agent-destructiveness-eval \
     --state-dir /Users/sarosh/programming/agent-destructiveness-eval/.git/migration-orchestrator \
     --config /Users/sarosh/programming/agent-destructiveness-eval/scripts/migration/config.json \
     --providers-config /patched/coordinator/source/scripts/migration/providers-gemini-scoped.json \
     --max-workers 1 --approved-execution
   ```

   Substitute only the selected patched source path and preserve explicitly chosen
   existing concurrency. Resolve reported existing blockers through the established
   audited recovery procedure; do not use `run` to start over or automatically
   relaunch a persisted uncertain/partial attempt. Mandatory reviews, integration
   checks and wave validation remain authoritative. These are operator instructions;
   no migration status/resume/repair action was executed during this infrastructure
   task, and no matching coordinator process was signaled or restarted.

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
