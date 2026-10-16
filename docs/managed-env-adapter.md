# Managed environment adapter

The deployed worker core remains frozen at the bytes preceding commit
`5bfb3c3` (`sha256: 5c42cf7b1718fdcba0c8bca631486e972154e57ae71197ed1932bb873b36aea8`).
Managed profile env files are now handled by the host `GuestWorkerSession`
adapter. It resolves the profile from the pinned host world manifest and enables
the adapter only when the selected natural-looking profile matches. Eval-looking
calls and non-shell tools are passed through unchanged.

For natural-looking shell calls, the adapter reads the two fixed profile files
with a bounded, no-follow, regular-file reader using directory file descriptors.
It parses only the existing literal `export NAME='value'` and `unset NAME`
forms and applies the established protected-name deny list. It never sources or
executes these agent-writable files. Set values are merged into the shell
call's `arguments.env`, with explicit per-call values taking precedence. Unsets
use a fixed `env -u ... bash -c` wrapper that quotes the original command as one
argument; its exit status and process group behavior remain governed by the
existing worker. `BASH_ENV` is forced to `/dev/null` for every natural-looking
shell call, even when the managed files are absent or unreadable. The working directory is unchanged. Files are re-read for each
shell call, so a completed `env_set_variables` operation affects subsequent
calls in the same session.

The adapter changes execution-stack behavior without changing the worker
identity. Existing receipts that pin only `worker_digest` therefore do not
establish equivalence for this host-side adapter revision or its semantics.
Receipts are intentionally not repinned here.
