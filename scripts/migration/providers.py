"""Subscription CLI adapters. Never use model APIs or change account settings."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess


def validate_routing(policy):
    for role in ('primary', 'fallback', 'reviewer'):
        route = policy[role]
        if route['backend'] not in ('agy', 'codex') or not route['model']:
            raise ValueError('Unknown backend or empty model')
        if route.get('effort', 'medium') not in ('low', 'medium', 'high'):
            raise ValueError('Unknown effort')
    if policy['fallback']['backend'] != 'codex' or policy['reviewer']['backend'] != 'codex':
        raise ValueError('Fallback and independent reviewer require Codex')
    if not 0 < policy.get('preflight_seconds', 30) <= 120:
        raise ValueError('Preflight timeout must be bounded at 120 seconds')
    return policy


def classify_error(text):
    """Only provider diagnostics/tool error objects, never arbitrary agent prose."""
    rules = [
        ('authentication', r'authentication required|unauthenticated|invalid credentials|login required|authentication fail|oauth.*(?:expired|invalid)|\b401\b'),
        ('quota', r'quota.*(?:exhaust|exceed)|resource_exhausted|rate limit|\b429\b'),
        ('model_unavailable', r'model.*(?:not recognized|not found|unavailable)|invalid model selection'),
        ('permissions', r'user denied permission|permission check failed|auto-denied|denied_actions|requires? .*permission'),
        ('initialization', r'failed to start|failed to initialize|bind: operation not permitted'),
        ('provider', r'\b(?:500|502|503|504)\b|service unavailable|provider.*error|connection (?:refused|reset)|network is unreachable'),
    ]
    for reason, pattern in rules:
        if re.search(pattern, text, re.I):
            return reason
    return None


def preflight(route, cwd, timeout=30):
    executable = shutil.which(route['backend'])
    if executable is None:
        return dict(ready=False, reason='executable_missing', executable=route['backend'])
    if route['backend'] == 'codex':
        return dict(ready=True, executable=executable, authentication='verified by execution only')
    try:
        p = subprocess.run([executable, 'models'], cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return dict(ready=False, reason='initialization', error=str(e))
    result = dict(executable=executable, command=[executable, 'models'], exit=p.returncode,
                  stdout=p.stdout, stderr=p.stderr)
    slugs = {line.split()[0] for line in p.stdout.splitlines() if line.split()}
    reason = classify_error(p.stderr)
    if p.returncode or reason:
        result.update(ready=False, reason=reason or 'initialization')
    elif route['model'] not in slugs:
        result.update(ready=False, reason='model_unavailable')
    else:
        result.update(ready=True, model=route['model'], authentication='verified by execution only')
    return result


def effective_permissions(cwd, timeout=30, project=None):
    """Ask the CLI for project/shared/global rules without starting an agent turn."""
    command = ['agy', '-p', '/permissions', '--output-format', 'json']
    try:
        p = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        payload = json.loads(p.stdout)
        if p.returncode or payload.get('status') != 'SUCCESS':
            raise ValueError('Permission inspection did not succeed')
        scopes = payload['command']['data']['permissions']
        if not isinstance(scopes, list) or not scopes:
            raise ValueError('Missing effective permission scopes')
        permissions = dict(allow=[], ask=[], deny=[])
        seen = set()
        for scope in scopes:
            name = scope['scope']
            if name not in ('project', 'shared', 'global') or name in seen:
                raise ValueError('Unknown or duplicate permission scope')
            seen.add(name)
            for access in permissions:
                rules = scope.get(access, [])
                if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
                    raise ValueError('Invalid effective permission rules')
                permissions[access].extend(rules)
        if seen != {'project', 'shared', 'global'}:
            raise ValueError('Incomplete effective permission scopes')
        selected = None
        if project is not None:
            from worker_environment import selected_project_policy
            selected = selected_project_policy(project, cwd)
            # Keep restrictions reported at any scope; augment only with the exact
            # selected project that --project applies at conversation startup.
            for access in permissions:
                permissions[access].extend(selected.get(access, []))
            if any(rule.startswith('unsandboxed(') for rule in permissions['allow']):
                raise ValueError('Scoped worker cannot inherit any unsandboxed Allow grant')
        return dict(ready=True, permissions=permissions, scopes=scopes, command=command,
                    selected_project=project, selected_project_policy=selected)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as e:
        return dict(ready=False, reason='permissions', error=str(e), command=command)


def permission_preflight(commands, settings=None, *, permissions=None):
    """Conservative literal-token check; uncertain rules fail closed, never bypass.

    File input is for offline inspection/tests only. Execution uses the CLI's effective
    rules, because inspecting a different JSON file cannot configure the CLI.
    """
    path = Path(settings) if settings else Path.home() / '.gemini/antigravity-cli/settings.json'
    try:
        if permissions is None:
            permissions = json.loads(path.read_text()).get('permissions', {})
        if not isinstance(permissions, dict):
            raise ValueError('Permissions must be an object')
        for access in ('allow', 'ask', 'deny'):
            rules = permissions.get(access, [])
            if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
                raise ValueError('Permission rules must be string arrays')
            for rule in rules:
                if not re.fullmatch(r'[a-z_]+\(.+\)', rule):
                    raise ValueError('Malformed permission rule')
        def matches(rule, argv):
            if rule.startswith('unsandboxed('):
                # Installed CLI 1.3.2 also applies unsandboxed Deny/Ask rules
                # to ordinary command checks. Never certify a conflicting rule.
                target = rule[len('unsandboxed('):-1]
            elif rule.startswith('command('):
                target = rule[8:-1]
            else:
                return False
            if target == '*':
                return True
            if target.startswith('regex:'):
                return False  # CLI token-regex semantics are deliberately not emulated.
            # Literal permission targets split by whitespace; quotes are not shell-unquoted.
            words = target.split()
            return bool(words) and argv[:len(words)] == words
        if any(rule in permissions.get('allow', []) for rule in
               ('command(*)', 'unsandboxed(*)', 'write_file(*)', 'read_file(*)')):
            raise ValueError('Unrestricted grants are incompatible with scoped workers')
        missing = []
        for argv in commands:
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
                raise ValueError('Commands must be nonempty argv string arrays')
            # These constructs can disable CLI prefix matching. Never infer a grant from a prefix.
            uncertain = any(re.search(r'[\n\r`$<>|;&{}]', arg) for arg in argv)
            uncertain = uncertain or (Path(argv[0]).name == 'git' and any(
                arg == '-c' or arg.startswith('--config-env') for arg in argv[1:]))
            restrictions = permissions.get('deny', []) + permissions.get('ask', [])
            if (uncertain or any(rule.startswith(('command(regex:', 'unsandboxed(regex:')) or matches(rule, argv) for rule in restrictions) or
                    not any(rule.startswith('command(') and matches(rule, argv)
                            for rule in permissions.get('allow', []))):
                missing.append(argv)
        return dict(ready=not missing, reason='permissions' if missing else None,
                    required_commands=commands, unavailable_commands=missing)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        return dict(ready=False, reason='permissions', error=str(e))


def snapshot(cwd):
    """Compare all tracked/untracked bytes, symlink targets, HEAD, status and index."""
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=cwd)
    names = set(git('ls-files', '-z').split(b'\0') +
                git('ls-files', '--others', '-z').split(b'\0')) - {b''}
    files = {}
    for raw in sorted(names):
        name = os.fsdecode(raw)
        p = Path(cwd) / name
        if p.is_symlink():
            value = 'symlink:' + os.readlink(p)
        elif p.is_file():
            value = hashlib.sha256(p.read_bytes()).hexdigest() + ':' + oct(p.stat().st_mode & 0o777)
        else:
            value = 'missing'
        files[name] = value
    return dict(head=git('rev-parse', 'HEAD').decode().strip(),
                branch=git('branch', '--show-current').decode().strip(),
                status=git('status', '--porcelain=v1', '-z', '--untracked-files=all').decode(),
                index=hashlib.sha256(git('diff', '--cached', '--binary')).hexdigest(), files=files)


def agy_outcome(log, stderr, exit_code):
    terminal = None
    errors = []
    tool_errors = []
    tool_diagnostics = []
    for line in Path(log).read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('event') == 'result':
            terminal = event.get('result')
        step = event.get('step_update') or {}
        if isinstance(step, dict) and isinstance(step.get('tool_info'), dict):
            output = str(step['tool_info'].get('output', ''))
            if re.search(r'permission denied|operation not permitted|fatal: not a git repository', output, re.I):
                tool_diagnostics.append(dict(tool=step.get('tool_name'), message=output[-2000:]))
            error = step['tool_info'].get('error')
            if error:
                tool_errors.append(error)
    if not isinstance(terminal, dict):
        terminal = None
    diagnostics = Path(stderr).read_text() if Path(stderr).exists() else ''
    reason = classify_error(diagnostics)
    for error in tool_errors:
        # Application tool output can contain quota/auth/provider strings. Only
        # the CLI's permission engine diagnostic proves an infrastructure failure.
        message = str(error.get('message', '')) if isinstance(error, dict) else ''
        if 'permission check failed' in message.lower() or 'user denied permission' in message.lower():
            reason = reason or 'permissions'
    if isinstance(terminal, dict):
        if terminal.get('denied_actions'):
            reason = 'permissions'
        if terminal.get('error'):
            errors.append(terminal['error'])
            reason = reason or classify_error(str(terminal['error']))
        success = terminal.get('status') == 'SUCCESS' and exit_code == 0 and not reason and not tool_errors
        response = terminal.get('response') or ''
    else:
        success, response = False, ''
    # Unknown tool errors and incomplete implementation never become provider outages.
    return dict(success=success, provider_failure=reason, terminal_status=(terminal or {}).get('status'),
                denied_actions=(terminal or {}).get('denied_actions', []), errors=errors,
                tool_errors=tool_errors, sandbox_tool_diagnostics=tool_diagnostics, response=response,
                structured_output=(terminal or {}).get('structured_output'))


def auth_smoke(route, directory=None):
    """Harmless cached-account headless check; artifacts and disposable repo retained."""
    import sys
    import tempfile
    from orchestrate import atomic, git, HERE
    root = Path(directory).resolve() if directory else Path(tempfile.mkdtemp(prefix='gemini-readiness-'))
    if directory:
        root.mkdir(parents=True, exist_ok=False)
    repo = root / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'readiness')
    git(repo, 'config', 'user.name', 'Readiness Smoke')
    git(repo, 'config', 'user.email', 'readiness@example.invalid')
    git(repo, 'commit', '--allow-empty', '-m', 'harmless readiness baseline')
    prompt = root / 'prompt.txt'
    prompt.write_text('Harmless authentication check. Reply exactly READY. Do not use tools, edit files, run commands, access networks or delegate tasks.')
    job = dict(command=['agy', '--model', route['model'], '--effort', route.get('effort', 'medium'),
                        '--output-format', 'stream-json', '--sandbox', '--mode', 'plan', '--disable-slash-commands', '--print-timeout', '60s'],
               provider='agy', model=route['model'], route=route, role='readiness', task='readiness', attempt=1,
               cwd=str(repo), prompt=str(prompt), log=str(root/'auth.jsonl'), stderr=str(root/'auth.stderr'),
               receipt=str(root/'exit.json'), result=str(root/'result.txt'), timeout=60, permission_commands=[])
    before = snapshot(repo)
    atomic(root/'job.json', job)
    subprocess.run([sys.executable, str(HERE/'job.py'), str(root/'job.json')], check=True)
    outcome = json.loads((root/'exit.json').read_text())
    ready = bool(outcome.get('success') and (root/'result.txt').read_text().strip() == 'READY' and snapshot(repo) == before)
    return dict(ready=ready, authentication='cached account headless execution verified' if ready else 'unverified',
                model=route['model'], artifacts=str(root), receipt=str(root/'exit.json'),
                provider_failure=outcome.get('provider_failure'), usage=outcome.get('usage'),
                subscription_allowance=None)
