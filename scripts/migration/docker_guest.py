#!/usr/bin/env python3
"""Bounded Docker broker for a dedicated isolated OrbStack machine.

The Gemini client sees a file queue, never a host daemon socket or OrbStack IPC.
The trusted runner alone executes fixed build/run operations. Provisioning is an
explicit operator step; this module never creates, restarts, or reconfigures hosts.
"""
import argparse
import hashlib
import io
import json
import os
import selectors
import signal
import stat
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import threading
import time
import uuid

from orchestrate import atomic
from worker_environment import EnvironmentUnavailable


def bounded_process(command, data=None, timeout=120, limit=262144, cancel=None, cwd=None):
    """Cap both output streams and terminate only our subprocess group on failure."""
    output = {"stdout": bytearray(), "stderr": bytearray()}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, cwd=cwd)
    payload = memoryview(data or b'')
    offset = 0
    reason = None
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        for name in output:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if payload:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            process.stdin.close()
        while selector.get_map():
            if cancel is not None and cancel.is_set():
                reason = 'Guest command cancelled'
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = 'Guest command timed out'
                break
            for key, _ in selector.select(min(.1, remaining)):
                if key.data == 'stdin':
                    try:
                        offset += os.write(key.fd, payload[offset:offset + 65536])
                    except BrokenPipeError:
                        offset = len(payload)
                    if offset == len(payload):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                elif len(output[key.data]) + len(chunk) > limit:
                    reason = 'Guest command output exceeded limit'
                    break
                else:
                    output[key.data].extend(chunk)
            if reason:
                break
    if reason:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        code = process.wait(timeout=max(.1, deadline-time.monotonic()))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        reason = 'Guest command timed out'
    for stream in (process.stdin, process.stdout, process.stderr):
        stream.close()
    if reason:
        code = 124 if 'timed out' in reason else (130 if 'cancelled' in reason else 125)
        output['stderr'].extend(('\n'+reason).encode())
    return subprocess.CompletedProcess(command, code,
        output['stdout'].decode(errors='replace'), output['stderr'].decode(errors='replace'))


class GuestBroker:
    def __init__(self, cwd, directory, config):
        self.cwd = Path(cwd).resolve()
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.requests = self.directory/'requests'
        self.responses = self.directory/'responses'
        self.receipts = self.directory/'receipts'
        for p in (self.requests, self.responses, self.receipts):
            p.mkdir()
        self.machine = config['machine']
        if not re.fullmatch(r'gemini-worker-[a-z0-9-]{1,64}', self.machine):
            raise EnvironmentUnavailable('permissions', 'Guest must be a dedicated gemini-worker-* machine')
        self.files = config['files']
        if (not isinstance(self.files, list) or not self.files or len(self.files) > 256 or
                any(not isinstance(n, str) or Path(n).is_absolute() or '..' in Path(n).parts or
                    any(part.startswith('.') for part in Path(n).parts) or Path(n).name=='Dockerfile' for n in self.files)):
            raise EnvironmentUnavailable('permissions', 'Guest context must be explicit non-hidden relative files')
        self.base_image = config.get('base_image', 'python:3.11-slim')
        if not re.fullmatch(r'python:3\.11-slim(?:@sha256:[0-9a-f]{64})?', self.base_image):
            raise EnvironmentUnavailable('permissions', 'Unapproved guest base image')
        self.tag = 'gemini-worker:'+uuid.uuid4().hex
        self.config = self.inspect_machine()
        info = self.orb(['docker', 'info', '--format', '{{.ID}}'])
        if info.returncode or not info.stdout.strip():
            raise EnvironmentUnavailable('initialization', 'Dedicated guest Docker daemon is not ready')
        self.daemon_id = info.stdout.strip()
        image = self.orb(['docker', 'image', 'inspect', self.base_image, '--format', '{{.Id}}'])
        if image.returncode or not re.fullmatch(r'sha256:[0-9a-f]{64}', image.stdout.strip()):
            raise EnvironmentUnavailable('initialization', 'Approved guest base image must already be cached')
        self.base_id = image.stdout.strip()
        self.container_name = 'gemini-worker-'+uuid.uuid4().hex
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.serve, daemon=False)
        self.records = []
        self.built_sources = None
        self.image_id = None
        atomic(self.directory/'environment.json', dict(machine=self.machine, machine_record=self.config,
              daemon_id=self.daemon_id, base_image=self.base_image, base_image_id=self.base_id,
              tag=self.tag, host_daemon_access=False, workspace=str(self.cwd), files=self.files))
        # Pin the task directory before the worker starts. Every descendant is
        # opened relative to this descriptor, without following any symlinks.
        self.root_fd = os.open(self.cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.request_fd = os.open(self.requests, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def inspect_machine(self):
        p = bounded_process(['orb', 'info', self.machine, '--format', 'json'], timeout=30,
                            cancel=getattr(self, 'stop', None))
        try:
            record = json.loads(p.stdout)['record']
            settings = record['config']
            if (p.returncode or record['name'] != self.machine or record['state'] != 'running' or
                    settings.get('isolated') is not True or settings.get('isolate_network') is not True or
                    settings.get('forward_ssh_agent') is not False or settings.get('mounts')):
                raise ValueError('Guest isolation is incomplete')
            if settings.get('memory_limit_mib', 0) > 4096 or not settings.get('memory_limit_mib'):
                raise ValueError('Guest memory must be bounded at 4 GiB')
            if not 0 < settings.get('cpu_limit', 0) <= 2:
                raise ValueError('Guest CPUs must be bounded at two')
            return record
        except (ValueError, KeyError, TypeError) as e:
            raise EnvironmentUnavailable('permissions', str(e))

    def orb(self, argv, data=None, timeout=120, cleanup=False):
        command = ['orb', '-m', self.machine, '-u', 'root', '-w', '/',
                   *(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', *argv[1:]]
                     if argv[:1] == ['docker'] else argv)]
        return bounded_process(command, data, timeout,
                               cancel=None if cleanup else getattr(self, 'stop', None))

    def sources(self):
        sources = {}
        total = 0
        for name in self.files:
            directory = os.dup(self.root_fd)
            descriptor = None
            try:
                for part in Path(name).parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                descriptor = os.open(Path(name).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('Guest source is not a plain file: '+name)
                if info.st_size > 32*1024*1024 - total:
                    raise ValueError('Guest build context exceeds 32 MiB')
                with os.fdopen(descriptor, 'rb') as stream:
                    descriptor = None
                    content = stream.read(32*1024*1024 - total + 1)
            except OSError as error:
                raise ValueError('Guest source cannot be safely opened: '+name) from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                os.close(directory)
            total += len(content)
            if total > 32*1024*1024:
                raise ValueError('Guest build context exceeds 32 MiB')
            sources[name] = content
        return sources

    def execute(self, operation):
        current = self.inspect_machine()
        if current['id'] != self.config['id'] or current['config'] != self.config['config']:
            raise ValueError('Guest identity/isolation changed')
        sources = self.sources()
        hashes = {n:hashlib.sha256(b).hexdigest() for n,b in sources.items()}
        if operation == 'build':
            if self.image_id:
                raise ValueError('Each session builds once; use a new session for another build')
            context = io.BytesIO()
            with tarfile.open(fileobj=context, mode='w') as tar:
                recipe = (f'FROM {self.base_id}\nWORKDIR /app\nCOPY . /app/\n'
                          'USER 65534:65534\nCMD ["python", "-B", "-m", "unittest", "-v", "test_probe"]\n').encode()
                for name, content in {'Dockerfile':recipe, **sources}.items():
                    info = tarfile.TarInfo(name); info.size=len(content); info.mode=0o644
                    tar.addfile(info, io.BytesIO(content))
            p = self.orb(['docker','build','--network','none','--pull=false','-t',self.tag,'-'], context.getvalue())
            if p.returncode == 0:
                image = self.orb(['docker','image','inspect',self.tag,'--format','{{.Id}}'])
                if image.returncode or not re.fullmatch(r'sha256:[0-9a-f]{64}',image.stdout.strip()):
                    raise ValueError('Missing built image identity')
                self.image_id=image.stdout.strip(); self.built_sources=hashes
            marker='GUEST_BUILD_OK' if p.returncode == 0 else 'GUEST_BUILD_FAILED'
        elif operation == 'run':
            if not self.image_id or hashes != self.built_sources:
                raise ValueError('Guest run requires an unchanged verified build context')
            p=self.orb(['docker','run','--rm','--name',self.container_name,'--network','none','--read-only','--cap-drop','ALL',
                        '--security-opt','no-new-privileges','--pids-limit','64','--memory','256m',
                        '--cpus','1','--user','65534:65534',self.image_id])
            if p.returncode != 0:
                cleanup=self.orb(['docker','rm','-f',self.container_name],timeout=15,cleanup=True)
                if cleanup.returncode and 'No such container' not in cleanup.stderr:
                    raise ValueError('Guest container cleanup failed: '+cleanup.stderr)
            marker='GUEST_RUN_OK' if p.returncode == 0 else 'GUEST_RUN_FAILED'
        else:
            raise ValueError('Only build/run operations are allowed')
        return dict(operation=operation, exit=p.returncode, stdout=p.stdout[-262144:], stderr=p.stderr[-262144:],
                    marker=marker, machine=self.machine, daemon_id=self.daemon_id, image_id=self.image_id,
                    sources_sha256=hashes, origin='trusted guest coordinator', command=p.args)

    def serve(self):
        seen = set()
        try:
            while not self.stop.wait(.1):
                with os.scandir(self.request_fd) as entries:
                    count = 0
                    for entry in entries:
                        if self.stop.is_set():
                            break
                        count += 1
                        if count > 24:  # Includes temporary files used by atomic request publication.
                            raise ValueError('Guest queue entry limit exceeded')
                        if not entry.name.endswith('.json'):
                            continue
                        token=entry.name[:-5]
                        if token in seen:
                            continue
                        seen.add(token)
                        if len(seen)>8 or not re.fullmatch(r'[0-9a-f]{32}', token):
                            raise ValueError('Guest request limit/identity invalid')
                        try:
                            request = self.read_request(entry.name)
                            if not isinstance(request,dict) or set(request)!={'operation'}:
                                raise ValueError('Guest request accepts no arbitrary Docker arguments')
                            result=self.execute(request['operation'])
                        except Exception as e:
                            result=dict(exit=125,error=str(e),origin='trusted guest coordinator')
                        result['request_id']=token
                        atomic(self.receipts/(token+'.json'),result)
                        self.records.append(result)
                        atomic(self.responses/(token+'.json'),result)
        except Exception as e:
            self.error=str(e)

    def read_request(self, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.request_fd)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError('Guest request must be a bounded regular file')
            raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('Guest request exceeded size limit')
        return json.loads(raw)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=170)
        if self.thread.is_alive():
            self.error='Guest coordinator did not stop'
        else:
            os.close(self.root_fd)
            os.close(self.request_fd)
        atomic(self.directory/'summary.json',dict(records=self.records,error=self.error,
              machine=self.machine,daemon_id=self.daemon_id,host_daemon_access=False))

    def client_commands(self):
        return [[sys.executable,'-B',str(Path(__file__).resolve()),'client','--queue',str(self.directory),op]
                for op in ('build','run')]


def client(directory, operation):
    root=Path(directory).resolve(); token=uuid.uuid4().hex
    atomic(root/'requests'/(token+'.json'),dict(operation=operation))
    deadline=time.monotonic()+140
    response=root/'responses'/(token+'.json')
    while time.monotonic()<deadline:
        if response.is_file():
            result=json.loads(response.read_text())
            print(result.get('stdout',''),end=''); print(result.get('stderr',''),end='')
            print(result.get('marker',result.get('error','Guest failure')))
            print('GUEST_RECEIPT '+token)
            return result['exit']
        time.sleep(.1)
    print('Guest coordinator timeout',file=sys.stderr)
    return 124


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['client']);parser.add_argument('--queue',required=True)
    parser.add_argument('operation',choices=['build','run'])
    args=parser.parse_args();sys.exit(client(args.queue,args.operation))
