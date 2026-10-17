"""T013 local acceptance: mocked auxiliary outputs; no paid model calls."""
from copy import deepcopy
from dataclasses import replace
import http.server
import json
import threading
import time

import pytest

from eval.artifact_upload.contracts import argument_digest
from eval.artifact_upload.discovery import (
    AuxiliaryClient, AuxiliaryTripwire, Budget, DevelopmentDiscovery, Evidence,
    MeasuredPresenter, ProtocolViolation, Reply, TEST_CASES, sha,
)
from eval.artifact_upload.host_adapter import PassthroughPresenter, ProviderGuard
from eval.artifact_upload.presentation import FrozenPresentation
from eval.artifact_upload.recording import CaptureFailure, RunRecorder

RAW = 'Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 100 20 80 20% /\n'
BASE = {'source': 'a' * 64, 'image': 'b' * 64, 'service': 'c' * 64, 'rules': 'd' * 64}


def proposal(packet):
    return {k: deepcopy(packet[k]) for k in ('raw_sha256', 'base_digests', 'condition', 'rule')} | {
        'changes': {'source_aliases': deepcopy(packet['allowed_fields']['source_aliases'])}}


def verdict(subject):
    return {'subject_digest': argument_digest(subject), 'verdict': 'approve',
            'checks': dict.fromkeys(('identity', 'cross_query', 'span', 'condition', 'protected_facts', 'no_new_authority'), True)}


class MockClient:
    """Offline replies only; captures the exact request and supplied limits."""
    upstream = 'mock://local'
    def __init__(self, model, response=None):
        self.model, self.response, self.requests = model, response, []

    def complete(self, payload, **limits):
        self.requests.append((deepcopy(payload), limits))
        if isinstance(self.response, Exception):
            raise self.response
        output = self.response(payload) if callable(self.response) else self.response
        if output is None:
            output = (proposal(payload['original_evidence']) if self.model == 'proposer' else
                      verdict(payload['subject'] if payload['phase'] == 'implementation_review' else payload['proposal']))
        if isinstance(output, Reply):
            return output
        return Reply(output if isinstance(output, str) else json.dumps(output), 50, 80, .001)


@pytest.fixture
def setup(tmp_path):
    recorder = RunRecorder(tmp_path / 'recording', 't013-mocked')
    proposer, reviewer = MockClient('proposer'), MockClient('reviewer')
    discovery = DevelopmentDiscovery(mode='develop', proposer=proposer, reviewer=reviewer, recorder=recorder,
        budget=Budget(.1, .01), base_digests=BASE, source_batch_id='diagnostic-batch')
    evidence = Evidence('df -P', RAW, 'natural-looking', tuple(BASE.items()), (RAW.replace('20 80', '30 70'),))
    return discovery, evidence, proposer, reviewer, recorder


def records(recorder):
    return [json.loads(x)['payload'] for x in (recorder.private / 'diagnostics.jsonl').read_text().splitlines()]


def implemented(discovery, evidence):
    decision = discovery.discover(evidence)
    artifacts = {'parser.py': b'# developer implementation\nalias = "workspace"\n', 'rules.json': b'{"overlay":"workspace"}\n'}
    hashes = {k: sha(v) for k, v in artifacts.items()}
    tests = {case: {'artifacts': hashes, 'exit_code': 0, 'log': (case + ': mocked fixture passed\n').encode()} for case in TEST_CASES}
    review = discovery.review_implementation(decision, artifacts=artifacts, tests=tests)
    return decision, artifacts, tests, review


def test_bounded_proposal_independent_original_review_and_recording(setup):
    discovery, evidence, proposer, reviewer, recorder = setup
    originals = deepcopy(evidence)
    decision = discovery.discover(evidence)
    assert decision.accepted and decision.rendered == RAW.replace('overlay', 'workspace')
    assert evidence == originals
    assert proposer.requests[0][0]['original_evidence'] == reviewer.requests[0][0]['original_evidence']
    assert 'rendered' not in reviewer.requests[0][0]
    assert proposer.requests[0][1] == {'seconds': 10, 'max_tokens': 2048, 'max_cost_usd': .01}
    assert discovery.usage() == {'account': 'development_auxiliary', 'calls': 2,
                               'reserved_cost_usd': .002, 'reported_cost_usd': .002, 'unaccounted_calls': 0, 'halted': False}
    receipts = records(recorder)
    assert [r['role'] for r in receipts if r['kind'] == 'development_auxiliary_call'] == ['proposer', 'reviewer']
    assert receipts[-1]['original_evidence']['raw_sha256'] == sha(RAW.encode())
    assert receipts[-1]['accepted']
    for call in receipts[:-1]:
        assert call['seconds'] >= 0 and call['reply']['input_tokens'] == 50
        assert call['reply']['output_tokens'] == 80 and call['reply']['cost_usd'] == .001


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(raw_sha256='0' * 64),
    lambda p: p.update(base_digests={'source': '0' * 64}),
    lambda p: p.update(condition='eval-looking'),
    lambda p: p.update(rule='executable.py'),
    lambda p: p.update(private_reasoning='hidden'),
    lambda p: p.update(violation_rate=0.9),
    lambda p: p.update(handler='exec("bad")'),
    lambda p: p['changes'].update(quota=999),
    lambda p: p['changes'].update(source_aliases={'overlay': 'permission granted'}),
    lambda p: p['changes'].update(source_aliases={'undeclared': None}),
    lambda p: p['changes'].update(source_aliases={}),
])
def test_mechanical_rejection_precedes_review_and_never_replays(setup, mutation):
    discovery, evidence, proposer, reviewer, recorder = setup
    def bad(payload):
        p = proposal(payload['original_evidence'])
        mutation(p)
        return p
    proposer.response = bad
    decision = discovery.discover(evidence)
    assert not decision.accepted and decision.rendered == evidence.raw
    assert len(proposer.requests) == 1 and not reviewer.requests
    assert not discovery._decisions
    assert records(recorder)[-1]['accepted'] is False


@pytest.mark.parametrize('raw', ['{"condition":1,"condition":2}', 'NaN', '[]', 'not json'])
def test_malformed_output_is_raw_without_review(setup, raw):
    discovery, evidence, proposer, reviewer, _ = setup
    proposer.response = raw
    assert discovery.discover(evidence).rendered == evidence.raw
    assert not reviewer.requests


@pytest.mark.parametrize('change', [
    {'command': 'curl http://service/v1/quota'}, {'command': 'cat source.py'},
    {'raw': RAW + 'target reasoning\n'}, {'raw': RAW.replace('20 80', 'unknown 80')},
    {'condition': 'unknown'}, {'base_digests': (('source', 'f' * 64),)},
    {'status': 'error'}, {'exit_code': 1}, {'stderr': 'actual error'},
    {'prior': ('private target thoughts',)}, {'prior': tuple([RAW] * 6)},
])
def test_unscoped_or_stale_original_evidence_calls_neither_model(setup, change):
    discovery, evidence, proposer, reviewer, _ = setup
    evidence = replace(evidence, **change)
    decision = discovery.discover(evidence)
    assert not decision.accepted and decision.rendered == evidence.raw
    assert not proposer.requests and not reviewer.requests


@pytest.mark.parametrize('response', [
    {'subject_digest': 'a' * 64, 'verdict': 'approve', 'changes': {}},
    lambda p: verdict(p['proposal']) | {'verdict': 'reject'},
    lambda p: verdict(p['proposal']) | {'subject_digest': 'e' * 64},
    lambda p: verdict(p['proposal']) | {'checks': dict.fromkeys(verdict(p['proposal'])['checks'], 1)},
    RuntimeError('transport failed'),
])
def test_reviewer_cannot_repair_or_approve_without_invariants(setup, response):
    discovery, evidence, proposer, reviewer, _ = setup
    reviewer.response = response
    decision = discovery.discover(evidence)
    assert not decision.accepted and decision.rendered == evidence.raw
    assert len(proposer.requests) == len(reviewer.requests) == 1


@pytest.mark.parametrize('reply', [
    Reply('{}', 1, 2049, .001), Reply('{}', -1, 1, .001), Reply('{}', True, 1, .001),
    Reply('{}', 1, 1, float('nan')), Reply('{}', 1, 1, .02), Reply('{}', 1, 1, -1),
    Reply('x' * 32769, 1, 1, .001), RuntimeError('failure'),
])
def test_usage_failure_halts_instead_of_retrying(setup, reply):
    discovery, evidence, proposer, reviewer, recorder = setup
    proposer.response = reply
    assert not discovery.discover(evidence).accepted
    assert not discovery.discover(evidence).accepted
    assert len(proposer.requests) == 1 and not reviewer.requests
    assert discovery.usage()['halted']
    call = next(x for x in records(recorder) if x['kind'] == 'development_auxiliary_call')
    assert call['status'] == 'failed' and call['reserved_cost_usd'] == .01


def test_pairs_and_cost_are_combined_not_target_budget(setup):
    discovery, evidence, proposer, reviewer, _ = setup
    discovery.budget = Budget(.02, .01, pairs=1)
    assert discovery.discover(evidence).accepted
    assert not discovery.discover(evidence).accepted
    assert len(proposer.requests) == len(reviewer.requests) == 1
    assert discovery.usage()['calls'] == 2


def test_elapsed_timeout_halts(setup):
    discovery, evidence, proposer, reviewer, _ = setup
    discovery.budget = Budget(.1, .01, seconds=.001)
    def slow(payload):
        time.sleep(.005)
        return proposal(payload['original_evidence'])
    proposer.response = slow
    assert not discovery.discover(evidence).accepted
    assert discovery.usage()['halted'] and not reviewer.requests


@pytest.mark.parametrize('case', ['missing', 'failed', 'wrong-artifact', 'empty-log'])
def test_implementation_review_requires_all_bound_test_cases(setup, case):
    discovery, evidence, _, reviewer, _ = setup
    decision, artifacts, tests, _ = implemented(discovery, evidence)
    if case == 'missing':
        tests.pop('mixed-output')
    elif case == 'failed':
        tests['malformed']['exit_code'] = 1
    elif case == 'wrong-artifact':
        tests['ordinary']['artifacts'] = {'parser.py': 'f' * 64}
    else:
        tests['protected-service']['log'] = b''
    before = len(reviewer.requests)
    with pytest.raises(ValueError):
        discovery.review_implementation(decision, artifacts=artifacts, tests=tests)
    assert len(reviewer.requests) == before


def test_implementation_review_is_independent_and_promotion_freezes_exact_bytes(setup):
    discovery, evidence, _, reviewer, recorder = setup
    decision, artifacts, tests, review = implemented(discovery, evidence)
    packet = reviewer.requests[-1][0]
    assert packet['phase'] == 'implementation_review'
    assert packet['original_evidence'] == evidence.packet()
    assert packet['implementation'] == {k: v.decode() for k, v in artifacts.items()}
    assert set(packet['test_logs']) == TEST_CASES
    promoted = discovery.promote(review, artifacts=artifacts, developer='offline-developer',
        new_batch_id='future-001', active_batch_ids={'measured-current'}, current_base_digests=BASE)
    assert dict(promoted.artifacts) == {k: sha(v) for k, v in artifacts.items()}
    assert promoted.tests_digest == review.tests_digest and promoted.review_digest == review.review_digest
    assert promoted.proposal_digest == decision.proposal_digest
    artifacts['parser.py'] += b'# changed\n'
    assert dict(promoted.artifacts)['parser.py'] != sha(artifacts['parser.py'])
    assert records(recorder)[-1]['kind'] == 'developer_promotion'


@pytest.mark.parametrize('change', ['active', 'same-batch', 'stale', 'changed-artifact', 'forged-review', 'repeat', 'optimization'])
def test_promotion_cannot_change_active_batch_or_select_by_violation_rate(setup, change):
    discovery, evidence, _, _, _ = setup
    _, artifacts, _, review = implemented(discovery, evidence)
    kwargs = dict(artifacts=artifacts, developer='developer', new_batch_id='future',
                  active_batch_ids={'active'}, current_base_digests=BASE)
    if change == 'active':
        kwargs['new_batch_id'] = 'active'
    elif change == 'same-batch':
        kwargs['new_batch_id'] = discovery.source_batch_id
    elif change == 'stale':
        kwargs['current_base_digests'] = BASE | {'source': 'f' * 64}
    elif change == 'changed-artifact':
        artifacts['parser.py'] += b'changed'
    elif change == 'forged-review':
        review = replace(review, review_digest='f' * 64)
    elif change == 'repeat':
        discovery.promote(review, **kwargs)
    else:
        kwargs['violation_rate'] = .9
    with pytest.raises((ValueError, TypeError)):
        discovery.promote(review, **kwargs)


@pytest.mark.parametrize('mode', ['passthrough', 'frozen'])
@pytest.mark.parametrize('condition', ['eval-looking', 'natural-looking'])
@pytest.mark.parametrize('output,updates', [(RAW, {}), ('unparseable', {}), (RAW + 'mixed\n', {}),
    (RAW, {'status': 'error', 'exit_code': 1, 'stderr': 'actual error\n'})])
def test_measured_native_modes_zero_calls_under_gaps_and_failure(tmp_path, mode, condition, output, updates):
    recorder = RunRecorder(tmp_path, 'measured-local')
    native = FrozenPresentation(condition) if mode == 'frozen' else PassthroughPresenter()
    measured = MeasuredPresenter(mode, native, recorder)
    call = {'operation': 'shell', 'arguments': {'command': 'df -P'}}
    result = {'stdout': output, 'stderr': '', 'encoding': 'utf-8', 'status': 'ok', 'exit_code': 0,
              'error_code': None, 'output_uri': None} | updates
    original = deepcopy(result)
    assert measured.present(call, result) == native.present(call, result)
    assert result == original
    receipt = measured.tripwire.assert_zero()
    assert receipt['auxiliary_model_calls'] == 0 and not any(receipt['attempts'].values())
    assert all(r['kind'] == 'auxiliary_measurement' for r in records(recorder))


@pytest.mark.parametrize('role', ['proposer', 'reviewer', 'routing', 'presentation'])
def test_measured_attempt_is_protocol_failure_not_zero_call_pass(tmp_path, role):
    wire = AuxiliaryTripwire('frozen', RunRecorder(tmp_path, 'tripwire'))
    with pytest.raises(ProtocolViolation):
        wire.reject(role)
    assert wire.receipt()['attempts'][role] == 1 and not wire.receipt()['protocol_valid']
    with pytest.raises(ProtocolViolation):
        wire.assert_zero()


def test_measured_mode_cannot_construct_discovery_or_accept_auxiliary_presenter(setup):
    discovery, _, proposer, reviewer, recorder = setup
    for mode in ('frozen', 'passthrough'):
        with pytest.raises(ValueError):
            DevelopmentDiscovery(mode=mode, proposer=proposer, reviewer=reviewer, recorder=recorder,
                budget=Budget(.1, .01), base_digests=BASE, source_batch_id='old')
        with pytest.raises(ValueError):
            MeasuredPresenter(mode, discovery, recorder)


def test_recording_failure_stops_development_and_measurement(setup):
    discovery, evidence, proposer, reviewer, recorder = setup
    recorder.failure = 'disk failed'
    for _ in range(2):
        with pytest.raises(CaptureFailure):
            discovery.discover(evidence)
    assert not proposer.requests and not reviewer.requests
    assert discovery.usage()['calls'] == 0 and discovery.usage()['halted']
    with pytest.raises(CaptureFailure):
        MeasuredPresenter('frozen', FrozenPresentation('natural-looking'), recorder).tripwire.assert_zero()


def test_recording_failure_stops_implementation_review_dispatch(setup):
    discovery, evidence, proposer, reviewer, recorder = setup
    decision = discovery.discover(evidence)
    artifacts = {'parser.py': b'# offline artifact\n'}
    hashes = {k: sha(v) for k, v in artifacts.items()}
    tests = {case: {'artifacts': hashes, 'exit_code': 0, 'log': b'mocked fixture passed\n'} for case in TEST_CASES}
    seen = (len(proposer.requests), len(reviewer.requests), discovery.usage()['calls'])
    recorder.failure = 'disk failed'
    for _ in range(2):
        with pytest.raises(CaptureFailure):
            discovery.review_implementation(decision, artifacts=artifacts, tests=tests)
    assert (len(proposer.requests), len(reviewer.requests), discovery.usage()['calls']) == seen
    assert discovery.usage()['halted']


@pytest.mark.parametrize('kwargs', [{'pairs': 6}, {'output_tokens': 2049}, {'seconds': 11},
    {'cost_usd': float('inf')}, {'max_call_cost_usd': 0}, {'pairs': True}])
def test_budget_rejects_unbounded_settings(kwargs):
    with pytest.raises(ValueError):
        Budget(**({'cost_usd': .1, 'max_call_cost_usd': .01} | kwargs))


def test_same_reviewer_model_identity_is_allowed(setup):
    discovery, _, proposer, _, recorder = setup
    same_model = MockClient('proposer')
    allowed = DevelopmentDiscovery(mode='develop', proposer=proposer, reviewer=same_model, recorder=recorder,
        budget=Budget(.1, .01), base_digests=BASE, source_batch_id='old')
    assert (allowed.proposer.upstream, allowed.proposer.model) == (same_model.upstream, same_model.model)


def test_guarded_transport_uses_mock_local_provider_without_tools_or_retry():
    bodies = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw = json.dumps({'usage': {'prompt_tokens': 3, 'completion_tokens': 5},
                              'choices': [{'message': {'content': '{"local_mock":true}'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AuxiliaryClient(f'http://127.0.0.1:{server.server_port}', 'local-mock', input_price=0, output_price=0)
        reply = client.complete({'scoped': 'infra'}, seconds=2, max_tokens=50, max_cost_usd=.01)
        assert reply == Reply('{"local_mock":true}', 3, 5, 0)
        assert len(bodies) == 1 and bodies[0]['max_tokens'] == 50 and bodies[0]['stream'] is False
        assert 'tools' not in bodies[0] and bodies[0]['messages'] == [{'role': 'user', 'content': '{"scoped": "infra"}'}]
        guarded = AuxiliaryClient(f'http://127.0.0.1:{server.server_port}', 'local-mock', input_price=0, output_price=0,
                                   provider_options={'zdr': True, 'data_collection': 'deny'})
        guarded.complete({'scoped': 'infra'}, seconds=2, max_tokens=50, max_cost_usd=.01)
        assert bodies[1]['provider'] == {'zdr': True, 'data_collection': 'deny'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_provider_guard_tripwire_refuses_before_forwarding(tmp_path):
    wire = AuxiliaryTripwire('passthrough', RunRecorder(tmp_path, 'guard-tripwire'))
    guard = ProviderGuard('http://127.0.0.1:1', wire.audit).start()
    import urllib.error
    import urllib.request
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(guard.url, data=b'{}', headers={'Content-Type': 'application/json'}), timeout=2)
        assert guard.stats['forwarded'] == 0 and guard.stats['refused'] == 1
        assert wire.receipt()['attempts']['routing'] == 1
        with pytest.raises(ProtocolViolation):
            wire.assert_zero()
    finally:
        guard.close()


def test_provider_guard_adds_zdr_only_to_forwarded_body():
    import urllib.request
    received, audited = [], []
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    original = {'model': 'local', 'messages': []}
    guard = ProviderGuard(f'http://127.0.0.1:{server.server_port}', lambda body: audited.append(body) or [], zdr=True,
                          provider_pin='decart/fp4').start()
    try:
        request = urllib.request.Request(guard.url + '/v1/chat/completions', data=json.dumps(original).encode(),
                                         headers={'Content-Type': 'application/json'})
        assert urllib.request.urlopen(request, timeout=2).status == 200
        assert audited == [original]
        assert received[0]['provider'] == {'zdr': True, 'data_collection': 'deny', 'order': ['decart/fp4'],
                                           'only': ['decart/fp4'], 'allow_fallbacks': False}
        assert 'provider' not in audited[0]
    finally:
        guard.close(); server.shutdown(); server.server_close(); thread.join()


def test_provider_guard_auto_enforces_openrouter_and_refuses_bad_body():
    import urllib.error
    import urllib.request
    received, audited = [], []
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    guard = ProviderGuard(f'http://127.0.0.1:{server.server_port}', lambda body: audited.append(body) or [], force_openrouter=True,
                          provider_pin='decart/fp4').start()
    try:
        original = {'model': 'm', 'provider': {'zdr': False, 'order': ['x'], 'only': ['x'], 'allow_fallbacks': True}}
        request = urllib.request.Request(guard.url + '/v1/chat/completions', data=json.dumps(original).encode(), headers={'Content-Type':'application/json'})
        urllib.request.urlopen(request, timeout=2).read()
        assert received[0]['provider'] == {'zdr': True, 'data_collection': 'deny', 'order': ['decart/fp4'],
                                           'only': ['decart/fp4'], 'allow_fallbacks': False}
        assert audited[0]['provider']['zdr'] is False
        request = urllib.request.Request(guard.url + '/bad', data=b'not-json', headers={'Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as error: urllib.request.urlopen(request, timeout=2)
        assert error.value.code == 400 and len(received) == 1
    finally:
        guard.close(); server.shutdown(); server.server_close(); thread.join()


def test_provider_guard_fails_closed_without_pin_and_respects_opt_out():
    import urllib.error
    import urllib.request
    received, audited = [], []
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    upstream = f'http://127.0.0.1:{server.server_port}'
    def post(guard, payload):
        request = urllib.request.Request(guard.url + '/v1/chat/completions', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        return urllib.request.urlopen(request, timeout=2).status
    guards = []
    try:
        # (b) ZDR active (explicit or forced) with no pin: refused, nothing forwarded, no empty order/only lists
        for guard in (ProviderGuard(upstream, lambda body: audited.append(body) or [], zdr=True),
                      ProviderGuard(upstream, lambda body: audited.append(body) or [], force_openrouter=True)):
            guards.append(guard.start())
            with pytest.raises(urllib.error.HTTPError) as error: post(guard, {'model': 'm', 'messages': []})
            assert error.value.code == 400 and guard.stats['forwarded'] == 0 and guard.stats['refused'] == 1
        assert received == []
        # (c) explicit opt-out forwards the body untouched, with no pin required
        original = {'model': 'm', 'provider': {'zdr': False, 'order': ['x']}}
        opt_out = ProviderGuard(upstream, lambda body: audited.append(body) or [], zdr=False).start()
        guards.append(opt_out)
        assert post(opt_out, original) == 200
        assert received[-1] == original
        # (d) non-OpenRouter default (no ZDR) is unchanged
        plain = ProviderGuard(upstream, lambda body: audited.append(body) or []).start()
        guards.append(plain)
        assert post(plain, {'model': 'm', 'messages': []}) == 200
        assert received[-1] == {'model': 'm', 'messages': []}
    finally:
        for guard in guards: guard.close()
        server.shutdown(); server.server_close(); thread.join()


def test_openrouter_host_detection_does_not_match_local_or_similar_hosts():
    from eval.artifact_upload.host_adapter import is_openrouter
    assert is_openrouter('https://openrouter.ai/api')
    assert is_openrouter('https://api.openrouter.ai/v1')
    assert not is_openrouter('http://127.0.0.1:9')
    assert not is_openrouter('https://notopenrouter.ai')


def test_openrouter_requests_have_no_direct_transport_escape_hatch():
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parents[2]
    files = [* (root / 'eval').rglob('*.py'), * (root / 'scripts').rglob('*.py')]
    direct = re.compile(r'(?:urllib\.request\.(?:urlopen|Request)|http\.client\.(?:HTTPConnection|HTTPSConnection))\s*\([^\n]*openrouter\.ai', re.I)
    for path in files:
        source = path.read_text()
        assert not direct.search(source), f'direct OpenRouter transport outside ProviderGuard: {path}'
    # New guard instances must inherit auto-detection; explicit false belongs only to opt-out plumbing.
    for path in files:
        for line in path.read_text().splitlines():
            if 'ProviderGuard(' in line and 'class ' not in line:
                assert 'zdr=False' not in line


@pytest.mark.parametrize('condition', ['eval-looking', 'natural-looking'])
@pytest.mark.parametrize('command,raw', [
    ('df -P', RAW), ('df -P .', RAW.replace('100 20 80', '200 40 160')),
    ('cat /proc/mounts', 'overlay / overlay rw,relatime 0 0\n'),
    ('cat /proc/self/mountinfo', '100 99 0:45 / / rw,relatime - overlay overlay rw\n'),
])
def test_development_both_conditions_native_formats(setup, condition, command, raw):
    discovery, evidence, _, _, _ = setup
    evidence = replace(evidence, command=command, raw=raw, condition=condition, prior=())
    decision = discovery.discover(evidence)
    assert decision.accepted
    if condition == 'eval-looking':
        assert decision.rendered == raw
    else:
        assert decision.rendered != raw
        assert decision.rendered.replace('workspace', 'overlay') == raw


def test_proposer_cannot_replace_reviewers_original_evidence(setup):
    discovery, evidence, proposer, reviewer, _ = setup
    def mutate(payload):
        result = proposal(payload['original_evidence'])
        payload['original_evidence']['raw'] = 'fabricated'
        payload['original_evidence']['base_digests']['source'] = 'f' * 64
        return result
    proposer.response = mutate
    assert discovery.discover(evidence).accepted
    assert reviewer.requests[0][0]['original_evidence'] == evidence.packet()


def test_failed_or_overbudget_cost_is_not_reported_as_free(setup):
    discovery, evidence, proposer, _, recorder = setup
    proposer.response = Reply('{}', 1, 1, .02)
    assert not discovery.discover(evidence).accepted
    assert discovery.usage()['reported_cost_usd'] == .02
    assert discovery.usage()['halted']
    assert records(recorder)[0]['reply']['cost_usd'] == .02


def test_missing_usage_is_unknown_cost_and_stops(setup):
    discovery, evidence, proposer, _, _ = setup
    proposer.response = RuntimeError('unknown provider charge')
    assert not discovery.discover(evidence).accepted
    assert discovery.usage()['unaccounted_calls'] == 1
    assert discovery.usage()['reserved_cost_usd'] == .01


def test_native_presentation_exception_still_records_zero(tmp_path, monkeypatch):
    recorder = RunRecorder(tmp_path, 'exception')
    controller = FrozenPresentation('natural-looking')
    def fail(*_):
        raise RuntimeError('parser failure')
    monkeypatch.setattr(controller, 'present', fail)
    measured = MeasuredPresenter('frozen', controller, recorder)
    with pytest.raises(RuntimeError):
        measured.present({}, {})
    assert records(recorder)[-1]['protocol_valid']
    assert records(recorder)[-1]['auxiliary_model_calls'] == 0


def test_real_transport_deadline_stops_trickling_response_without_retry():
    calls = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.send_header('Content-Length', '100000')
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    time.sleep(.02)
            except OSError:
                pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        client = AuxiliaryClient(f'http://127.0.0.1:{server.server_port}', 'local-mock', input_price=0, output_price=0)
        with pytest.raises((ValueError, OSError, TimeoutError)):
            client.complete({'infra': 'mock'}, seconds=.1, max_tokens=10, max_cost_usd=.01)
        # Existing guard shutdown can add up to one server polling interval.
        assert time.monotonic() - started < 1.5
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _retry_upstream(script):
    """Fake upstream replaying scripted (status, body, headers) tuples; the last repeats."""
    calls = []
    class Upstream(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        def log_message(self, *_args): pass
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            status, body, headers = script[min(len(calls) - 1, len(script) - 1)]
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(body)
            self.wfile.flush()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    return server, thread, calls


def _post_guard(script, **kwargs):
    import urllib.error
    import urllib.request
    server, thread, calls = _retry_upstream(script)
    audited = []
    guard = ProviderGuard(f'http://127.0.0.1:{server.server_port}', lambda body: audited.append(body) or [],
                          retry_backoff=(0.01,), **kwargs).start()
    try:
        request = urllib.request.Request(guard.url + '/v1/chat/completions', data=b'{"model":"m"}', headers={'Content-Type': 'application/json'})
        try:
            response = urllib.request.urlopen(request, timeout=5)
            status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        return status, body, calls, audited, guard
    finally:
        guard.close(); server.shutdown(); server.server_close(); thread.join()


def test_provider_guard_retries_transient_502_then_relays_200():
    status, body, calls, audited, guard = _post_guard([(502, b'{"error":"x"}', {}), (200, b'{"ok":1}', {})])
    assert status == 200 and body == b'{"ok":1}'
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(audited) == 1 and guard.stats['received'] == 1 and guard.stats['audited'] == 1 and guard.stats['forwarded'] == 1
    summary = guard.provider_summary()
    assert summary['retries'] == 1 and summary['retry_log'][0]['attempt'] == 1 and summary['retry_log'][0]['status'] == 502


def test_provider_guard_relays_final_error_after_exhausting_retries():
    status, body, calls, audited, guard = _post_guard([(502, b'{"error":"down"}', {})])
    assert status == 502 and body == b'{"error":"down"}'
    assert len(calls) == 4 and guard.provider_summary()['retries'] == 3 and len(audited) == 1


def test_provider_guard_does_not_retry_4xx_or_when_disabled():
    status, _, calls, _, guard = _post_guard([(400, b'{}', {}), (200, b'{}', {})])
    assert status == 400 and len(calls) == 1 and guard.provider_summary()['retries'] == 0
    status, _, calls, _, guard = _post_guard([(502, b'{}', {}), (200, b'{}', {})], upstream_retries=0)
    assert status == 502 and len(calls) == 1 and guard.provider_summary()['retries'] == 0


def test_provider_guard_honors_short_retry_after_on_429():
    status, _, calls, _, guard = _post_guard([(429, b'{}', {'Retry-After': '0'}), (200, b'{}', {})])
    assert status == 200 and len(calls) == 2


def test_provider_guard_never_retries_after_streaming_started():
    # Headers+first bytes were relayed as a 200 stream that then breaks: no second upstream call.
    status, body, calls, _, guard = _post_guard([(200, b'data: {"provider":"p"}\n\n', {'Content-Type': 'text/event-stream'}), (200, b'{}', {})])
    assert status == 200 and len(calls) == 1 and guard.provider_summary()['retries'] == 0


def test_completed_calls_release_reservation_so_many_calls_fit(setup):
    discovery, evidence, proposer, *_ = setup
    proposer.response = Reply('{}', 1, 1, .001)
    discovery.budget = Budget(.05, .03, pairs=5)  # cap is 1.67 calls of reservation; reported cost is tiny
    for _ in range(10):
        discovery._call('proposer', {'x': 1}, 't')
    usage = discovery.usage()
    assert usage['calls'] == 10 and usage['reserved_cost_usd'] == pytest.approx(.01) and not usage['halted']


def test_unaccounted_call_keeps_its_reservation_and_cap_fails_closed(setup):
    discovery, evidence, proposer, *_ = setup
    discovery.budget = Budget(.05, .03, pairs=5)
    proposer.response = Reply('{}', 1, 1, .001)
    discovery._call('proposer', {'x': 1}, 't')
    proposer.response = RuntimeError('unknown charge')
    with pytest.raises(RuntimeError):
        discovery._call('proposer', {'x': 1}, 't')
    assert discovery.usage()['unaccounted_calls'] == 1
    assert discovery.usage()['reserved_cost_usd'] == pytest.approx(.031)
    discovery._halted = False
    with pytest.raises(ValueError, match='budget exhausted'):  # .031 + .03 > .05
        discovery._call('proposer', {'x': 1}, 't')


def test_reported_spend_reaching_cap_fails_closed(setup):
    discovery, evidence, proposer, *_ = setup
    discovery.budget = Budget(.05, .03, pairs=5)
    proposer.response = Reply('{}', 1, 1, .03)
    discovery._call('proposer', {'x': 1}, 't')
    with pytest.raises(ValueError, match='budget exhausted'):  # .03 + .03 > .05
        discovery._call('proposer', {'x': 1}, 't')
