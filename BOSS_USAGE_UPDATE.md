# Consolidated boss usage update

Paste the following into the running boss:

```text
Apply this consolidated usage policy now. It supersedes the earlier one-active-Sol/sequential-review advice. Read only section 6 of the updated root BOSS_PROMPT.md once, adopt it, and save a compact reference in memory.

- Optimize total tokens, not usage per minute. Keep independent coding/reviews/investigations parallel when dependencies and ownership permit; max two isolated coding workers. Serialize only shared integration mutations.
- Preserve active work. Read instructions once; use compact current status instead of rereading full prompts, plans, old threads or logs. Older docs/migration/BOSS.md is historical guidance, not the current workflow.
- Give children exact absolute paths and relevant rules directly in their briefs; do not make every child read or search for the full boss prompt. Use one recovery scout only when substantial missing state needs summarizing; do not duplicate its reads.
- Keep repair briefs around 3–5K characters: consolidated findings, acceptance criteria, candidate SHA, evidence paths and validation commands. Give fresh coders summaries and the distilled OpenCode semantics reference instead of raw source trees; allow targeted source inspection when needed.
- Use rg/grep and targeted sed ranges. Aim for at most about 6K characters per tool result where practical. Save full test logs, return concise summaries and preserve test exit codes.
- Review exact SHA/scope, prior findings and focused adversarial cases first. Once a reproducible blocker establishes rejection, consolidate related findings and return. Explicitly defer unrelated broad checks; every mandatory gate remains required before approval/integration.
- Around 15 tool calls or 60K context, reassess whether further work can change the immediate decision. Do not automatically stop, restart or spawn another agent. Around 120K coder context, consider a fresh consolidated handoff if continued repair would be wasteful; never overlap writers or lose unfinished work.
- Reuse valid investigations/evidence. Do not repeat passing checks without changed code, new findings or a required gate. Do not retry failed repairs without a new hypothesis; report repeated blockers rather than cycling blindly.
- Edit bookkeeping directly. Handle each completion once; ignore later notifications for an already processed hand-back. Report meaningful state changes/blockers in at most five lines; otherwise update status silently. Answer explicit user questions and request decisions when authority requires them.
- Boss: Sonnet 5.5, fallback GPT-6.1 Sol Low. Escalations: Opus 5.5, fallback GPT-6.1 Sol Medium. Keep independent reviews and all quality gates; use Standard service tier for OpenAI tasks.

Acknowledge in one line and continue without restarting active agents.
```
