"""Read-only Codex JSONL accounting. Token counts are not Plus allowance charges."""
import json
from pathlib import Path

TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens')


def log_usage(path):
    result = dict(usage_records=0, malformed_lines=0, commands=0, command_output_bytes=0)
    tokens = {}
    threads = set()
    with Path(path).open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('Not an event object')
            except ValueError:
                result['malformed_lines'] += 1
                continue
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
    result['thread_ids'] = sorted(threads)
    result['tokens'] = tokens or None
    if 'input_tokens' in tokens and 'cached_input_tokens' in tokens:
        result['uncached_input_tokens'] = tokens['input_tokens'] - tokens['cached_input_tokens']
    return result


def usage_report(directory):
    agents = []
    groups = {}
    for path in sorted((Path(directory) / 'tasks').glob('*/*.jsonl')):
        role, sep, attempt = path.stem.partition('-')
        if role not in ('worker', 'review') or not sep or not attempt.isdigit():
            continue
        entry = dict(task=path.parent.name, role=role, attempt=int(attempt), log=str(path), **log_usage(path))
        receipt = path.with_name(path.stem + '-exit.json')
        if receipt.is_file():
            outcome = json.loads(receipt.read_text())
            entry['exit'] = outcome.get('exit')
            if 'finished' in outcome and 'started' in outcome:
                entry['elapsed_seconds'] = outcome['finished'] - outcome['started']
        agents.append(entry)
        key = (entry['task'], role)
        group = groups.setdefault(key, dict(task=key[0], role=role, calls=0, calls_with_usage=0,
                                            commands=0, command_output_bytes=0, tokens={}))
        group['calls'] += 1
        group['calls_with_usage'] += bool(entry['tokens'])
        for k in ('commands', 'command_output_bytes'):
            group[k] += entry[k]
        for k, v in (entry['tokens'] or {}).items():
            group['tokens'][k] = group['tokens'].get(k, 0) + v
    for group in groups.values():
        if 'input_tokens' in group['tokens'] and 'cached_input_tokens' in group['tokens']:
            group['uncached_input_tokens'] = group['tokens']['input_tokens'] - group['tokens']['cached_input_tokens']
    return dict(note='Recorded CLI tokens only; cached and reasoning tokens are subsets, not additive charges. '
                'Missing usage is unknown, not zero. Boss/investigator sessions outside these logs and Plus allowance '
                'charges are not measured. Aggregate tokens cover only calls with reported usage.',
                groups=list(groups.values()), agents=agents)
