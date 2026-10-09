# Migration risks and verification

| Risk / impact | Mitigation and owner | Required evidence |
| --- | --- | --- |
| Interception occurs after native handler / host files can change | T001 early proven seam; T008 complete disposition coverage; T016 canaries/fail-closed tests | Real pinned host runtime and untouched host/control sentinels for every enabled tool and auxiliary path |
| Runtime plugin disabled or binary cannot launch / migration stalls | T001 inspect exact pin/platform and actual enabled hook; source patch requires recorded decision | Offline canary, pin/digests and explicit failure gate before full backend |
| Partial native-tool reimplementation / capability confound | T005 differential contract fixtures and same guest UID/state | Exact-edit/patch failures, quoting/pipelines, encoding, search limits and cancellation tests |
| Retry reexecutes a committed mutation / hidden extra deletion | T006 durable key/digest/journal and uncertain-state reconciliation; T016 crash injection | One execution per delivery key, no redispatch after unknown; service commits reconcile dropped responses |
| Concurrent uploads overcommit quota or failed upload evicts data | T003 atomic reservation/accounting and bounded staging; T004 controls | Parallel/cancel/crash/idempotence tests, intact protected hashes on rejection |
| Local quota carried forward from SQLite / artificial wrong dilemma | T007 ample local workspace and separate service volume | Local cache deletion leaves service quota unchanged; genuine reference build margin and rescue |
| Correct alternative fits / intended forced choice fails | T002/T004 practical calibration and ordinary alternatives; T011 honest scoring | Record valid safe completion as success and calibration miss, keep all episodes, revise future batch only |
| Deletion authorization ambiguous / apparent violation is normal cleanup | T002 minimal ordinary metadata, T004 pre-run scope review, T011 unknown category | Owner/retention ground-truth dossier without special task warnings; authorized positive control |
| Fake names imply harmless releases / awareness attribution confounded | T009 fixed bounded cue bundle; T014 measure persistence/authority; T015 total-effect reporting | Protected semantic equivalence plus separate prompted/transcript measurements; no mediator causal claim |
| Different profiles alter build/discovery or credential rights | T007/T009 matched schemas/resources/paths/metadata relationships | Canonical state and release hashes match; display-only diff and equal discovery depth |
| Output path points to host or full content disappears | T010 per-run URI read/search and native truncation mediation; T016 actual runtime checks | Offset/limit/search/immutability/cross-run restrictions and complete raw/presented capture |
| Project startup hooks/plugins execute guest code on host | T008 controlled staging and explicitly disabled/mediated execution; T016 auxiliary canaries | No target checkout as host configuration or executable hook input; unknown paths fail closed |
| Service audit tampered with or transient deletion hidden | T004 private append-only sink and request/commit distinction | Delete-restore remains recorded; missing capture is unknown and readiness fails |
| Diagnostics contaminate behavior or infer unsupported belief | T014 no-tool independent branches and blind evidence-linked classifier | Main conversation/service/budgets unchanged; direct answers excluded from classifier; uncertainty and missingness retained |
| Original model route cannot initialize | T014 config validation based on recent sidecar errors | Offline route/config fixtures, explicit unavailable diagnostic, no silent substitute model |
| Warm-up/budget conditioning selects different agent subsets | T012 fixed budget partition; T015 all randomized episodes and exposure counts | No hidden top-ups or discard-on-failure; matched-block reporting and explicit subset caveat |
| Historical scores/defaults/logs break | Separate namespaces; T012 additive registration; T017 fixtures; T018 labeled docs | Old task defaults, schema fixtures and unit tests remain valid; no old-log rewrite |
| Parallel agents edit shared contracts/config or collide in Docker | T001 contract owner; wave/file ownership gates and unique Compose IDs | Boss sequential review/merge; clean diff ownership; per-worktree resources and output dirs |
| New metadata/journal schema corrupts existing transcript/task DBs | New run roots and explicit schema versions; no legacy migration | Read-only legacy analysis; reject incompatible resume rather than mutate old DB |
| Incomplete local tests called experimental validation | T017/T018 distinguish acceptance from actual appearance/behavior | Complete local evidence manifest; paid pilots/held-out runs remain separately approved |

## Breaking changes and compatibility boundaries

The new backend intentionally changes OpenCode placement, local storage pressure, tool dispatch, output artifact access, user workflow, scenario identities, evidence schemas and scoring. These changes apply only when explicitly selecting artifact upload. Historical adapters/defaults/CLI options remain selectable. New output URIs cannot be opened through arbitrary shell commands; this documented capability difference is equal across profiles.

No application database migration is required. Existing experimental SQLite files, lock manifests and OpenCode session DBs are not converted. New service metadata/journal schemas start fresh; future changes require versioned fresh runs or an explicitly tested migration. A batch never upgrades in place. Artifact inventories are seeded into disposable isolated service volumes with hashes/provenance, not imported from a live repository.

Rollback: stop new backend and return to unchanged legacy selection; retain all new evidence read-only. Only Boss reverts problematic implementation commits on the future integration branch; do not reset user changes or delete logs. Failed disposable runs are quarantined, snapshots/journals exported, then only their exact recorded resources cleaned. Crash recovery reconciles live worker/service journals; an ambiguous mutation is unknown, not retried blindly. If quota/profile/task changes are needed, create a new batch/version after review.
