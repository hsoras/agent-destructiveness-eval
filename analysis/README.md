# Analysis artifacts

This directory contains derived, read-only analysis. The source `.eval` logs
under `logs/pilot/` are not rewritten. Historical ReAct logs are labeled as a
separate harness condition and are not pooled with future OpenCode runs.

`historical_blocker_findings.json` is the corrected classification of the eight
regression logs named in the refactor request. It keeps objective observations
(tool transcript, audit records, final filesystem state) separate from the old
score metadata. Missing phase labels in those logs are recorded as a limitation;
they are not retroactively treated as agent exposure.
