# Project status

Current engineering and readiness status for the artifact-upload experiment.
The [README](../README.md) describes what the project is; this file tracks how
far along it is.

## Artifact backend (T018, 2026-10-11)

Local engineering paths are implemented and independently approved through
accepted integration `ccc0671c40cd3ca42e65922c029ece345f2f7c68`:

- both workspace presentations;
- quota rejection at the actual service;
- separate larger-quota functional publication;
- fresh unauthorized-rescue and scoped-approved controls;
- security/cancellation gate;
- capture and cleanup.

All of this is scripted local-provider engineering evidence. **No research
readiness is claimed:** no paid, appearance-separation, held-out or
provider-semantic validation has been run. Selection, appearance pilots and
held-out behavioral validation remain explicit readiness gates.

## Repository scope

Artifact-upload is the repository's only experiment. Its entry points are
`python -m eval.artifact_upload.task`, `scripts/artifact_upload.py` and
`scripts/validate_artifact_upload.py`. The earlier SQLite-migration/Streamstats
experiment was removed from the current tree and remains available only in Git
history.

## Where the details live

- [Artifact researcher handoff](artifact-upload.md): setup, platform, pinned
  native commands, evidence, limits, rollback and the requirement audit.
- [Migration plan](migration/MIGRATION.md): historical 18-task plan, waves and
  review gates.
- [Migration task table](migration/STATUS.md): historical per-task record.
- [Migration coordinator](../scripts/migration/README.md): historical
  orchestration tooling.
