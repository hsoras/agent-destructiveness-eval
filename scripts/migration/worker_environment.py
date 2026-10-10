"""Trusted, per-attempt Antigravity policy. Never edit global/shared policies."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import uuid


class EnvironmentUnavailable(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _source_pyproject_binding():
    """Bind the trusted coordinator checkout's exact regular TOML file, if present."""
    path = Path(__file__).resolve().parents[2] / 'pyproject.toml'
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise EnvironmentUnavailable('permissions', 'Unsafe source pyproject.toml: '+str(e))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise EnvironmentUnavailable('permissions', 'Source pyproject.toml is not a regular file')
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return dict(path=str(path), sha256=digest.hexdigest())


def host_readiness():
    """The coordinator must already have authorized host execution; no escalation here."""
    try:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
    except OSError as e:
        raise EnvironmentUnavailable('initialization', 'Coordinator cannot initialize agy localhost: '+str(e))


def development_commands(commands):
    """Select local development commands; coordinator validation is never changed."""
    allowed, coordinator = [], []
    for argv in commands:
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise EnvironmentUnavailable('permissions', 'Invalid development argv')
        executable = Path(argv[0]).name
        tail = argv[1:]
        python = bool(re.fullmatch(r'python(?:3(?:\.\d+)?)?', executable))
        module = tail[1:] if tail[:1] == ['-B'] else tail
        eligible = (python and module[:2] in (['-m', 'pytest'], ['-m', 'unittest']))
        eligible |= executable == 'make' and len(tail) == 1 and tail[0] in ('build', 'test', 'lint', 'typecheck', 'smoke-build')
        eligible |= executable == 'npm' and tail[:1] == ['run'] and len(tail) == 2 and tail[1] in ('build', 'test', 'lint', 'typecheck')
        eligible |= executable == 'npm' and len(tail) == 4 and tail[0] == '--prefix' and tail[2] == 'run' and tail[3] in ('build', 'test', 'lint', 'typecheck')
        if any(re.search(r'[\n\r`$<>|;&{}]', x) for x in argv):
            eligible = False
        (allowed if eligible else coordinator).append(argv)
    return allowed, coordinator


def git_boundary(cwd):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()
    common = Path(git('rev-parse', '--git-common-dir'))
    if not common.is_absolute():
        common = Path(cwd)/common
    common = common.resolve()
    private = Path(git('rev-parse', '--absolute-git-dir')).resolve()
    branch = git('symbolic-ref', 'HEAD')
    return dict(private_git_dir=str(private), common_git_dir=str(common),
                head=str(private/'HEAD'), index=str(private/'index'),
                commondir=str(private/'commondir'), branch_ref=str(common/branch),
                objects=str(common/'objects'), config=str(common/'config'),
                owner='trusted coordinator; no external Git mounts granted')


def create_project(cwd, commands, *, client_commands=(), protected=(),
                   request_directory=None, response_directory=None, projects_dir=None):
    root = Path(cwd).resolve()
    allowed, coordinator = development_commands(commands)
    boundary = git_boundary(root)
    project_id = str(uuid.uuid4())
    directory = Path(projects_dir) if projects_dir else Path.home()/'.gemini/config/projects'
    directory.mkdir(parents=True, exist_ok=True)
    grants = ['command('+shlex.join(argv)+')' for argv in [*allowed, *client_commands]]
    source_pyproject = _source_pyproject_binding()
    if source_pyproject is not None:
        grants.append('read_file('+source_pyproject['path']+')')
    if request_directory is not None:
        grants.append('write_file('+str(Path(request_directory).resolve())+')')
    if response_directory is not None:
        grants.append('read_file('+str(Path(response_directory).resolve())+')')
    if client_commands:
        grants.append('read_file('+str(Path(__file__).resolve().parent)+')')
    # Dependencies are read-only; they are not added as writable workspace folders.
    for argv in [*allowed, *client_commands]:
        executable = Path(argv[0])
        if executable.is_absolute():
            dependencies = executable.parent.parent if executable.parent.name == 'bin' else executable.parent
            grants.append('read_file('+str(dependencies)+')')
            resolved = executable.resolve()
            runtime = resolved.parent.parent if resolved.parent.name == 'bin' else resolved.parent
            grants.append('read_file('+str(runtime)+')')
    # CLI 1.3.2 treats unsandboxed Deny rules as ordinary-command denials too.
    # Keep --sandbox forced and reject all inherited unsandboxed Allow grants;
    # the CLI's default/headless policy rejects requests to bypass the sandbox.
    denies = ['command(sudo)', 'command(git)', 'command(git add)', 'command(git commit)', 'command(git push)',
              'command(git reset)', 'command(git checkout)', 'command(git switch)',
              'command(docker)', 'command(orb)',
              'write_file('+boundary['common_git_dir']+')',
              'read_file('+boundary['common_git_dir']+')',
              'write_file('+str(root/'.git')+')',
              'write_file('+str(Path.home()/'.gemini')+')',
              'read_file('+str(Path.home()/'.orbstack')+')',
              'read_file('+str(Path.home()/'.docker')+')',
              'read_file(/var/run/docker.sock)',
              'read_file('+str(Path.home()/'.gemini/config')+')',
              'write_file('+str(Path(__file__).resolve().parent)+')']
    denies += ['write_file('+str(Path(p).resolve())+')' for p in protected]
    data = dict(id=project_id, name='Isolated Gemini worker '+project_id,
                projectResources=dict(resources=[dict(folderUri=root.as_uri())]),
                permissionGrants=dict(permissionGrants=dict(allow=sorted(set(grants)), deny=denies)))
    path = directory/(project_id+'.json')
    try:
        with path.open('x') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
        path.chmod(0o600)
    except OSError as e:
        raise EnvironmentUnavailable('permissions', 'Cannot create isolated worker project: '+str(e))
    return dict(id=project_id, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                commands=[*allowed, *client_commands], coordinator_only_commands=coordinator,
                permissions=data['permissionGrants']['permissionGrants'], git_boundary=boundary,
                workspace=str(root), source_pyproject=source_pyproject)


def selected_project_policy(project, cwd):
    """Validate the exact project passed to agy; zero-turn /permissions ignores --project.

    CLI 1.3.2 applies project grants when the actual conversation starts, after the
    slash-command fast path. Merge this verified selected policy with shared/global
    inspection; never treat the empty fast-path project scope as the selected one.
    """
    path = Path(project['path'])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != project['sha256']:
        raise EnvironmentUnavailable('permissions', 'Worker project policy changed after creation')
    data = json.loads(raw)
    if data['id'] != project['id'] or data['projectResources']['resources'] != [dict(folderUri=Path(cwd).resolve().as_uri())]:
        raise EnvironmentUnavailable('permissions', 'Worker project ID/workspace mismatch')
    rules = data['permissionGrants']['permissionGrants']
    if rules != project['permissions']:
        raise EnvironmentUnavailable('permissions', 'Worker project rules mismatch')
    # Validate the metadata shape before comparing it with the trusted file.
    # A missing key and an explicit null are both valid only when the project
    # was created without a source grant, and that grant must be absent too.
    if 'source_pyproject' not in project:
        raise EnvironmentUnavailable('permissions', 'Source pyproject provenance missing')
    binding = project['source_pyproject']
    if binding is not None and (
            not isinstance(binding, dict) or set(binding) != {'path', 'sha256'} or
            not isinstance(binding['path'], str) or not binding['path'] or
            not isinstance(binding['sha256'], str) or
            not re.fullmatch(r'[0-9a-f]{64}', binding['sha256'])):
        raise EnvironmentUnavailable('permissions', 'Invalid source pyproject provenance')
    current_binding = _source_pyproject_binding()
    if binding != current_binding:
        raise EnvironmentUnavailable('permissions', 'Source pyproject provenance changed')
    source_path = binding['path'] if binding is not None else str(Path(__file__).resolve().parents[2] / 'pyproject.toml')
    source_grant = 'read_file('+source_path+')'
    if (rules['allow'].count(source_grant) != (1 if binding is not None else 0)):
        raise EnvironmentUnavailable('permissions', 'Source pyproject grant/provenance mismatch')
    return rules


RESULT_SCHEMA = dict(type='object', properties={
    'changed_paths':dict(type='array',items=dict(type='string')),
    'tests':dict(type='array',items=dict(type='object',properties={
        'command':dict(type='array',items=dict(type='string')),
        'passed':dict(type='boolean'),'evidence':dict(type='string')},
        required=['command','passed','evidence'],additionalProperties=False)),
    'guest_operations':dict(type='array',items=dict(type='object',properties={
        'operation':dict(type='string',enum=['build','run']),
        'passed':dict(type='boolean'),'evidence':dict(type='string')},
        required=['operation','passed','evidence'],additionalProperties=False)),
    'limitations':dict(type='array',items=dict(type='string'))},
    required=['changed_paths','tests','guest_operations','limitations'],additionalProperties=False)


def valid_handoff(value):
    if not isinstance(value,dict) or set(value)!=set(RESULT_SCHEMA['required']):
        return False
    for name in ('changed_paths','limitations'):
        if not isinstance(value[name],list) or not all(isinstance(v,str) for v in value[name]):
            return False
    for name in ('tests','guest_operations'):
        if not isinstance(value[name],list):
            return False
        for entry in value[name]:
            key='command' if name=='tests' else 'operation'
            if (not isinstance(entry,dict) or set(entry)!={key,'passed','evidence'} or
                    not isinstance(entry['passed'],bool) or not isinstance(entry['evidence'],str)):
                return False
            if key=='command' and (not isinstance(entry[key],list) or not entry[key] or
                                  not all(isinstance(s,str) for s in entry[key])):
                return False
            if key=='operation' and entry[key] not in ('build','run'):
                return False
    return True
