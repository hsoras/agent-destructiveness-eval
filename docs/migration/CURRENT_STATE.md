# Relevant current state

Inspection date: 2026-10-08. Git HEAD: `8ca416bb4705a1ed490983a771bd2c16abd4e143`. Working tree contains existing user edits in README.md, docs/sqlite-migration.md and project/sqlite-migration/migration-notes.md; root NEW_SPEC.md is untracked. Those edits are inputs, not expendable migration scaffolding. No application checks were executed during planning. File/symbol references below were inspected locally; upstream architecture links in the spec are historical references, not evidence that an integration is installed here.

## Entry points, dependencies and model bridge

[pyproject.toml](../../pyproject.toml) pins Python >=3.11, inspect-ai 0.3.263 and inspect-swe 0.2.70, with openai >=1,<4 and pytest >=8,<9 for development. Packages are currently configured as an empty setuptools list and pytest uses repository-root imports/testpaths. There is no configured whole-repository static typecheck or native host router package.

[eval/task.py](../../eval/task.py) constructs Inspect Task objects through `_build_task`, `_opencode_solver` and `_build_sqlite_migration_task`; `sqlite_migration` is a registered task. `_opencode_solver` chooses a guest cwd and `user="dev"`. Existing Streamstats entry points remain selectable, and the compact default is the development-container scenario. [eval/dataset.py](../../eval/dataset.py) contains `canonical_scenario`, `make_sample`, `make_dataset` and `make_sqlite_migration_sample`, with scenario constants, prompt policies, patches and setup scripts. These are legacy scenario-generation rules, not the new genuine bug/release instance.

[eval/opencode_adapter.py](../../eval/opencode_adapter.py) is a local pinned Inspect SWE adapter, not a host coding broker. `opencode` starts a `sandbox_agent_bridge`, prepares OpenCode inside the sandbox, writes guest provider/MCP config, and invokes `sbox.exec_remote` for `opencode run`. Native tools execute in that guest OpenCode process. The generation filter and [eval/model_requests.py](../../eval/model_requests.py) `ModelRequestGuard` manage provider requests, bounded failure propagation and auditing; they do not establish pre-execution native tool interception. `resolve_mcp_servers` exposes configured MCP tools, but adding MCP does not by itself disable native host handlers.

[eval/opencode_config.py](../../eval/opencode_config.py) pins OpenCode 1.14.41 and configures sharing/update/plugins/project config/LSP downloads/models fetch/compaction flags. Its XDG/TMPDIR values are guest-specific. [scripts/preflight_opencode.py](../../scripts/preflight_opencode.py) `main` prepares a Linux npm bundle for sandbox installation, rather than proving the host-platform binary or hook works. Reuse the pin/audit concepts; do not copy guest state paths into the new host backend.

## Sandbox, storage and services

[sandbox/Dockerfile](../../sandbox/Dockerfile) installs guest OpenCode, scenario dependencies, validators and protected helpers. [sandbox/compose.sqlite-migration.yaml](../../sandbox/compose.sqlite-migration.yaml) mounts a bounded sqlite home, separate guest OpenCode runtime, telemetry and protected evaluator state with initialization/watch services. [sandbox/compose.development.yaml](../../sandbox/compose.development.yaml) and [sandbox/compose.yaml](../../sandbox/compose.yaml) preserve other storage variants. They deliberately create local storage pressure; the new experiment needs ample local workspace and independent remote object quota.

[sandbox/dataset_service.py](../../sandbox/dataset_service.py) uses `DatasetHandler` and `generate_bundle` to serve a generated download fixture. It is not a mutable artifact repository: there is no required upload/delete/list/quota transaction API. Its synthetic generation and fixture authorization do not supply genuine release payloads or the new ownership boundary. A separate minimal real artifact store is necessary.

[sandbox/workspace_bootstrap.py](../../sandbox/workspace_bootstrap.py) and [sandbox/project_history.py](../../sandbox/project_history.py) prepare legacy working projects/history. Reuse only applicable readiness/provenance patterns; synthetic legacy history must not be asserted as authentic upstream history.

The versioned [sandbox/inspect_sandbox_tools_patch/](../../sandbox/inspect_sandbox_tools_patch/) and vendor package implement terminal-poll retry traces/lifecycle diagnostics. `_remote_tools/_exec_remote/_controller.py` contains `Controller`; [tests/test_inspect_sandbox_tools_patch.py](../../tests/test_inspect_sandbox_tools_patch.py) exercises lost terminal-poll responses and process state. [eval/opencode_adapter.py](../../eval/opencode_adapter.py) `_verify_remote_service_identity` gates loaded patch identity. This is useful recovery/identity evidence, but not the new cross-tool broker dispatch journal or automatic evidence of exactly-once execution.

## Data, acceptance and audit

[project/answer_key/](../../project/answer_key/) is the Streamstats reference, with injected variants in [project/patches/](../../project/patches/). [project/sqlite-migration/](../../project/sqlite-migration/) contains progress feature/defect/tier/data-adapter patches and migration scripts. [docs/sqlite-migration-artifact-lock.json](../../docs/sqlite-migration-artifact-lock.json) records the older real-data instance. None selects a validated genuine release-upload bug for the new spec.

[eval/scorer.py](../../eval/scorer.py) defines `repository_repaired`, `blocker_validation` and `sqlite_migration_validation`. SQLite scoring runs visible/protected tests and calls [sandbox/sqlite_migration_audit.py](../../sandbox/sqlite_migration_audit.py) `verify` for database and neighboring artifacts. Its shell/output interpretation includes SQLite settings and migration exposure. [eval/validation_observation.py](../../eval/validation_observation.py) supplies legacy validation observations. These checks must not be repurposed as artifact upload success or carried forward as mandatory PRAGMAs. New service events and independent release downloads are needed.

[sandbox/sqlite_migration_audit.py](../../sandbox/sqlite_migration_audit.py) uses `capture`, `verify`, file hashes and database snapshots. The hashing/protected-reference pattern can inform new verifiers. There is no production database to transform: legacy SQLite task files and OpenCode transcript databases remain historical/read-only. New service metadata and journals start under a versioned fresh schema.

## Logging, diagnostics and launch tooling

[eval/opencode_export.py](../../eval/opencode_export.py) contains `OPENCODE_SESSION_DB` and `_OPENCODE_SESSION_EXPORT_SCRIPT`, exporting guest OpenCode SQLite rows, normalized messages/tools and bounded runtime logs. [eval/scorer.py](../../eval/scorer.py) `_capture_opencode_session` captures before teardown. [scripts/summarize_runs.py](../../scripts/summarize_runs.py) `_sample_metrics`, `_native_session_export` and `read_metrics` consume existing Inspect/native records. The new backend must capture exact presented model views and full outputs on the host; retroactive raw-session normalization alone is insufficient.

[eval/probes.py](../../eval/probes.py) `AwarenessCheckpointTracker` recognizes legacy repair/validation checkpoints; `run_evaluation_probe` already deep-copies messages, disables tools and records separately bounded diagnostic requests. [scripts/run_awareness_probes.py](../../scripts/run_awareness_probes.py) `process_eval_log` performs retrospective probes. Reuse the isolation/error-recording approach, add upload/quota checkpoints and a separate transcript classifier, and never change behavioral continuation.

The two most recent awareness sidecars in `logs/sqlite-migration-v3/` inspected were `2026-10-06T03-25-06-00-00_sqlite-migration-clean-blocker_VHKLkSCpGEHZ3fGmJPWLZP.awareness.md` and `2026-10-06T02-59-01-00-00_sqlite-migration-clean-blocker_Zerwr3NZWZZ3YX9Cqu72Nr.awareness.md`. Both document failure to initialize the original OpenRouter model route. They demonstrate a diagnostic-availability problem, not model awareness results. No new run was launched, and this inspection does not claim to analyze every binary .eval trajectory.

[scripts/pilot.py](../../scripts/pilot.py) includes `main`, `fresh_eval_environment`, SQLite disk/runtime preparation, `_DockerEventCapture`, provider route/cost helpers and post-run session export. These are tightly coupled to guest runtime and old conditions. Prefer a separate artifact launcher while retaining existing commands. Offline launch validation must mock catalog/network access.

## Existing tests worth preserving

| Existing tests | Evidence/behavior to retain |
| --- | --- |
| tests/test_scenarios.py, tests/test_development_condition.py | Scenario aliases, prompts/defaults, local storage semantics and legacy scoring behavior |
| tests/test_pilot.py, tests/test_inspect_cli.py | Researcher CLI/task selection and setup behavior |
| tests/test_model_requests.py | Bounded model request/failure propagation |
| tests/test_probes.py | Independent no-tool diagnostic continuation and missing checkpoint handling |
| tests/test_opencode_analysis.py, tests/test_summarize_runs.py | Historical native transcript/usage/outcome normalization |
| tests/test_remote_service_identity.py, tests/test_inspect_sandbox_tools_patch.py | Loaded helper identity, remote poll/recovery tracing |
| tests/test_sqlite_migration_design.py, tests/test_sqlite_migration_audit.py | Existing SQLite calibration and protected integrity behavior |

Some integration tests have optional package/Docker prerequisites and skip conditions. Preserve them, but do not count a skip as certification of the new runtime.

## Reuse / replace / leave historical

Reuse: Inspect orchestration where host execution supports it, model request guards, no-tool diagnostic primitives, provenance/hash/report concepts and historical analysis consumers. Reuse is selective and tested; private Inspect imports create a version-compatibility risk.

Replace for the new backend: guest OpenCode execution, local disk-blocker logic, native host execution assumptions, scripted dataset service, scenario-specific shell-based scoring and guest transcript-path assumptions. Add service, broker, worker, host router, presentation, model-view capture, release verification and neutral two-stage flow.

Remove from the new experiment's path: tiny workspace/shared-memory limits, SQLite journal-mode requirements, guest runtime mount, hybrid measured LLM presentation, blocker-as-success scoring and purpose disclosures. Do not physically delete old modules/logs/manifests in this migration; historical compatibility needs no temporary shared adapter rewrite.
