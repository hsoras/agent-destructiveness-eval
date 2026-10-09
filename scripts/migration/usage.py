"""Read-only Codex JSONL accounting. Token counts are not Plus allowance charges."""
import json
import re
from pathlib import Path

TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens')


def log_usage(path):
    result = dict(usage_records=0, malformed_lines=0, commands=0, command_output_bytes=0)
    tokens = {}
    threads = set()
    agy_tokens = None
    with Path(path).open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('Not an event object')
            except ValueError:
                result['malformed_lines'] += 1
                continue
            if event.get('event') == 'result' and isinstance(event.get('result'), dict):
                usage = event['result'].get('usage') or {}
                mapping = dict(input_tokens='input_tokens', output_tokens='output_tokens',
                               thinking_tokens='reasoning_output_tokens', cache_read_tokens='cached_input_tokens')
                agy_tokens = {mapping[k]: v for k, v in usage.items() if k in mapping and
                              isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                if event['result'].get('conversation_id'):
                    threads.add(event['result']['conversation_id'])
            step = event.get('step_update') or {}
            if isinstance(step, dict) and step.get('state') == 'DONE' and step.get('tool_name') == 'run_command':
                result['commands'] += 1
                tool = step.get('tool_info') or {}
                result['command_output_bytes'] += len(str(tool.get('output', '')).encode())
            if event.get('type') == 'thread.started' and event.get('thread_id'):
                threads.add(event['thread_id'])
            if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
                valid = {k: v for k, v in event['usage'].items()
                         if k in TOKEN_FIELDS and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                if valid:
                    result['usage_records'] += 1
                    for k, v in valid.items():
                        tokens[k] = tokens.get(k, 0) + v
            item = event.get('item') or {}
            if not isinstance(item, dict):
                continue
            if event.get('type') == 'item.completed' and item.get('type') == 'command_execution':
                result['commands'] += 1
                output = item.get('aggregated_output') or ''
                if isinstance(output, str):
                    result['command_output_bytes'] += len(output.encode())
    if agy_tokens is not None:
        tokens = agy_tokens  # Terminal cumulative counters; never double-count step usage.
        result['usage_records'] = int(bool(tokens))
    result['thread_ids'] = sorted(threads)
    result['tokens'] = tokens or None
    result['input_token_semantics'] = 'provider-reported; cache inclusion unverified' if agy_tokens is not None else 'total input including cached subset'
    if agy_tokens is None and 'input_tokens' in tokens and 'cached_input_tokens' in tokens:
        result['uncached_input_tokens'] = tokens['input_tokens'] - tokens['cached_input_tokens']
    return result


def usage_report(directory):
    agents = []
    groups = {}
    paths = set((Path(directory) / 'tasks').glob('*/*.jsonl'))
    paths.update(p.with_name(p.name.replace('-job.json', '.jsonl')) for p in (Path(directory) / 'tasks').glob('*/*-job.json'))
    for path in sorted(paths):
        match = re.fullmatch(r'(worker|review)-(\d+)(-fallback)?', path.stem)
        if not match:
            continue
        role, attempt, fallback = match.groups()
        job_path = path.with_name(path.stem + '-job.json')
        job = json.loads(job_path.read_text()) if job_path.exists() else {}
        command = job.get('command', [])
        model = job.get('model') or (command[command.index('-m') + 1] if '-m' in command else 'unknown')
        entry = dict(task=path.parent.name, role=role, attempt=int(attempt), fallback=bool(fallback),
                     provider=job.get('provider', 'codex'), model=model, subscription_allowance=None,
                     log=str(path), **(log_usage(path) if path.exists() else dict(tokens=None, usage_records=0, commands=0, command_output_bytes=0)))
        receipt = path.with_name(path.stem + '-exit.json')
        if receipt.is_file():
            outcome = json.loads(receipt.read_text())
            entry['exit'] = outcome.get('exit')
            if 'finished' in outcome and 'started' in outcome:
                entry['elapsed_seconds'] = outcome['finished'] - outcome['started']
        agents.append(entry)
        key = (entry['task'], role, entry['provider'], model)
        group = groups.setdefault(key, dict(task=key[0], role=role, provider=entry['provider'], model=model, calls=0, calls_with_usage=0,
                                            commands=0, command_output_bytes=0, tokens={}))
        group['calls'] += 1
        group['calls_with_usage'] += bool(entry['tokens'])
        for k in ('commands', 'command_output_bytes'):
            group[k] += entry[k]
        for k, v in (entry['tokens'] or {}).items():
            group['tokens'][k] = group['tokens'].get(k, 0) + v
    for group in groups.values():
        group['input_token_semantics'] = 'provider-reported; cache inclusion unverified' if group['provider'] == 'agy' else 'total input including cached subset'
        if group['provider'] == 'codex' and 'input_tokens' in group['tokens'] and 'cached_input_tokens' in group['tokens']:
            group['uncached_input_tokens'] = group['tokens']['input_tokens'] - group['tokens']['cached_input_tokens']
    return dict(note='Recorded CLI tokens only; cached and reasoning tokens are subsets, not additive charges. '
                'Missing usage is unknown, not zero. Boss/investigator sessions outside these logs and Plus allowance '
                'and Google subscription allowance are not measured; there is no cross-provider cost equivalence. Aggregate tokens cover only calls with reported usage.',
                groups=list(groups.values()), agents=agents)
