# Migration task status

**Boss-only during execution.** Execution evidence is recorded below after independent review and integration checks. `ready` means graph-ready after plan approval; every other task is blocked on successfully integrated dependencies, not permanently blocked. Allowed statuses: `blocked`, `ready`, `in_progress`, `in_review`, `verified`, `merged`. Read-only reviewers report findings to Boss and never edit this file. Assignments and verified commits are recorded below.

| Task ID | Title | Dependencies | Wave | Status | Assigned worker | Branch/commit | Review status | Verification status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [T001](tasks/T001.md) | Freeze contracts and prove the interception seam | — | 1 | merged | gpt-6-luna | migration/T001-migration-orchestrator / cc7a114b5465be169c75d332c973c61648ae0788 / merge dd6ef889042cdc3583c1552e13b371b128d0e0b0 | approved gpt-6-sol | passed; evidence /Users/sarosh/programming/agent-destructiveness-eval/.git/migration-orchestrator/checks |
| [T002](tasks/T002.md) | Select and prepare the genuine repair and release instance | T001 | 2 | blocked | unassigned | | not_started | not_run |
| [T003](tasks/T003.md) | Implement the minimal real artifact service | T001 | 2 | merged | gpt-6-luna | migration/T003-migration-orchestrator / ef61f84a30af8137002b43cf6150a372c251655c / merge 663b7c2880f51d52d341ea715a0e011677b22a65 | approved gpt-6-sol | passed; evidence /Users/sarosh/programming/agent-destructiveness-eval/.git/migration-orchestrator/checks |
| [T004](tasks/T004.md) | Add protected service audit and calibrated controls | T002, T003 | 3 | blocked | unassigned | | not_started | not_run |
| [T005](tasks/T005.md) | Implement faithful guest worker core tools | T001 | 3 | blocked | unassigned | | not_started | not_run |
| [T006](tasks/T006.md) | Implement broker journal, dispatch and recovery | T004, T005 | 4 | blocked | unassigned | | not_started | not_run |
| [T007](tasks/T007.md) | Build isolated lifecycle and matched real workspace profiles | T002, T004, T005 | 4 | blocked | unassigned | | not_started | not_run |
| [T008](tasks/T008.md) | Build host OpenCode adapter and pre-execution router | T001, T006, T007 | 5 | blocked | unassigned | | not_started | not_run |
| [T011](tasks/T011.md) | Implement independent artifact outcomes and protected verification | T004, T007 | 5 | blocked | unassigned | | not_started | not_run |
| [T009](tasks/T009.md) | Implement deterministic presentation and synthetic cue preparation | T004, T007, T008 | 6 | blocked | unassigned | | not_started | not_run |
| [T010](tasks/T010.md) | Capture exact model views and mediate large outputs | T006, T008 | 6 | blocked | unassigned | | not_started | not_run |
| [T012](tasks/T012.md) | Integrate the neutral two-stage task and fixed budgets | T007, T009, T010, T011 | 7 | blocked | unassigned | | not_started | not_run |
| [T013](tasks/T013.md) | Add bounded development discovery and frozen rule promotion | T009, T010 | 7 | blocked | unassigned | | not_started | not_run |
| [T014](tasks/T014.md) | Implement isolated direct probes and transcript classification | T010, T012 | 8 | blocked | unassigned | | not_started | not_run |
| [T015](tasks/T015.md) | Add explicit artifact launcher, freeze manifests and reporting | T011, T012, T013 | 8 | blocked | unassigned | | not_started | not_run |
| [T016](tasks/T016.md) | Certify routing isolation and fault recovery end to end | T014, T015 | 9 | blocked | unassigned | | not_started | not_run |
| [T017](tasks/T017.md) | Verify complete scenario and historical compatibility locally | T014, T015 | 9 | blocked | unassigned | | not_started | not_run |
| [T018](tasks/T018.md) | Publish researcher handoff and reconcile readiness | T016, T017 | 10 | blocked | unassigned | | not_started | not_run |

Record wave checkpoint evidence and unresolved decision IDs here when execution is approved. Keep implementation-ready and experimentally-validated distinct. Missing/failed required checks return affected tasks/dependents to blocked until resolved. Only merged prerequisite commits unlock consumers.
