# Parent integration review

Read-only review of the candidate working tree, 2026-10-12. Only this report was
written. Runtime implementation and native prerequisites remain owned by their
assigned workers; their temporary absence is not a finding. Candidate files were
changing during review; locations below reflect the final inspected contents.

## Blocking findings

### P1: Legacy v2 rules cannot be imported across matched conditions

Location: `scripts/artifact_upload.py:146-147`.

`RuleSet.load(path, selected)` uses `selected` only for v1 conversion. For v2,
it retains every condition-specific rule. `ProfileBundle.from_dict` then rejects
rules belonging to another condition. Consequently an existing valid v2 rules
file exported from a natural-looking run prevents the default `--condition both`
LLM launch, including the eval-looking episode where those rules should simply
not apply. Explicit profile output with legacy rules has the same failure.
The legacy presenter filters rules by condition at rendering time.

Reproduced without native resources:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from eval.artifact_upload.rules import default_ruleset
from scripts.artifact_upload import configure_profiles

with TemporaryDirectory() as d:
    rules = Path(d) / 'rules.json'
    rules.write_text(default_ruleset('natural-looking').dumps())
    frozen = {'proxy': {'mode': 'llm', 'rules_path': str(rules)},
              'frozen_files': {}}
    configure_profiles(frozen, condition='both')
```

Actual: `ProfileError: profile ruleset contains a rule for another condition`.

Required fix: import only rules whose condition is `any` or the selected
condition into each bundle, preserving the applicable legacy rendering and
recording import provenance. Add a regression using a v2 input with
condition-specific rules and both conditions.

### P1: Teardown failures skip result persistence and explicit lease release

Location: `eval/artifact_upload/task.py:594-600` (also unguarded adapter close
at line 566).

The lease exit and `result.json` write occur after teardown calls that can
raise. Real `Episode.cleanup` raises `InfrastructureError` on a failed compose
down. Such a failure skips both operations. With the exception/traceback retained,
the lease remains locked and a subsequent owner is refused. Earlier close
failures additionally skip the remaining cleanup and checkpoint attempt.

Reproduced by replacing only `task.Episode` with a stub whose `start()` raises
`RuntimeError('setup failed')` and whose `cleanup()` raises
`RuntimeError('cleanup failed')`, then invoking ordinary frozen `run_episode`
inside a temporary directory and retaining the caught exception. Results:

```text
result persisted: False
lease reacquisition: ContinuationBlocked another continuation owner holds the run lease
```

Required fix: make lease release an outer unconditional `finally` or context
manager; independently attempt remaining teardown and persist the partial result
with cleanup errors even when a close/cleanup step raises. Preserve the original
failure for the caller. Test failed cleanup and failed close with a retained
traceback and verify result persistence and immediate lease reacquisition.

## Validation and limits

- Profile CLI tests: 5 passed.
- Profile system-message transport and proxy control tests: 12 passed after
  enabling loopback socket access. Initial sandbox run failed only at socket
  binding (`PermissionError: [Errno 1] Operation not permitted`).
- Both blocking findings reproduced through focused temporary-directory probes;
  no providers, Docker resources, or native sessions were started.
- Condition mappings, cross-condition path rejection, immutable launch bundles,
  and reconstruction of fresh input for each assignment were inspected.
- Rule-export receipts were added concurrently and a stubbed integration probe
  observed both `rules.json` and `rules.json.receipt.json`; no finding retained.
- System/user message separation passed the transport test. Binding the exact
  prompts in the in-progress runtime remains a runtime acceptance check.
- Full checkpoint restoration, proxy factory integration, guest transactions,
  and saved runtime findings require the workers' completed implementations;
  no completion claim is made for those paths.

## Optional ideas

None. This review reports required behavior failures only.
