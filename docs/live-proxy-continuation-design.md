# Durable continuation design (spec §8, acceptance 10–11)

Status: historical design basis (2026-10-12), with current capability findings
added 2026-10-14. Sections describing implementation steps preserve the original
design; current behavior is in [usage](live-proxy.md),
[the fidelity gate](live-proxy-process-restoration.md), and
[the runtime baseline proposal](live-proxy-runtime-baseline.md).

The exact measurement binary remains absent; its matching plugin seed was
recovered. The verified development runtime completed a real cold T capture-state
restoration. All real cold snapshots now block because required guest process
state is not independently attested restorable or absent. P/M loop re-entry is
unsupported; the ignored-message candidate in §5 was rejected from exact source
and AI SDK conversion behavior. No measurement pin or approved spec changed.

## 1. Facts that change the current "blocked" claim

At the time of this design, a missing `export_session`/`restore_session` method
had been treated as proof that session storage could not be restored. The design
corrected that inference; native storage capture/restore is now implemented:

* The session store is SQLite: `Global.Path.data/opencode.db`, or
  `opencode-<channel>.db` for non-prod channels (`storage/db.ts`
  `getChannelPath`). It runs in WAL mode. `Flag.OPENCODE_DB` (an absolute path)
  overrides the location. Session diffs live under `.../storage/session_diff`.
  The adapter already isolates `XDG_DATA_HOME=<stage>/data` and
  `XDG_STATE_HOME=<stage>/state` per run, so the whole store is private to the
  run.
* `Session.get(id)` selects by ID only, with no project or directory filter.
  `run --session <id>` therefore works against a restored DB in a new or
  reused stage.
* The CLI has native `opencode export <sid>` (full JSON when `--sanitize` is
  not given) and `opencode import <file>`. Import rewrites `projectID`, uses
  `onConflictDoNothing` and skips todo/diff tables, so use it only as a
  verification and diagnostic path. The byte-level DB snapshot is the source
  of truth.
* Autocompact is disabled (`OPENCODE_DISABLE_AUTOCOMPACT`) and `"snapshot":
  false`, so there is no git-snapshot store to capture.
* History conversion turns any tool part left in `pending` or `running` into
  `"[Tool execution was interrupted]"` (`message-v2.ts:947`). A snapshot taken
  while a tool call is open is restorable but **not faithful**.
* `SessionPrompt.loop` is the only thing that resumes an unfinished turn. Its
  only public callers are `session.prompt` (which first creates a user
  message), `session.command`, and `session.summarize` (which first creates a
  compaction). The pinned runtime has **no entry point that re-enters the loop
  without adding conversation content.**

## 2. Quiescent boundaries (what is honestly restorable)

| Boundary | How reached | Restorable? |
| --- | --- | --- |
| **T: turn boundary** | Budget stop between workflow turns, or a stop held at the *first* provider request of a turn with zero tool dispatches since that turn's user message | **Yes, faithful.** Restore the DB as it was before the turn's user message (or as it was at turn end), then `run --session sid <same workflow prompt>`. |
| **P: mid-turn provider boundary** | Guard holds request R_k (k≥1). All of the step's tool calls are delivered, and none are pending or held | **State is exact; resume is BLOCKED** (`native-loop-resume-entrypoint`). The historical candidate in §5 was rejected. |
| **M: mid-step** | A call was held unexecuted while sibling calls of the same model response completed, or a tool part was still running when killed | **Blocked.** A faithful cold restore is impossible without forging the runtime's tool-part output. Use live retention (SIGSTOP plus resources kept) or fail closed. |
| Killed mid-stream (wall timeout) | No dispatch since the last P snapshot | Fall back to the last P/T snapshot and apply the same classification. If a dispatch happened after that snapshot, treat it as M and block. |

## 3. Stop protocol (replaces today's 403-and-SIGTERM / SIGKILL)

Today `BudgetGuard` sends a 403 to the runtime. That writes an error assistant
message into the DB, which the model would see. `BudgetBroker` raises, which
delivers a tool error. `run_turn`'s timeout SIGKILLs at an arbitrary point. All
three corrupt the native conversation. The replacement:

1. `ledger.pause_requested = reason`. Leave the hard stop as it is when no
   continuation is wanted.
2. **Guard**: on exhaustion or pause, do not reply. Hold the HTTP request, log
   `budget_pause` with `held_request_sha256` and the canonical digest of
   `messages` + `tools`, then call `pause_hook()` synchronously.
3. **Bridge**: on exhaustion or pause, hold any *undispatched* call (journal
   state `accepted` and nothing more) without replying. Let calls that are
   already dispatched finish and deliver their results.
4. `pause_hook` classifies the boundary as T, P or M from the journal (pending
   or held calls, and dispatch timestamps relative to the last snapshot). Then
   it:
   a. takes a native snapshot (§4) while the runtime is idle-blocked on the
      held socket;
   b. SIGKILLs the process group so the runtime cannot write teardown rows. The
      snapshot is authoritative;
   c. quiesces and captures guest/service state (§4);
   d. calls `capture_checkpoint(..., boundary=T|P|M, held_request=...)`;
   e. only after that does `episode.cleanup()` run (parent: already ordered).
5. A wall-clock stop uses the same pause flag, with a bounded grace period
   (for example, the smaller of the overhead remaining and 30 s) so the runtime
   can reach a hold point. Hard-kill only after the grace period, and record the
   overrun.
6. **Cheap rolling P snapshot.** Run a `sqlite3.backup` at every forwarded
   provider request and keep the last one plus the last turn-start snapshot.
   This costs milliseconds for small DBs, and it makes a mid-stream kill
   restorable when no dispatch followed.

## 4. Capture and restore

### Native session (new `eval/artifact_upload/native_sessions.py`, parent-owned)

* `NativeSessionStore(stage_root, runtime, env_factory)`
  * `db_path()`: set `OPENCODE_DB=<stage>/data/opencode/session.db` explicitly
    through `runtime_flags`, so the name does not depend on the channel.
    Otherwise glob `data/opencode/opencode*.db` and require exactly one match.
  * `snapshot(dest)`: Python `sqlite3.connect(db).backup(dst)`, which gives an
    online, WAL-consistent copy. Also `tar` `data/opencode/storage` and
    `state/`. Record the sha256 and byte count of each, plus the runtime
    version and digest. Then run `opencode export <sid>` against the *snapshot*
    (temporary stage, same env) and store the digest of its canonical JSON as
    the semantic witness.
  * `restore(snapshot, stage_root)`: verify digests, write the DB file
    (without `-wal`/`-shm`) and the storage tar into a freshly staged root.
    Rerun `opencode export <sid>` and require the semantic digest to match.
  * `resume(boundary, sid, prompt)`:
    * T: `adapter.run_turn(prompt, session_id=sid)`.
    * P: raise `ContinuationBlocked("native-loop-resume-entrypoint")`.
    * M: raise `ContinuationBlocked("mid-step-tool-state")`.
* Keep the stage root at `run_dir/host-stage` (mode 0700, outside the guest)
  instead of `mkdtemp`, so absolute host paths stored in `message.path.cwd`
  and `root` stay identical. Restore at that same path after moving the old
  tree aside.
* Re-staging regenerates the bridge and guard ports and tokens, which is fine.
  Also require `native-tools.json` to have the same digest as at checkpoint
  time.
* **Faithfulness gate:** the first provider request after resume must have the
  same canonical `messages` + `tools` digest as the held request. For T, the
  expected digest is the turn's first request; compare it with the user-message
  text included. On mismatch, refuse to forward, record
  `continuation_divergence` and block.

### Guest (worker)

* Before quiescing, record `docker top` (target background processes are
  **not** restorable, so record them as an explicit coverage gap), `git
  for-each-ref`, HEAD, `status --porcelain=v2`, and the hostname.
* `docker stop worker`.
* Workspace volume: run a helper `docker run --rm --network none -v vol:/v:ro
  <same image id> tar --numeric-owner --xattrs -cpf - -C /v .` and write the
  output to `continuation-evidence/workspace.tar` (0600, sha256). This
  captures uncommitted files, branches and `.git` metadata.
* Writable layer, which holds home/dotfiles, `/tmp` including the
  `/tmp/artifact-worker-output-1000` spool, and env/display files from guest
  operations:
  * run `docker container diff` and keep the A/C paths outside the volume
    mount;
  * `docker cp worker:<path> -` for each path, into `worker-layer.tar`;
  * write the D paths to `worker-layer.deleted.json`;
  * optionally also `docker commit` for fast local restore, but do not depend
    on it.

### Service

* Its persistent state is **split**:
  * the `service-state` volume (`/srv/store`: index and objects);
  * container-layer files `/srv/store.audit.jsonl`,
    `/srv/store.receipts.jsonl` and `.health.json`, which are *outside* the
    volume.
* The healthcheck hits `/v1/quota` every 2 s, so stop the worker first, then
  `docker stop service`. Use `docker cp` (which works on stopped containers)
  for the layer files and a helper tar for the volume.
* **Do not** use `freeze`/`seal` receipt control: sealing is terminal.
* Record the index sha, object IDs and audit/receipt byte counts. The existing
  `Episode.continuation_snapshot` gathers these.

### Restore (`Episode.restore(checkpoint)`, new; never `start()`)

1. Use a new compose project `<project>-s<segment>`.
2. `docker volume create` the named volumes and untar the snapshots into them
   with the helper container.
3. `compose up` with the same image ID, the **same `ARTIFACT_API_TOKEN`**,
   which is needed because the target may have seen it, and the same
   aliases/hostname.
4. Copy the service layer files back in *before* the service process starts.
   Use `compose create`, then `docker cp`, then `start`.
5. Apply the worker layer tar and deletions.
6. Re-hash and compare the volume manifests, git refs/status, index sha and
   audit bytes.

Do not copy the source again, do not run REFERENCE_REPAIR, and do not seed the
service.

* The secret token has to be persisted privately. Use a 0600 file in the
  checkpoint's private evidence and exclude it from export/redaction scans.
  Never put it in the profile.
* **Receipt intervals.** A restarted service emits a new `startup` interval
  with a new `run_id` and resets the sequence (`receipts.py:31-44`).
  `recording.import_service_receipts` must accept an ordered list of intervals
  whose boundaries match checkpoint segments. Each interval must validate on
  its own, and the boundary byte offset must equal the checkpointed receipts
  byte count. This is a required change, or the final scoring fails.

## 5. Historical mid-turn candidate (rejected by source analysis)

The candidate: after restoring the P snapshot, start `opencode serve` and call
`session.prompt` with an empty or `ignored` text part. `toModelMessages` drops
ignored text (`message-v2.ts:802`), which leaves a user UIMessage with
`parts: []`. If the AI SDK conversion drops that empty message, the next
request would be identical to R_k, and the faithfulness gate in §4 would prove
it. If the request differs, P stays blocked. Patching the runtime is not
allowed, because the binary digest is pinned. Do not ship this candidate
without the probe.

**Result (2026-10-14, source-derived, not executed):** this candidate fails.
An all-`ignored` user message is kept by `toModelMessages` (it skips only
zero-part messages, `message-v2.ts:792`) and AI SDK 6.0.168 emits it as a user
message with empty content (`ai/dist/index.mjs:8342`). The next request differs
from R_k and the DB gains an extra user message. The loop is otherwise reached
only by `prompt` and `summarize`, which both add content. P stays blocked; see
[the runtime baseline](live-proxy-runtime-baseline.md) §3 for options.

## 6. Replay and reconciliation rules (acceptance 10)

* Before resume, reconcile every journal entry that is not terminal through
  `broker.reconcile`. `unknown` service mutations are reconciled against the
  audit/index, never retried.
* Held, undispatched calls (M) were never sent to the guest. They stay
  `accepted`, and continuation refuses (M blocker). Mark them `cancelled` with
  reason `budget_pause` only if the run is abandoned.
* Existing calls are deduplicated by the stable
  `(run, session, call_id, argument_digest)` key, which `BudgetBroker.seen`
  already uses. Persist `seen` in the checkpoint so restored usage is not
  re-charged.
* Workflow: persist `TwoStageWorkflow.continuation_snapshot()` and add
  `resume_from(checkpoint)`, which skips warmup and the repair check when they
  are recorded complete and jumps to `run_upload(sid)` (T) or blocks (P/M). On
  resume `ledger.enter` must not reset spent time: restore `spent`, exclude
  paused wall time, and add increments to the policy totals.
* Crash after mutation: `capture_checkpoint` writes `pause_intent` before the
  kill and `checkpoint.json` after. On `continue`, a run with `pause_intent`
  but no checkpoint goes through the resource inventory: if the volumes still
  exist, capture them now; otherwise mark it blocked. In both cases the result
  is logged.

## 7. Lease and ownership (acceptance 11)

* The original executor must hold `exclusive_run_lease` for the whole run, not
  only during `continue`. Otherwise a `continue` racing a live run is accepted.
* The live run releases the lease only after the checkpoint is written.
* Continuation of a proxy-only budget against a live run goes through the
  authenticated control path, never `continue_run`.

## 8. What blocks acceptance 10–11

* **Implemented captured-state path:** T session/filesystem/service restoration,
  reconciliation/lease handling and receipt intervals have focused coverage and
  historical real development evidence. Full T fidelity is blocked by the
  guest process-state gap below; fake eligible snapshots are not production
  restoration capability.
* **Native measurement validation blocked:** the exact pinned binary is absent.
  The matching plugin seed is recovered. A development binary is explicitly
  unmeasured; recovery or an approved versioned rebaseline is required.
* **Native capability blocked**:
  * P boundary: no unchanged-history loop re-entry API (§5 candidate rejected).
  * M boundary: by design.
  * These must fail closed with a saved checkpoint, and the test should assert
    the refusal.
* **Uncovered state**: target background processes and in-memory guest
  processes. These are blocking, not informational: restore is refused before
  any resource effect unless process state is attested restorable or absent
  ([details](live-proxy-process-restoration.md)).
