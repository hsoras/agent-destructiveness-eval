import json
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from scripts import artifact_upload as launcher
from eval.artifact_upload import lifecycle, task
from eval.artifact_upload.workflow import Allowance, Ledger, Policy, TwoStageWorkflow

from tests.artifact_upload.test_workflow import WORLD, Adapter, turn
from tests.artifact_upload.test_run_live_wiring import INSTANCE, LIVE_BUDGET, live_plan, recorded, route  # noqa: F401


def flow(turns, scenario, policy=Policy()):
    a = Adapter(turns)
    ledger = Ledger(policy)
    w = TwoStageWorkflow(a, ledger, WORLD, lambda: pytest.fail('no repair checkpoint'), lambda: {'s': 1}, scenario=scenario)
    return a, ledger, w.run()


def test_default_scenario_unchanged():
    a = Adapter([turn(), turn('May I remove it?')])
    w = TwoStageWorkflow(a, Ledger(), WORLD, lambda: {'passed': True}, lambda: {})
    r = w.run()
    assert w.scenario == 'bug-and-dilemma' and r['scenario'] == 'bug-and-dilemma'
    assert [c[0] for c in a.calls] == [WORLD['task_prompts']['repair'], WORLD['task_prompts']['upload']]
    assert a.calls[1][1]['session_id'] == 'fresh' and r['warmup'] == {'passed': True}


def test_dilemma_only_skips_warmup_and_checkpoint():
    a, ledger, r = flow([turn('May I remove it?', sid='s1')], 'dilemma-only',
                        Policy(warmup=Allowance(0, 0, 0, 0)))
    assert [c[0] for c in a.calls] == [WORLD['task_prompts']['upload']]
    assert a.calls[0][1]['session_id'] is None  # first user message of a fresh session
    assert r['scenario'] == 'dilemma-only' and r['warmup'] == {'skipped': True} and r['upload_reached'] is True
    assert r['termination_reason'] == 'awaiting_user'
    assert 'warmup_before_upload' not in r['checkpoints']
    spent = r['resources']['spent']
    assert spent['warmup'] == dict(wall_seconds=0.0, tool_calls=0, tokens=0, cost_microusd=0)


def test_dilemma_only_infrastructure_failure_without_session():
    a, _, r = flow([turn(sid=None)], 'dilemma-only', Policy(warmup=Allowance(0, 0, 0, 0)))
    assert r['termination_reason'] == 'infrastructure_failure'


def test_ledger_zero_warmup_allows_upload_charges_and_rejects_warmup_charges():
    ledger = Ledger(Policy(warmup=Allowance(0, 0, 0, 0)))
    ledger.enter('upload')
    ledger.charge(tool_calls=1, tokens=10)
    ledger.enter('overhead')
    ledger.stage = 'warmup'
    with pytest.raises(Exception):
        ledger.charge(tool_calls=1)


def test_invalid_scenario_rejected():
    with pytest.raises(ValueError):
        TwoStageWorkflow(Adapter([]), Ledger(), WORLD, None, None, scenario='nope')
    with pytest.raises(ValueError):
        launcher.plan(INSTANCE, scenario='nope')
    with pytest.raises(ValueError):
        lifecycle.Episode('natural-looking', scenario='nope')


def test_plan_records_scenario_and_default():
    assert launcher.plan(INSTANCE)['scenario'] == 'bug-and-dilemma'
    assert 'scenario_note' not in launcher.plan(INSTANCE)
    frozen = launcher.plan(INSTANCE, scenario='dilemma-only')
    assert frozen['scenario'] == 'dilemma-only' and 'warmup is ignored' in frozen['scenario_note']


@pytest.mark.parametrize('kw', [{'operation': 'prepare'}, {'operation': 'control'}, {'operation': 'develop', 'mode': 'develop'},
                                {'label': 'paid-pilot', 'model': 'm', 'provider': 'http://x',
                                 'budget': {**asdict(launcher.policy_from()), 'microusd_per_token': 1}},
                                {'label': 'held-out', 'model': 'm', 'provider': 'http://x',
                                 'budget': {**asdict(launcher.policy_from()), 'microusd_per_token': 1}}])
def test_dilemma_only_launcher_refusals(kw):
    with pytest.raises(ValueError, match='dilemma-only'):
        launcher.plan(INSTANCE, scenario='dilemma-only', **kw)


def test_live_plan_dilemma_only_keeps_live_preview_rules():
    policy = {**asdict(launcher.policy_from()), 'microusd_per_token': 1}
    with pytest.raises(ValueError, match='live-preview'):
        launcher.plan(INSTANCE, operation='run', mode='develop', proxy_mode='llm', label='local-acceptance',
                      model='m', provider='http://x', budget=policy, scenario='dilemma-only')
    frozen = launcher.plan(INSTANCE, operation='run', mode='develop', proxy_mode='llm', label='live-preview',
                           model='m', provider='https://openrouter.ai/api', budget=policy, scenario='dilemma-only')
    assert frozen['label'] == 'live-preview' and frozen['scenario'] == 'dilemma-only'


def test_run_live_passes_scenario_to_run_episode(tmp_path, recorded):  # noqa: F811
    frozen = live_plan()
    frozen['scenario'] = 'dilemma-only'
    launcher.run_live(frozen, tmp_path / 'a', routes={'proposer': route()}, live_budget=LIVE_BUDGET, target_api_key='k')
    assert all(ep['scenario'] == 'dilemma-only' for ep in recorded['episodes'])
    recorded['episodes'].clear()
    launcher.run_live(live_plan(), tmp_path / 'b', routes={'proposer': route()}, live_budget=LIVE_BUDGET, target_api_key='k')
    assert all(ep['scenario'] == 'bug-and-dilemma' for ep in recorded['episodes'])


def test_main_scenario_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['artifact_upload.py', 'run', '--dry-run', '--instance', str(INSTANCE), '--scenario', 'dilemma-only'])
    assert launcher.main() == 0
    assert json.loads(capsys.readouterr().out)['scenario'] == 'dilemma-only'
    monkeypatch.setattr(sys, 'argv', ['artifact_upload.py', 'run', '--dry-run', '--instance', str(INSTANCE)])
    assert launcher.main() == 0
    assert json.loads(capsys.readouterr().out)['scenario'] == 'bug-and-dilemma'


def test_run_episode_refuses_dilemma_only_with_local_acceptance():
    with pytest.raises(ValueError, match='dilemma-only'):
        task.run_episode('natural-looking', '/nonexistent', 'http://127.0.0.1:1', 'test-canary', local_acceptance=True,
                         scenario='dilemma-only')


def test_scripted_provider_dilemma_only_has_no_repair_steps():
    full = task.local_provider()
    only = task.local_provider(scenario='dilemma-only')
    try:
        assert len(only.script) == len(full.script) - 4
        assert [s[1][0][0] for s in only.script if s[0] == 'tools'] == ['publish']
    finally:
        full.close(); only.close()


def test_reference_repair_is_the_scripted_repair_and_applies_to_prefix_source(tmp_path):
    import subprocess
    src = lifecycle.SOURCE
    if not (src / '.git').is_dir():
        pytest.skip('prepared pre-fix checkout not available')
    work = tmp_path / 'w'
    subprocess.run(['git', 'clone', '-q', str(src), str(work)], check=True)
    subprocess.run([sys.executable, '-c', lifecycle.REFERENCE_REPAIR], cwd=work, check=True)
    diff = subprocess.run(['git', 'diff', '--stat'], cwd=work, capture_output=True, text=True).stdout
    assert 'sqlite_utils/db.py' in diff and 'pyproject.toml' in diff
