"""Protected HTTP application-reader/writer observations, independent of business audit."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
import threading
import time
import uuid

SCHEMA = 'artifact-http-observer.v1'


class ReceiptFailure(RuntimeError):
    pass


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


class ServiceReceipts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.path.parent.stat().st_mode & 0o077:
            raise PermissionError('receipt directory must be private')
        self.run_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.sequence = 0
        self.active = set()
        self.expected = []
        self.failure = None
        self.sealed = False
        self.mutations_stopped = False
        self.pid = os.getpid()
        self.status_path = self.path.with_suffix('.health.json')
        # Preserve prior bytes on restart. Mixed process intervals cannot validate
        # as one complete bundle, but business storage recovery stays unchanged.
        self.persisted_bytes = self.path.stat().st_size if self.path.exists() else 0
        self.emit('startup', pid=os.getpid(), started_at=time.time())

    def _status(self):
        data = canonical(self.health())
        temporary = self.status_path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, self.status_path)

    def health(self):
        with self.lock:
            return {'schema': SCHEMA, 'run_id': self.run_id, 'healthy': self.failure is None,
                    'failure': self.failure, 'active': len(self.active), 'sequence': self.sequence,
                    'sealed': self.sealed, 'mutations_stopped': self.mutations_stopped,
                    'pid': self.pid, 'observed_at': time.time()}

    def emit(self, kind, **facts):
        with self.lock:
            if self.failure or self.sealed:
                raise ReceiptFailure('receipt interval unavailable')
            record = {'schema': SCHEMA, 'run_id': self.run_id, 'sequence': self.sequence + 1,
                      'kind': kind, 'observed_at': time.time(), 'monotonic_ns': time.monotonic_ns(), **facts}
            try:
                flags = os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW
                if self.sequence == 0 and self.persisted_bytes == 0:
                    flags |= os.O_CREAT
                fd = os.open(self.path, flags, 0o600)
                with os.fdopen(fd, 'ab') as f:
                    if os.fstat(f.fileno()).st_size != self.persisted_bytes:
                        raise ReceiptFailure('receipt file size changed')
                    data = canonical(record)
                    if f.write(data) != len(data):
                        raise ReceiptFailure('short receipt append')
                    f.flush(); os.fsync(f.fileno())
                    self.persisted_bytes += len(data)
                self.sequence += 1
                self._status()
            except Exception as exc:
                self.failure = 'receipt persistence failed'
                try:
                    self._status()
                except Exception:
                    pass
                raise ReceiptFailure(self.failure) from exc
            return record

    def begin(self, raw_requestline, attempt_id):
        with self.lock:
            exchange = uuid.uuid4().hex
            self.active.add(exchange)
            self.expected.append(exchange)
            self.emit('begin', exchange_id=exchange, attempt_id=attempt_id,
                      raw_requestline=raw_requestline.decode('iso-8859-1'))
            return exchange

    def end(self, exchange, **facts):
        with self.lock:
            self.emit('end', exchange_id=exchange, **facts)
            self.active.remove(exchange)
            self._status()

    def seal(self, **boundaries):
        with self.lock:
            if self.active:
                raise ReceiptFailure('active exchanges prevent seal')
            self.emit('seal', expected_exchange_ids=self.expected, active=0, healthy=True, **boundaries)
            self.sealed = True
            self._status()

    def bundle(self):
        raw = self.path.read_bytes()
        return {'schema': SCHEMA, 'source_bytes_b64': base64.b64encode(raw).decode(),
                'source_sha256': hashlib.sha256(raw).hexdigest(), 'source_size_bytes': len(raw),
                'source_health': self.health()}


class ReadObserver:
    def __init__(self, reader, sink, exchange):
        self.reader, self.sink, self.exchange = reader, sink, exchange
        self.count = 0
        self.digest = hashlib.sha256()
        self.called = False
        self.error = False
        self.eof = False

    def read(self, size=-1):
        self.called = True
        try:
            data = self.reader.read(size)
        except Exception:
            self.error = True
            self.sink.emit('read', exchange_id=self.exchange, outcome='read_error')
            raise
        self.eof = not data
        self.count += len(data)
        self.digest.update(data)
        self.sink.emit('read', exchange_id=self.exchange, outcome='returned', size_bytes=len(data),
                       sha256=hashlib.sha256(data).hexdigest(), cumulative_sha256=self.digest.hexdigest())
        return data

    def facts(self, declared_length, completed_empty=False):
        complete = not self.error and declared_length == self.count and (self.called or completed_empty)
        reason = ('read_error' if self.error else 'fully_read' if complete else
                  'short_body' if self.called else 'not_evaluated_early')
        return {'termination': reason, 'observed_size_bytes': self.count,
                'observed_prefix_sha256': self.digest.hexdigest(),
                'complete_sha256': self.digest.hexdigest() if complete else None}


class WriteObserver:
    def __init__(self, writer, sink, exchange):
        self.writer, self.sink, self.exchange = writer, sink, exchange
        self.count = 0
        self.digest = hashlib.sha256()
        self.failed = False

    def write(self, data):
        self.sink.emit('write', exchange_id=self.exchange, phase='prepared',
                       bytes_b64=base64.b64encode(data).decode(), size_bytes=len(data),
                       sha256=hashlib.sha256(data).hexdigest())
        try:
            result = self.writer.write(data)
        except Exception:
            self.failed = True
            self.sink.emit('write', exchange_id=self.exchange, phase='failed', partial_transfer='unknown')
            raise
        self.count += len(data)
        self.digest.update(data)
        self.sink.emit('write', exchange_id=self.exchange, phase='written', size_bytes=len(data))
        return result

    def __getattr__(self, key):
        return getattr(self.writer, key)


def validate_bundle(bundle):
    """Validate complete persistence/coverage; never accept a business-audit projection."""
    try:
        if bundle['schema'] != SCHEMA:
            raise ValueError('wrong source boundary')
        raw = base64.b64decode(bundle['source_bytes_b64'], validate=True)
        if len(raw) != bundle['source_size_bytes'] or hashlib.sha256(raw).hexdigest() != bundle['source_sha256']:
            raise ValueError('receipt source bytes disagree')
        records = [json.loads(line) for line in raw.splitlines()]
        if not raw.endswith(b'\n') or not records or records[0]['kind'] != 'startup' or records[-1]['kind'] != 'seal':
            raise ValueError('missing startup or terminal seal')
        run_id = records[0]['run_id']
        if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
            raise ValueError('invalid observer run identity')
        exchanges = {}
        ended = set()
        previous_time = -1
        attempt_ids = set()
        for sequence, record in enumerate(records, 1):
            if record['schema'] != SCHEMA or record['run_id'] != run_id or record['sequence'] != sequence:
                raise ValueError('receipt sequence or process identity gap')
            if not isinstance(record['monotonic_ns'], int) or record['monotonic_ns'] < previous_time:
                raise ValueError('observer monotonic time invalid')
            previous_time = record['monotonic_ns']
            kind = record['kind']
            if kind == 'begin':
                eid = record['exchange_id']
                aid = record['attempt_id']
                if (not re.fullmatch(r'[0-9a-f]{32}', eid) or not re.fullmatch(r'[0-9a-f]{32}', aid)
                        or eid in exchanges or aid in attempt_ids):
                    raise ValueError('duplicate or invalid observer exchange/attempt')
                attempt_ids.add(aid)
                exchanges[eid] = {'begin': record, 'events': []}
            elif kind in ('request', 'read', 'write', 'end'):
                eid = record['exchange_id']
                if eid not in exchanges or eid in ended:
                    raise ValueError('unmatched exchange event')
                exchanges[eid]['events'].append(record)
                if kind == 'request':
                    if 'request' in exchanges[eid]:
                        raise ValueError('duplicate request facts')
                    exchanges[eid]['request'] = record
                if kind == 'end':
                    exchanges[eid]['end'] = record
                    ended.add(eid)
            elif kind not in ('startup', 'seal', 'boundary') or (kind == 'startup' and sequence != 1) or (kind == 'seal' and sequence != len(records)):
                raise ValueError('unexpected observer event')
        health = bundle['source_health']
        if (health['schema'] != SCHEMA or health['run_id'] != run_id or health['healthy'] is not True
                or health['sealed'] is not True or health['sequence'] != len(records) or health['active'] != 0
                or health['pid'] != records[0]['pid']):
            raise ValueError('terminal observer health unavailable or inconsistent')
        seal = records[-1]
        if seal['active'] != 0 or seal['healthy'] is not True or seal['expected_exchange_ids'] != list(exchanges) or ended != set(exchanges):
            raise ValueError('incomplete observer coverage')
        for slot in exchanges.values():
            if 'request' not in slot or slot['end'].get('parse_ok') is not True:
                raise ValueError('unreconciled parse failure')
            if slot['request']['attempt_id'] != slot['begin']['attempt_id']:
                raise ValueError('request private attempt identity changed')
            body = slot['end']['body']
            if (body['termination'] not in ('fully_read', 'not_evaluated_early', 'short_body', 'read_error')
                    or type(body['observed_size_bytes']) is not int or body['observed_size_bytes'] < 0
                    or not re.fullmatch(r'[0-9a-f]{64}', body['observed_prefix_sha256'])):
                raise ValueError('invalid body observation')
            reads = [r for r in slot['events'] if r['kind'] == 'read' and r['outcome'] == 'returned']
            if sum(r['size_bytes'] for r in reads) != body['observed_size_bytes']:
                raise ValueError('body observation gap')
            if not reads and body['observed_prefix_sha256'] != hashlib.sha256(b'').hexdigest():
                raise ValueError('unread prefix is not empty')
            if reads and reads[-1]['cumulative_sha256'] != body['observed_prefix_sha256']:
                raise ValueError('observed body digest disagrees')
            if body['termination'] == 'fully_read' and (body['complete_sha256'] != body['observed_prefix_sha256'] or body['observed_size_bytes'] != int(slot['request']['declared_length'])):
                raise ValueError('full body does not match framing')
            if body['termination'] != 'fully_read' and body['complete_sha256'] is not None:
                raise ValueError('unread body claimed complete')
            prepared = None
            written = bytearray()
            failed = False
            for r in slot['events']:
                if r['kind'] != 'write':
                    continue
                if r['phase'] == 'prepared':
                    if prepared is not None:
                        raise ValueError('unclosed write')
                    prepared = base64.b64decode(r['bytes_b64'], validate=True)
                    if len(prepared) != r['size_bytes'] or hashlib.sha256(prepared).hexdigest() != r['sha256']:
                        raise ValueError('response bytes disagree')
                elif r['phase'] in ('written', 'failed') and prepared is not None:
                    if r['phase'] == 'written':
                        if r['size_bytes'] != len(prepared):
                            raise ValueError('response write size disagrees')
                        written.extend(prepared)
                    else:
                        failed = True
                    prepared = None
                else:
                    raise ValueError('unpaired write')
            response = slot['end']['response_write']
            if response['client_receipt'] != 'unknown' or response['model_exposure'] != 'unknown':
                raise ValueError('server observer cannot certify client/model receipt')
            if prepared is not None or response['successful_size_bytes'] != len(written) or response['successful_sha256'] != hashlib.sha256(written).hexdigest() or response['failed'] != failed:
                raise ValueError('response observation gap')
        return {'bundle': bundle, 'records': records, 'exchanges': exchanges, 'run_id': run_id}
    except (KeyError, TypeError, IndexError, OverflowError, json.JSONDecodeError) as exc:
        raise ValueError('malformed service receipt bundle') from exc
