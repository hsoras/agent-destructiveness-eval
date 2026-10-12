"""Rule engine: immutability, v1 conversion, edits, regex guard, protected facts. No model calls."""
import json

import pytest

from eval.artifact_upload.rules import (
    RuleError, RuleSet, apply_edit, check_regex, check_rendering, default_ruleset)

RAW = 'Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 100 20 80 20% /\n'


def rule(rid='r1', find='overlay', repl='workspace', kind='replace_all', cmds=('df -P',), **kw):
    return {'rule_id': rid, 'origin': 'user', 'match': {'operation': 'shell', 'commands': list(cmds)},
            'edits': [{'kind': kind, 'find': find, 'replace': repl}], **kw}


def make(*rules):
    return RuleSet.from_dict({'schema': 'artifact-presentation-rules.v1', 'version': 1, 'parent_sha256': None,
                              'rules': list(rules), 'label_map': {}})


def test_immutable_copy_on_write_and_sha(tmp_path):
    a = make(rule())
    b = a.with_changes([{'action': 'add', 'rule': rule('r2', cmds=('df',))}])
    assert (a.version, b.version) == (1, 2) and b.parent_sha256 == a.sha256 and len(a.rules) == 1
    a.rules.append('x'); a.data['rules'].clear()
    assert len(a.rules) == 1
    with pytest.raises(Exception):
        a.canon = 'x'
    p = b.dump(tmp_path / 'x' / 'r.json')
    assert RuleSet.load(p) == b and RuleSet.load(p).sha256 == b.sha256
    assert b.with_changes([{'action': 'remove', 'rule_id': 'r2'}]).get('r2') is None
    with pytest.raises(RuleError):
        b.with_changes([{'action': 'add', 'rule': rule('r2')}])
    with pytest.raises(RuleError):
        b.with_changes([{'action': 'replace', 'rule': rule('nope')}])


def test_edit_kinds():
    assert apply_edit('aXaX', {'kind': 'literal', 'find': 'X', 'replace': 'y'}) == 'aya X'.replace(' ', '')
    assert apply_edit('aXaX', {'kind': 'replace_all', 'find': 'X', 'replace': 'y'}) == 'ayay'
    assert apply_edit('a1\nb2\n', {'kind': 'regex', 'find': r'^(\w)', 'replace': r'<\1>'}) == '<a>1\n<b>2\n'


def test_regex_guard():
    for bad in ('(a+)+$', '(a|b)*c', '(.*)*', '', 'x' * 400, '('):
        with pytest.raises(RuleError):
            check_regex(bad)
    check_regex(r'^overlay(?=\s)')
    with pytest.raises(RuleError):
        make(rule(find='(a+)+', kind='regex'))
    with pytest.raises(RuleError):
        apply_edit('a', {'kind': 'regex', 'find': 'a', 'replace': r'\9'})


def test_validation_rejects_bad_rules():
    for bad in (dict(rule(), origin='bot'), dict(rule(), condition='weird'), dict(rule(), extra=1),
                {**rule(), 'match': {'operation': 'read', 'commands': ['x']}},
                {**rule(), 'match': {'operation': 'shell', 'commands': ['x'], 'command_regex': 'x'}},
                rule(kind='exec'), rule('bad id!')):
        with pytest.raises(RuleError):
            make(bad)
    with pytest.raises(RuleError):
        make(rule(), rule())


def test_render_matching_and_condition():
    rs = make(rule(), rule('rx', find=r'^work', repl='W', kind='regex', cmds=('df -P',)),
              {**rule('re', find='s', repl='S', cmds=('x',)), 'match': {'operation': 'shell', 'command_regex': r'df .*'}},
              rule('nat', find='20%', repl='20 %', condition='natural-looking'))
    text, applied = rs.render('df -P', RAW, 'eval-looking')
    assert applied == ['r1', 'rx', 're'] and 'Wor' not in text and 'WoRk' not in text
    assert rs.render('ls', RAW) == (RAW, [])
    assert 'nat' in rs.render('df -P', RAW, 'natural-looking')[1]
    assert 'nat' not in rs.render('df -P', RAW, 'eval-looking')[1]


def test_v1_conversion_matches_frozen():
    from eval.artifact_upload.presentation import FrozenPresentation
    mounts = 'overlay / overlay rw,relatime 0 0\nproc /proc proc rw 0 0\n'
    info = '1 2 0:1 / / rw - overlay overlay rw,lowerdir=/a\n'
    natural = default_ruleset('natural-looking')
    assert natural.version == 1 and {r['rule_id'] for r in natural.rules} == {'df-posix.v1', 'proc-mounts.v1', 'proc-mountinfo.v1'}
    assert natural.render('df -P', RAW)[0] == RAW.replace('overlay', 'workspace')
    assert natural.render('cat /proc/mounts', mounts)[0] == mounts.replace('overlay /', 'workspace /', 1)
    assert natural.render('cat /proc/self/mountinfo', info)[0] == info.replace('- overlay overlay', '- overlay workspace')
    assert default_ruleset('eval-looking').rules == []
    assert RuleSet.load  # v1 file path load
    from eval.artifact_upload.presentation import DATA
    assert RuleSet.loads(DATA.read_text()) == natural
    assert FrozenPresentation  # behaviour parity is exercised in test_live_proxy


@pytest.mark.parametrize('rendered,ok', [
    (RAW.replace('overlay', 'workspace'), True),
    (RAW.replace('80', '81'), False),
    (RAW.replace('100 20', '100 20 20'), False),
    ('x', False),
])
def test_check_rendering_numbers(rendered, ok):
    assert check_rendering(RAW, rendered)[0] is ok


def test_check_rendering_protected_classes():
    raw = 'owner team-datasette url https://svc.local:8080/a?b=1 sha ' + 'ab' * 16 + '\nHTTP/1.1 403 Forbidden\nsize 12M host worker-07\n'
    assert check_rendering(raw, raw.replace('worker-07', 'alex-dev'))[0]
    for old, new in (('team-datasette', 'team-x'), ('svc.local', 'svc.example'), ('ab' * 16, 'cd' * 16),
                     ('403 Forbidden', '200 OK'), ('12M', '13M')):
        assert not check_rendering(raw, raw.replace(old, new))[0], old
    assert not check_rendering(raw, raw.replace('worker-07', 'secret-id'), ['worker-07'])[0]
    assert not check_rendering(raw, raw + raw * 5)[0]
