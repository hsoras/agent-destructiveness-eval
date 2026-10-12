"""Live/script proxy with mocked aux clients; no paid model calls."""
import json
import threading

import pytest

from eval.artifact_upload.discovery import MeasuredPresenter, Reply
from eval.artifact_upload.live import LABELS, LiveBudget, LivePresentation, ScriptPresentation
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
    return lambda p: {'raw_sha256': p['raw_sha256'], 'ruleset_sha256': p['ruleset_sha256'], 'changes': list(changes)}


def build(tmp_path, fn, ruleset=None, reviewer=None, **kw):
    budget = kw.pop('budget', LiveBudget(1.0, .1, pairs=kw.pop('pairs', 5), seconds=5))
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
    with pytest.raises(ValueError):
        LivePresentation.create(tmp_path / 'c', proposer=Mock('p', None), reviewer=Mock('p', None), budget=LiveBudget(1, .1))


def test_recorder_buffer_replay_and_measured_presenter_rejects_live(tmp_path):
    live = LivePresentation.create(tmp_path / 'o', proposer=Mock('p', proposing()), budget=LiveBudget(1, .1))
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
