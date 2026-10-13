"""Checkpoint-bound HTTP observer intervals; strict single-process validation remains the default.

Recording owner hook: ``import_service_receipts(bundle, *, validator=validate_bundle)``
may invoke the callable returned by ``interval_validator(checkpoints)``. No interval is
accepted as an unfinished prefix without exact raw-byte and saved-health binding.
"""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import tarfile
from sandbox.artifact_upload.receipts import SCHEMA, validate_bundle, _validate_bundle


def _raw_artifact(snapshot, source):
    matches = [a for a in snapshot['artifacts'] if a.get('source') == source and a.get('kind') == 'layer-path']
    if len(matches) != 1:
        raise ValueError('checkpoint receipt artifact missing or ambiguous')
    artifact = matches[0]
    raw = Path(artifact['path']).read_bytes()
    if len(raw) != artifact['bytes'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
        raise ValueError('checkpoint receipt archive digest differs')
    import io
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = [m for m in archive.getmembers() if m.isfile()]
        if len(members) != 1 or any(m.issym() or m.islnk() for m in archive.getmembers()):
            raise ValueError('ambiguous receipt archive')
        return archive.extractfile(members[0]).read()


def prior_prefixes(checkpoints):
    prefixes = []
    for checkpoint in checkpoints:
        snapshot = checkpoint.get('guest_and_service', checkpoint)
        raw = _raw_artifact(snapshot, '/srv/store.receipts.jsonl')
        health = json.loads(_raw_artifact(snapshot, '/srv/store.receipts.health.json'))
        prefixes.append({'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'health': health})
    return prefixes


def _bundle(raw, health):
    return {'schema': SCHEMA, 'source_bytes_b64': base64.b64encode(raw).decode(),
            'source_size_bytes': len(raw), 'source_sha256': hashlib.sha256(raw).hexdigest(), 'source_health': health}


def validate_intervals(bundle, prior):
    if not prior:
        return validate_bundle(bundle)
    raw = base64.b64decode(bundle['source_bytes_b64'], validate=True)
    if bundle['schema'] != SCHEMA or len(raw) != bundle['source_size_bytes'] or hashlib.sha256(raw).hexdigest() != bundle['source_sha256']:
        raise ValueError('receipt source bytes disagree')
    groups = []
    for line in raw.splitlines(keepends=True):
        row = json.loads(line)
        if not groups or row['run_id'] != groups[-1][0]:
            groups.append((row['run_id'], bytearray()))
        groups[-1][1].extend(line)
    if len(groups) != len(prior) + 1 or len({rid for rid, _ in groups}) != len(groups):
        raise ValueError('unexpected observer intervals')
    records, exchanges, intervals, attempts = [], {}, [], set()
    offset = 0
    for index, (rid, data) in enumerate(groups):
        offset += len(data)
        if index < len(prior):
            binding = prior[index]
            if binding['size'] != offset or hashlib.sha256(raw[:offset]).hexdigest() != binding['sha256']:
                raise ValueError('checkpoint receipt prefix digest or length differs')
            checked = _validate_bundle(_bundle(bytes(data), binding['health']), allow_unsealed=True)
        else:
            checked = validate_bundle(_bundle(bytes(data), bundle['source_health']))
        for eid, slot in checked['exchanges'].items():
            aid = slot['begin']['attempt_id']
            if eid in exchanges or aid in attempts:
                raise ValueError('duplicate observer exchange/attempt across intervals')
            attempts.add(aid)
            exchanges[eid] = slot
        unfinished = [eid for eid, slot in checked['exchanges'].items() if 'end' not in slot]
        intervals.append({'run_id': rid, 'sealed': checked['records'][-1]['kind'] == 'seal',
                          'unfinished_exchange_ids': unfinished, 'end_offset': offset})
        records.extend(checked['records'])
    return {'bundle': bundle, 'records': records, 'exchanges': exchanges, 'run_id': groups[-1][0], 'intervals': intervals}


def interval_validator(checkpoints):
    bindings = prior_prefixes(checkpoints)
    return lambda bundle: validate_intervals(bundle, bindings)


def import_interval_receipts(recorder, bundle, *, validator):
    """Compatibility importer until RunRecorder exposes its optional validator hook.

    Keeps the exact source first and uses the recorder's existing terminal failure
    and event append paths. Recording owners may replace this with the equivalent
    ``recorder.import_service_receipts(bundle, validator=validator)`` hook.
    """
    import os
    try:
        raw = base64.b64decode(bundle['source_bytes_b64'], validate=True)
        source = recorder.private / ('service-receipts-' + hashlib.sha256(raw).hexdigest() + '.jsonl')
        if source.exists():
            if source.is_symlink() or source.read_bytes() != raw:
                raise ValueError('source retention collision')
        else:
            fd = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        validated = validator(bundle)
    except Exception:
        recorder.fail('service receipt source incomplete')
        raise
    for record in validated['records']:
        recorder.append('service_events', record)
    return validated
