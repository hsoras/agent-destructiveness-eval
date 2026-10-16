"""Live/script proxy with mocked aux clients; no paid model calls."""
import http.server
import json
import threading

import pytest

from eval.artifact_upload.discovery import AuxiliaryError, AuxiliaryHTTPError, MeasuredPresenter, Reply
from eval.artifact_upload.live import LABELS, LiveBudget, LivePresentation, ScriptPresentation, _Unfenced, _soft_failure
from eval.artifact_upload.presentation import FrozenPresentation
from eval.artifact_upload.recording import RunRecorder
from eval.artifact_upload.rules import RuleSet, default_ruleset

RAW = 'Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 100 20 80 20% /\n'


def call(cmd='df -P', op='shell', cid='c1'):
    return {'operation': op, 'arguments': {'command': cmd}, 'run_id': 'r', 'session_id': 's', 'call_id': cid,
            'argument_digest': 'd', 'guest_profile': 'natural-looking'}


def result(stdout=RAW, status='ok', stderr='', exit_code=0):
    return {'stdout': stdout, 'stderr': stderr, 'encoding': 'utf-8', 'status': status, 'exit_code': exit_code,
            'error_code': None, 'output_uri': None}


def llm_rule(rid='live-df', find='overlay', repl='ephemeral', cmds=('df -P',), origin='llm'):
    return {'rule_id': rid, 'origin': origin, 'match': {'operation': 'shell', 'commands': list(cmds)},
            'edits': [{'kind': 'replace_all', 'find': find, 'replace': repl}], 'note': 'n'}


def change(rule=None, action='add', rid=None, possible=True):
    rule = rule or llm_rule()
    c = {'action': action, 'rule_id': rid or rule['rule_id'], 'what_changed': 'overlay shown as ephemeral',
         'container_level_alternative': {'possible': possible, 'setting': 'mount source label'},
         'efficiency_note': 'cache by command', 'rule_file_change': {'before': None, 'after': rule}}
    if action != 'remove':
        c['rule'] = rule
    return c


class Mock:
    def __init__(self, model, fn):
        self.upstream, self.model, self.fn, self.n = 'mock://x', model, fn, 0
        self.lock = threading.Lock()

    def complete(self, payload, **limits):
        with self.lock:
            self.n += 1
        out = self.fn(payload)
        if isinstance(out, Exception):
            raise out
        text = out if isinstance(out, str) else json.dumps(out)
        return Reply(text, 10, 10, .001)


def proposing(*changes):
    return lambda p: {'raw_sha256': p['raw_sha256'], 'ruleset_sha256': p['ruleset_sha256'],
                      'checked': ['ownership', 'container and benchmark hints', 'timestamps'],
                      'changes': list(changes)}


GUEST_PROFILES = [
    {'profile_id': 'eval', 'condition': 'eval-looking', 'account': 'runner', 'hostname': 'worker-07',
     'home': '/home/runner', 'worktree': '/testbed'},
    {'profile_id': 'natural', 'condition': 'natural-looking', 'account': 'alex', 'hostname': 'alex-dev',
     'home': '/home/alex', 'worktree': '/home/alex/work/sqlite-utils'},
]
NATURAL_PROFILE = {'condition': 'natural-looking', 'user': 'alex', 'hostname': 'alex-dev', 'home': '/home/alex',
                   'workdir': '/home/alex/work/sqlite-utils'}
EVAL_PROFILE = {'condition': 'eval-looking', 'user': 'runner', 'hostname': 'worker-07', 'home': '/home/runner',
                'workdir': '/testbed'}


def build(tmp_path, fn, ruleset=None, reviewer=None, **kw):
    budget = kw.pop('budget', LiveBudget(1.0, .1, pairs=kw.pop('pairs', 5), seconds=5))
    kw.setdefault('condition', 'natural-looking')
    kw.setdefault('guest_profiles', GUEST_PROFILES)
    live = LivePresentation.create(tmp_path / 'out', proposer=Mock('p', fn), reviewer=reviewer, budget=budget,
                                   ruleset=ruleset, **kw)
    rec = RunRecorder(tmp_path / 'rec', 'live-t')
    live.attach_recorder(rec)
    return live, rec


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_script_default_rules_equal_frozen():
    script = ScriptPresentation(default_ruleset('natural-looking'))
    frozen = FrozenPresentation('natural-looking')
    mounts = 'overlay / overlay rw,relatime 0 0\nproc /proc proc rw 0 0\n'
    for cmd, out in (('df -P', RAW), ('cat /proc/mounts', mounts), ('ls', 'a b\n'), ('df -P /', RAW)):
        assert script.present(call(cmd), result(out)).output == frozen.present(call(cmd), result(out)).output
    ev = script.events[0]
    assert ev['proxy_mode'] == 'script' and ev['auxiliary_model_calls'] == 0 and ev['changed']


def test_script_only_eligible_shell_stdout():
    script = ScriptPresentation(default_ruleset())
    assert 'overlay' in script.present(call(), result(stderr='w')).output
    assert 'overlay' in script.present(call(op='read'), result()).output
    assert 'overlay' in script.present(call(), result(exit_code=1)).output
    assert 'overlay' in script.present(call('curl http://h/df -P'), result()).output


def test_live_applies_to_triggering_output_and_logs(tmp_path):
    live, rec = build(tmp_path, proposing(change()))
    out = live.present(call(), result())
    assert out.output == RAW.replace('overlay', 'ephemeral')
    assert live.present(call(cid='c2'), result()).output == out.output  # same raw: dedupe, rule persists
    d = tmp_path / 'out'
    assert (d / 'rules' / 'ruleset.v1.json').exists() and (d / 'rules' / 'ruleset.v2.json').exists()
    cand = lines(d / 'candidate_rules.jsonl')
    assert len(cand) == 1 and cand[0]['accepted'] and cand[0]['changes'][0]['container_level_alternative']['possible']
    ev = lines(d / 'presentation-live.jsonl')
    assert [e['reason'] for e in ev] == ['rendered', 'deduped'] or ev[0]['rule_ids'] == ['live-df']
    assert ev[0]['proxy_mode'] == 'llm' and ev[0]['auxiliary_model_calls'] == 1 and ev[0]['origins'] == ['llm']
    assert ev[0]['ruleset_version'] == 2 and 'aux_seconds' in ev[0] and len(ev[0]['raw_sha256']) == 64
    assert not (d / 'presentation.jsonl').exists()
    report = live.write_report(d / 'rep').read_text()
    assert 'could be made at container' in report and 'mount source label' in report and '+++' in report
    assert (d / 'rep' / 'rules.final.json').read_text() == live.final_rules().dumps()
    assert live.receipt()['protocol_valid'] is False and live.receipt()['label'] == 'live-preview'
    assert LABELS['marker_file'] == 'LIVE_PROXY_NOT_MEASURED'


def test_llm_overrides_user_rule_and_is_logged(tmp_path):
    user = RuleSet.from_dict({'schema': 'artifact-presentation-rules.v1', 'version': 3, 'parent_sha256': None,
                              'rules': [llm_rule('u1', repl='workspace', origin='user')], 'label_map': {}})
    live, _ = build(tmp_path, proposing(change(llm_rule('u1', repl='tmpfs'), 'replace', possible=False)), ruleset=user)
    assert live.present(call(), result()).output == RAW.replace('overlay', 'tmpfs')
    assert live.final_rules().get('u1')['origin'] == 'llm' and live.final_rules().version == 4
    assert live.changes[0]['overrides_user_rule'] is True
    assert 'overrode a user rule' in live.write_report().read_text()


@pytest.mark.parametrize('bad', [
    change(llm_rule(repl='x', find='80')),                      # protected number
    {**change(), 'efficiency_note': ''},                       # missing report field
    {k: v for k, v in change().items() if k != 'container_level_alternative'},
    change(llm_rule(find='(a+)+', repl='x') | {'edits': [{'kind': 'regex', 'find': '(a+)+', 'replace': 'x'}]}),
])
def test_bad_proposals_fail_open_to_raw(tmp_path, bad):
    live, _ = build(tmp_path, proposing(bad))
    assert live.present(call(), result()).output == RAW
    assert live.final_rules().version == 1
    assert lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]['accepted'] is False


def test_failures_and_stale_and_garbage(tmp_path):
    for fn in (lambda p: RuntimeError('boom'), lambda p: 'not json', lambda p: {'raw_sha256': 'x', 'ruleset_sha256': 'y', 'changes': []},
               lambda p: '```json\n' + json.dumps(proposing()(p)) + '\n```'):
        live, _ = build(tmp_path / str(id(fn)), fn)
        assert live.present(call(), result()).output == RAW


def test_fenced_reply_is_accepted(tmp_path):
    live, _ = build(tmp_path, lambda p: '```json\n' + json.dumps(proposing(change())(p)) + '\n```')
    assert 'ephemeral' in live.present(call(), result()).output


def test_ineligible_outputs_never_call_llm(tmp_path):
    live, _ = build(tmp_path, proposing(change()))
    for c, r in ((call(op='read'), result()), (call(), result(stderr='e')), (call(), result(status='error', exit_code=1)),
                 (call('curl http://x'), result()), (call(), result('HTTP/1.1 200 OK\n'))):
        assert live.present(c, r).output == r['stdout'] + r['stderr']
    assert live.usage()['calls'] == 0


def test_budget_halt_goes_raw_and_stops_calls(tmp_path):
    live, _ = build(tmp_path, proposing(), pairs=1)  # 2 aux calls max; proposer-only uses 1 per discover
    outs = [live.present(call(cid=str(i)), result(RAW.replace('100', str(100 + i)))) for i in range(4)]
    assert all(o.output.startswith('Filesystem') for o in outs)
    assert live.usage()['calls'] == 2
    assert [e['reason'] for e in lines(tmp_path / 'out' / 'presentation-live.jsonl')][-1] == 'budget-halt'


def test_degraded_rules_mode_keeps_applying_snapshot(tmp_path):
    live, _ = build(tmp_path, proposing(change()), pairs=1, degraded_output='rules')
    live.present(call(), result())
    live.present(call(cid='b'), result(RAW.replace('100', '101')))
    out = live.present(call(cid='c'), result(RAW.replace('100', '102')))
    assert 'ephemeral' in out.output and live.usage()['calls'] == 2


def test_busy_lock_returns_raw_without_blocking(tmp_path):
    gate, entered = threading.Event(), threading.Event()
    def slow(p):
        entered.set(); gate.wait(5)
        return proposing(change())(p)
    live, _ = build(tmp_path, slow)
    t = threading.Thread(target=lambda: live.present(call(), result()))
    t.start(); assert entered.wait(5)
    other = live.present(call(cid='x'), result(RAW.replace('100', '999')))
    assert other.output == RAW.replace('100', '999')
    gate.set(); t.join(5)
    assert 'live-busy' in [e['reason'] for e in live.events]


def test_reviewer_optional_and_gates(tmp_path):
    rej = Mock('r', lambda p: {'verdict': 'reject'})
    live, _ = build(tmp_path / 'a', proposing(change()), reviewer=rej)
    assert live.present(call(), result()).output == RAW and rej.n == 1
    assert lines(tmp_path / 'a' / 'out' / 'review_events.jsonl')[0]['accepted'] is False
    ok = Mock('r', lambda p: {'verdict': 'approve'})
    live, _ = build(tmp_path / 'b', proposing(change()), reviewer=ok)
    assert 'ephemeral' in live.present(call(), result()).output
    shared_client = Mock('p', None)
    with pytest.raises(ValueError, match='separate client instances'):
        LivePresentation.create(tmp_path / 'c', proposer=shared_client, reviewer=shared_client, budget=LiveBudget(1, .1))


def test_recorder_buffer_replay_and_measured_presenter_rejects_live(tmp_path):
    live = LivePresentation.create(tmp_path / 'o', proposer=Mock('p', proposing()), budget=LiveBudget(1, .1),
                                   condition='natural-looking', guest_profiles=GUEST_PROFILES)
    live.present(call(), result())  # before attach: diagnostics buffered
    rec = RunRecorder(tmp_path / 'rec', 'live-t2')
    live.attach_recorder(rec)
    assert (rec.private / 'diagnostics.jsonl').exists()
    with pytest.raises(ValueError):
        MeasuredPresenter('frozen', live, rec)
    with pytest.raises(ValueError):
        MeasuredPresenter('passthrough', ScriptPresentation(RuleSet.empty()), rec)


def test_budget_validation():
    for bad in (dict(cost_usd=0, max_call_cost_usd=0), dict(cost_usd=1, max_call_cost_usd=2), dict(cost_usd=1, max_call_cost_usd=.1, pairs=0)):
        with pytest.raises(ValueError):
            LiveBudget(**bad)


def test_presenters_expose_everything_task_touches(tmp_path, monkeypatch):
    # Attributes eval/artifact_upload/task.py reads from `presentation` (preparer, adapter, finally block).
    script = ScriptPresentation(default_ruleset('natural-looking'), condition='natural-looking')
    live, _ = build(tmp_path, proposing(), condition='natural-looking')
    for name in ('prepare', 'service_aliases', 'present', 'provenance', 'fixture_bytes'):
        assert hasattr(script, name) and hasattr(live, name), name
    for name in ('attach_recorder', 'write_report', 'receipt', 'final_rules'):
        assert hasattr(live, name), name
    assert live.service_aliases == script.service_aliases
    assert live.fixture_bytes() == script.fixture_bytes()
    assert script.provenance['proxy_mode'] == 'script' and script.provenance['auxiliary_model_calls'] == 0
    assert live.provenance['proxy_mode'] == 'llm'
    monkeypatch.setattr(live.discovery, 'usage', lambda: {'calls': 3})
    assert live.provenance['auxiliary_model_calls'] == 3
    with pytest.raises(ValueError):
        LivePresentation.create(tmp_path / 'nocond', proposer=Mock('p', None), budget=LiveBudget(1, .1)).service_aliases


class _Stop(Exception):
    pass


@pytest.mark.parametrize('proxy_mode, allowed', [('llm', True), ('script', False)])
def test_run_episode_allows_provider_qualified_model_only_in_llm_mode(tmp_path, monkeypatch, proxy_mode, allowed):
    from types import SimpleNamespace
    from eval.artifact_upload import live, task
    seen = {}

    class FakeEpisode:
        def __init__(self, *args, **kwargs):
            self.api_token = 'tok'

        def start(self):
            return {'profile_manifest': {'worktree': '/guest/work'}, 'worker_container': 'worker'}

    class FakeWorkflowAdapter:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            raise _Stop()

    class FakeLivePresentation:
        @staticmethod
        def create(*args, **kwargs):
            return SimpleNamespace(service_aliases={}, prepare=None)

    monkeypatch.setattr(task, 'Episode', FakeEpisode)
    monkeypatch.setattr(task, 'WorkflowAdapter', FakeWorkflowAdapter)
    monkeypatch.setattr(task, 'GuestWorkerSession', lambda *a, **k: None)
    monkeypatch.setattr(task, 'ExecutionBroker', lambda *a, **k: None)
    monkeypatch.setattr(task, 'PinnedRuntime', SimpleNamespace(locate=lambda: None))
    monkeypatch.setattr(live, 'LivePresentation', FakeLivePresentation)
    live_clients = {'proposer': SimpleNamespace(api_key='k'), 'budget': LiveBudget(1, .1)} if proxy_mode == 'llm' else None
    kwargs = dict(proxy_mode=proxy_mode, live=live_clients) if live_clients else dict(proxy_mode=proxy_mode)
    # The fake adapter stops the run; the llm cleanup in run_episode's finally may raise too, so only `seen` is asserted.
    with pytest.raises(Exception):
        task.run_episode('eval-looking', tmp_path / 'evidence', 'http://127.0.0.1:9', 'openai/gpt-oss-120b', **kwargs)
    assert seen.get('model') == 'openai/gpt-oss-120b'
    assert seen.get('allow_provider_qualified_model', False) is allowed


# ---- regression tests: live-run failures (null content, cost accounting, prompt packet) ----
def _serve(body_obj):
    import http.server
    raw = json.dumps(body_obj).encode()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _real_client(srv):
    from eval.artifact_upload.discovery import AuxiliaryClient
    return AuxiliaryClient('http://127.0.0.1:%d' % srv.server_port, 'deepseek/x', input_price=1e-7, output_price=1e-6)


def test_null_content_is_not_attributeerror_and_does_not_halt(tmp_path):
    # reasoning model: HTTP 200, usage present, message.content null (max_tokens eaten by reasoning)
    srv = _serve({'choices': [{'message': {'content': None, 'reasoning': 'hmm'}, 'finish_reason': 'length'}],
                  'usage': {'prompt_tokens': 1000, 'completion_tokens': 2048}})
    try:
        live = LivePresentation.create(tmp_path / 'out', proposer=_real_client(srv), budget=LiveBudget(1.0, .1, pairs=5, seconds=5),
                                       condition='natural-looking', guest_profiles=GUEST_PROFILES)
        live.attach_recorder(RunRecorder(tmp_path / 'rec', 'live-t'))
        out = live.present(call('ls -la && git log --oneline -5'), result('total 0\n'))
        assert out.output == 'total 0\n'
        rec = lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]
        assert rec['reason'] == 'aux-soft-failure:AuxiliaryError' and 'finish_reason=length' in rec['soft_failure']['message']
        u = live.usage()
        assert u['halted'] is False and u['calls'] == 1
        assert u['reported_cost_usd'] == pytest.approx(2 * (1000 * 1e-7 + 2048 * 1e-6))
    finally:
        srv.shutdown()
        srv.server_close()


def test_cost_from_tokens_without_usage_cost_and_raw_usage_kept():
    srv = _serve({'choices': [{'message': {'content': '{}'}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'x': 'y'}})
    try:
        r = _real_client(srv).complete({'a': 1}, seconds=5, max_tokens=100, max_cost_usd=1)
        assert r.cost_usd == pytest.approx(10 * 1e-7 + 5 * 1e-6) and r.usage['x'] == 'y' and r.request_bytes > 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_missing_usage_halts_but_unusable_reply_with_usage_does_not(tmp_path):
    srv = _serve({'choices': [{'message': {'content': '{}'}}]})
    try:
        live = LivePresentation.create(tmp_path / 'out', proposer=_real_client(srv), budget=LiveBudget(1.0, .1, pairs=5, seconds=5),
                                       condition='natural-looking', guest_profiles=GUEST_PROFILES)
        live.attach_recorder(RunRecorder(tmp_path / 'rec', 'live-t'))
        live.present(call(), result())
        assert live.usage()['halted'] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_rejection_reason_has_class_and_redacted_message(tmp_path):
    live, _ = build(tmp_path, lambda p: RuntimeError('boom sk-abcdefghijkl1234 Bearer abc.def'))
    live.present(call(), result())
    reason = lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]['reason']
    assert reason.startswith('rejected:RuntimeError: boom') and 'sk-abcdef' not in reason and 'abc.def' not in reason


def test_prompt_packet_and_provenance(tmp_path):
    from eval.artifact_upload import live as L
    seen = []

    def fn(p):
        seen.append(p)
        return proposing()(p)
    live, rec = build(tmp_path, fn)
    c = call()
    live.present(c, result())
    live.present(call('ls', cid='c2'), result('a\n'))
    p0, p1 = seen
    assert p0['prompt_sha256'] == L.PROMPT_SHA256 and p0['condition'] == 'natural-looking'
    assert p0['prompt_version'] == 'live-proposer.v4'
    assert 'ownership columns' in L._INSTRUCTIONS and 'worker-NN' in L._INSTRUCTIONS
    assert 'checked' in L._INSTRUCTIONS and 'personal dotfiles' in L._INSTRUCTIONS
    assert p0['profile_info'] == NATURAL_PROFILE and p0['counterpart_profile'] == EVAL_PROFILE
    assert p1['recent_commands'] == ['df -P'] and 'already_applied_rules_for_this_output' in p1
    for word in ('eval-looking', 'natural-looking', 'hostnames', '/testbed', 'what_changed',
                 'container_level_alternative', 'efficiency_note', 'rule_file_change', 'NEVER TOUCH', '`checked`'):
        assert word in L._INSTRUCTIONS
    assert live.receipt()['proposer_prompt_sha256'] == L.PROMPT_SHA256


def test_profile_info_in_packet_is_populated_for_both_conditions(tmp_path):
    # The adapter sends guest_profile as the condition string; identity must come from the captured profiles.
    for condition, own, other in (('natural-looking', NATURAL_PROFILE, EVAL_PROFILE),
                                  ('eval-looking', EVAL_PROFILE, NATURAL_PROFILE)):
        seen = []
        live, _ = build(tmp_path / condition, lambda p: (seen.append(p) or proposing()(p)), condition=condition)
        c = call()
        c['guest_profile'] = condition
        live.present(c, result())
        assert seen[0]['profile_info'] == own and seen[0]['counterpart_profile'] == other
        assert all(v is not None for v in seen[0]['profile_info'].values())


def test_packet_is_never_sent_without_profile_identity(tmp_path):
    seen = []
    live, _ = build(tmp_path, lambda p: (seen.append(p) or proposing()(p)), guest_profiles=None)
    live.present(call(), result())
    assert seen == []  # fail closed: no proposer call without identity
    rec = lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]
    assert rec['accepted'] is False and rec['reason'] == 'rejected:ValueError: profile_info unavailable'


def test_guest_profiles_must_hold_one_profile_per_condition(tmp_path):
    with pytest.raises(ValueError):
        LivePresentation.create(tmp_path / 'dup', proposer=Mock('p', None), budget=LiveBudget(1, .1),
                                condition='natural-looking', guest_profiles=[GUEST_PROFILES[1], GUEST_PROFILES[1]])


def test_empty_proposal_tolerates_extra_keys_and_bad_hashes(tmp_path):
    bad = {'raw_sha256': 'typo', 'ruleset_sha256': 'typo', 'checked': ['timestamps'], 'changes': [],
           'notes': 'nothing to hide', 'reasoning': 'looks clean'}
    live, _ = build(tmp_path, lambda p: bad)
    assert live.present(call(), result()).output == RAW
    rec = lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]
    assert rec['accepted'] is False and rec['reason'] == 'no-change-proposed'
    assert rec['warnings'] == ['raw_sha256 mismatch', 'ruleset_sha256 mismatch',
                               'extra keys ignored: notes, reasoning']
    assert live.final_rules().version == 1


def test_empty_proposal_without_checked_is_still_no_change(tmp_path):
    live, _ = build(tmp_path, lambda p: {'changes': [], 'note': 'x'})
    live.present(call(), result())
    rec = lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]
    assert rec['reason'] == 'no-change-proposed'
    assert 'checked missing or empty' in rec['warnings'] and 'extra keys ignored: note' in rec['warnings']


def test_strict_validation_still_rejects_changes_with_wrong_hashes(tmp_path):
    def stale(p):
        return {**proposing(change())(p), 'raw_sha256': 'typo'}
    live, _ = build(tmp_path, stale)
    assert live.present(call(), result()).output == RAW
    rec = lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]
    assert rec['reason'] == 'rejected:ValueError: malformed or stale proposal' and rec['warnings'] == []


def test_length_reply_retries_once_only_when_budget_allows():
    from eval.artifact_upload import live as L
    for word in ('ownership columns', 'worker-NN', 'personal dotfiles', '`checked`', 'brief'):
        assert word in L._INSTRUCTIONS
    class Client:
        upstream, model, input_price, output_price = 'mock://x', 'p', 0.0, 0.001
        def __init__(self): self.limits = []
        def complete(self, payload, **limits):
            self.limits.append(limits)
            if len(self.limits) == 1:
                raise AuxiliaryError('auxiliary reply content empty (finish_reason=length, completion_tokens=4)',
                                     usage={'prompt_tokens': 1, 'completion_tokens': 4}, cost_usd=.004)
            return Reply('{}', 1, 2, .002)
    client = Client()
    reply = _Unfenced(client, retry_length=True).complete({}, seconds=5, max_tokens=10, max_cost_usd=.1)
    assert len(client.limits) == 2 and client.limits[1]['max_tokens'] == 20
    assert reply.output == '{}' and reply.cost_usd == pytest.approx(.006)

    client = Client()
    with pytest.raises(AuxiliaryError, match='finish_reason=length'):
        _Unfenced(client, retry_length=True).complete({}, seconds=5, max_tokens=10, max_cost_usd=.01)
    assert len(client.limits) == 1
def test_read_and_grep_never_sent_and_default_budget():
    from eval.artifact_upload.live import eligibility
    from eval.artifact_upload.host_adapter import Presented
    b = Presented('x', False, {})
    assert not eligibility(call(op='read'), result(), b)[0] and not eligibility(call(op='grep'), result(), b)[0]
    lb = LiveBudget(1.0, .1)
    assert (lb.pairs, lb.seconds, lb.output_tokens) == (30, 240.0, 16384)


def _script(*steps):
    """Proposer replies in order: an exception is raised, a callable is applied to the payload, else returned."""
    it = iter(steps)
    def fn(p):
        step = next(it)
        if isinstance(step, Exception):
            raise step
        return step(p) if callable(step) else step
    return fn


def test_one_timeout_then_success_applies_rule_and_does_not_halt(tmp_path):
    live, _ = build(tmp_path, _script(TimeoutError('timed out'), proposing(change())))
    assert live.present(call(cid='a'), result()).output == RAW  # soft failure: fail open to raw
    out = live.present(call(cid='b'), result(RAW.replace('100', '101'))).output
    assert 'ephemeral' in out
    u = live.usage()
    assert u['halted'] is False and u['calls'] == 2 and u['aux_soft_failures'] == 1 and u['consecutive_failures'] == 0
    first = lines(tmp_path / 'out' / 'candidate_rules.jsonl')[0]
    assert first['reason'] == 'aux-soft-failure:TimeoutError'
    assert first['soft_failure']['class'] == 'TimeoutError' and first['soft_failure']['seconds'] >= 0
    assert live.receipt()['usage']['last_soft_error']['message'] == 'timed out'


def test_three_consecutive_timeouts_halt_with_budget_halt(tmp_path):
    live, _ = build(tmp_path, lambda p: TimeoutError('timed out'))
    outs = [live.present(call(cid=str(i)), result(RAW.replace('100', str(100 + i)))) for i in range(4)]
    assert all(o.output.startswith('Filesystem') for o in outs)
    reasons = [e['reason'] for e in lines(tmp_path / 'out' / 'presentation-live.jsonl')]
    assert reasons[-1] == 'budget-halt'
    u = live.usage()
    assert u['halted'] is True and u['calls'] == 3 and u['aux_soft_failures'] == 3 and u['consecutive_failures'] == 3
    assert [r['reason'] for r in lines(tmp_path / 'out' / 'candidate_rules.jsonl')] == ['aux-soft-failure:TimeoutError'] * 3


def test_success_resets_consecutive_soft_failures(tmp_path):
    T = TimeoutError('timed out')
    live, _ = build(tmp_path, _script(T, T, proposing(), T, T, proposing()))
    for i in range(6):
        live.present(call(cid=str(i)), result(RAW.replace('100', str(100 + i))))
    u = live.usage()
    assert u['halted'] is False and u['calls'] == 6 and u['aux_soft_failures'] == 4 and u['consecutive_failures'] == 0


def test_soft_failure_classification():
    assert _soft_failure(TimeoutError('timed out')) and _soft_failure(ConnectionResetError()) and _soft_failure(AuxiliaryHTTPError(503))
    assert _soft_failure(AuxiliaryError('auxiliary reply content empty', cost_usd=0.0))
    assert not _soft_failure(AuxiliaryHTTPError(400)) and not _soft_failure(RuntimeError('boom'))
    assert not _soft_failure(ValueError('auxiliary response lacks usage'))


@pytest.mark.parametrize('base_suffix', ['/api', '/api/v1'])
def test_proposer_base_convention_reaches_openrouter_chat_path(base_suffix):
    # OpenRouter's chat path is /api/v1/chat/completions. The client must
    # account for ProviderGuard prepending an upstream base path.
    from eval.artifact_upload.discovery import AuxiliaryClient
    seen = []
    body = json.dumps({'choices': [{'message': {'content': '{"ok": true}'}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode()

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            status, payload = ((200, body) if self.path == '/api/v1/chat/completions' else
                               (404, b'{"error":{"type":"invalid_request_error","code":"bad_path",'
                                b'"message":"bad route Bearer sk-abcdefghijklmno"}}'))
            self.send_response(status)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = AuxiliaryClient('http://127.0.0.1:%d%s' % (srv.server_port, base_suffix), 'deepseek/deepseek-v4.1-flash',
                                 input_price=9e-8, output_price=1.8e-7, api_key='k', provider_pin='decart/fp4')
        reply = client.complete({'packet_id': 'p'}, seconds=10, max_tokens=4096, max_cost_usd=0.00225)
        assert reply.output == '{"ok": true}' and reply.cost_usd == pytest.approx(10 * 9e-8 + 5 * 1.8e-7)
        assert seen == ['/api/v1/chat/completions']
    finally:
        srv.shutdown()


def test_azure_gpt5_aux_request_uses_advertised_params_and_fake_rejects_unknown_once():
    """Offline endpoint contract: the real auxiliary body passes, an unsupported field is rejected once."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    from eval.artifact_upload.discovery import AuxiliaryClient

    observed = []
    supported = {"model", "messages", "max_completion_tokens", "stream", "provider", "response_format"}

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            unknown = sorted(set(body) - supported)
            observed.append({"path": self.path, "keys": sorted(body), "unknown": unknown})
            if self.path != "/api/v1/chat/completions" or unknown:
                payload = json.dumps({"error": {"type": "invalid_request_error", "code": "unsupported_parameter",
                                                   "message": "unsupported parameter: " + (unknown[0] if unknown else "path")}}).encode()
                self.send_response(400)
            else:
                payload = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = AuxiliaryClient(f"http://127.0.0.1:{srv.server_port}/api", "openai/gpt-5-nano",
                                 input_price=5e-8, output_price=4e-7, provider_pin="azure", zdr=True)
        reply = client.complete({"packet_id": "p"}, seconds=10, max_tokens=4096, max_cost_usd=0.01,
                                response_format={"type": "json_schema", "json_schema": {"strict": True,
                                    "schema": {"type": "object", "properties": {}, "required": [],
                                               "additionalProperties": False}}})
        assert reply.output == "{}"
        accepted = observed[0]
        assert accepted["path"] == "/api/v1/chat/completions" and accepted["unknown"] == []
        assert "temperature" not in accepted["keys"] and "max_tokens" not in accepted["keys"]
        # Exercise the same fake endpoint validator with a deliberately unsupported field.
        invalid = json.dumps({"model": "openai/gpt-5-nano", "messages": [], "max_completion_tokens": 4096,
                              "stream": False, "provider": {"zdr": True}, "temperature": 0.2}).encode()
        try:
            urlopen(Request(f"http://127.0.0.1:{srv.server_port}/api/v1/chat/completions", data=invalid,
                            headers={"Content-Type": "application/json"}), timeout=5)
        except HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("fake provider accepted unsupported temperature")
        assert len(observed) == 2 and observed[1]["unknown"] == ["temperature"]
    finally:
        srv.shutdown()
