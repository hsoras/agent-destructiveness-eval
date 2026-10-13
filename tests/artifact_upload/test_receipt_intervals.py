import base64
import hashlib
import io
import json
import tarfile

import pytest

from eval.artifact_upload.receipt_intervals import prior_prefixes, validate_intervals, _bundle
from sandbox.artifact_upload.receipts import ServiceReceipts, validate_bundle


def archive(tmp_path, name, raw, source):
    path = tmp_path / name
    with tarfile.open(path, 'w') as tar:
        member = tarfile.TarInfo(source.rsplit('/', 1)[-1])
        member.size = len(raw)
        tar.addfile(member, io.BytesIO(raw))
    return {'kind': 'layer-path', 'source': source, 'path': str(path), 'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def restarted(tmp_path):
    store = tmp_path / 'private'
    store.mkdir(mode=0o700)
    path = store / 'receipts.jsonl'
    first = ServiceReceipts(path)
    first.begin(b'GET / HTTP/1.1', 'b' * 32)
    raw = path.read_bytes()
    snapshot = {'artifacts': [archive(tmp_path, 'receipts.tar', raw, '/srv/store.receipts.jsonl'),
        archive(tmp_path, 'health.tar', json.dumps(first.health()).encode(), '/srv/store.receipts.health.json')]}
    prior = prior_prefixes([snapshot])
    second = ServiceReceipts(path)
    second.seal()
    return second.bundle(), prior, snapshot


def test_exact_unfinished_checkpoint_prefix_is_explicit_and_final_interval_is_strict(restarted):
    bundle, prior, _ = restarted
    with pytest.raises(ValueError):
        validate_bundle(bundle)
    result = validate_intervals(bundle, prior)
    assert [i['sealed'] for i in result['intervals']] == [False, True]
    assert len(result['intervals'][0]['unfinished_exchange_ids']) == 1
    assert len(result['exchanges']) == 1


def test_prefix_digest_length_and_interval_count_are_bound(restarted):
    bundle, prior, _ = restarted
    for change in ({'sha256': '0' * 64}, {'size': prior[0]['size'] - 1}):
        with pytest.raises(ValueError, match='prefix'):
            validate_intervals(bundle, [{**prior[0], **change}])
    with pytest.raises(ValueError, match='intervals'):
        validate_intervals(bundle, prior * 2)


def test_archive_tamper_is_rejected(restarted):
    _, _, snapshot = restarted
    artifact = snapshot['artifacts'][0]
    from pathlib import Path
    Path(artifact['path']).write_bytes(b'bad')
    with pytest.raises(ValueError, match='archive digest'):
        prior_prefixes([snapshot])


def test_final_interval_tamper_is_rejected(restarted):
    bundle, prior, _ = restarted
    raw = base64.b64decode(bundle['source_bytes_b64'])
    rows = [json.loads(line) for line in raw.splitlines()]
    rows[-1]['sequence'] += 1
    with pytest.raises(ValueError, match='sequence'):
        validate_intervals(_bundle(raw[:prior[0]['size']] + b''.join((json.dumps(r) + '\n').encode() for r in rows[2:]), bundle['source_health']), prior)


def test_sealed_prior_interval(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'receipts.jsonl'
    first = ServiceReceipts(path)
    first.seal()
    raw = path.read_bytes()
    prior = [{'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'health': first.health()}]
    second = ServiceReceipts(path)
    second.seal()
    assert all(i['sealed'] for i in validate_intervals(second.bundle(), prior)['intervals'])


def test_cross_interval_exchange_and_attempt_ids_must_be_unique(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'receipts.jsonl'
    first = ServiceReceipts(path)
    eid = first.begin(b'GET / HTTP/1.1', 'a' * 32)
    raw = path.read_bytes()
    prior = [{'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'health': first.health()}]
    second = ServiceReceipts(path)
    new_eid = second.begin(b'GET / HTTP/1.1', 'a' * 32)
    second.emit('request', exchange_id=new_eid, attempt_id='a' * 32, declared_length='0')
    empty = hashlib.sha256(b'').hexdigest()
    second.end(new_eid, parse_ok=True, body={'termination': 'fully_read', 'observed_size_bytes': 0,
        'observed_prefix_sha256': empty, 'complete_sha256': empty},
        response_write={'client_receipt': 'unknown', 'model_exposure': 'unknown',
            'successful_size_bytes': 0, 'successful_sha256': empty, 'failed': False})
    second.seal()
    with pytest.raises(ValueError, match='duplicate'):
        validate_intervals(second.bundle(), prior)
    # Also isolate the exchange collision while assigning a new attempt ID.
    bundle = second.bundle()
    tail = base64.b64decode(bundle['source_bytes_b64'])[len(raw):].replace(new_eid.encode(), eid.encode()).replace(b'a' * 32, b'b' * 32)
    with pytest.raises(ValueError, match='duplicate'):
        validate_intervals(_bundle(raw + tail, bundle['source_health']), prior)


def test_three_intervals_need_both_exact_ordered_checkpoint_prefixes(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'receipts.jsonl'
    prefixes = []
    for _ in range(2):
        observer = ServiceReceipts(path)
        raw = path.read_bytes()
        prefixes.append({'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'health': observer.health()})
    final = ServiceReceipts(path)
    final.seal()
    checked = validate_intervals(final.bundle(), prefixes)
    assert [i['sealed'] for i in checked['intervals']] == [False, False, True]
    with pytest.raises(ValueError, match='prefix'):
        validate_intervals(final.bundle(), prefixes[::-1])
    with pytest.raises(ValueError, match='intervals'):
        validate_intervals(final.bundle(), prefixes[:1])
