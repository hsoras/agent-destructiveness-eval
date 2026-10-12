from dataclasses import asdict
import json
import sys

import pytest
from scripts import artifact_upload as launcher

INSTANCE = launcher.ROOT / 'project/artifact-upload/instance.lock.json'
ROUTE_PIN = 'decart/fp4'
CLI_PIN = ['inference-net/fp4']


def live_plan():
    policy = {**asdict(launcher.policy_from()), 'microusd_per_token': 1}
    return launcher.plan(INSTANCE, operation='run', mode='develop', proxy_mode='llm', label='live-preview',
                         model='z-ai/glm-5.3-flash', provider='https://openrouter.ai/api', budget=policy, blocks=1)


def route(**kw):
    return {'upstream': 'https://openrouter.ai/api', 'model': 'deepseek/deepseek-v4.1-flash', 'input_price': 9e-8,
            'output_price': 1.8e-7, 'api_key_env': 'TEST_PROXY_KEY', 'provider_pin': ROUTE_PIN, **kw}


LIVE_BUDGET = {'cost_usd': 1.0, 'max_call_cost_usd': 0.1}


@pytest.fixture
def recorded(monkeypatch):
    calls = {'episodes': [], 'clients': []}

    class FakeClient:
        def __init__(self, upstream, model, **kw):
            calls['clients'].append(kw)

    def fake_run_episode(condition, evidence_root, base_url, model, **kw):
        calls['episodes'].append(kw)
        return {'condition': condition}

    monkeypatch.setenv('TEST_PROXY_KEY', 'proxy-key')
    monkeypatch.setattr('eval.artifact_upload.discovery.AuxiliaryClient', FakeClient)
    monkeypatch.setattr('eval.artifact_upload.task.run_episode', fake_run_episode)
    monkeypatch.setattr(launcher, 'finish', lambda result, episode_dir, failure=None: result)
    return calls


def test_run_live_passes_cli_provider_pin_to_run_episode(tmp_path, recorded):
    frozen = live_plan()
    routes = {'proposer': route()}
    results = launcher.run_live(frozen, tmp_path / 'out', routes=routes, live_budget=LIVE_BUDGET,
                                target_api_key='target-key', provider_pin=CLI_PIN)
    assert results and len(recorded['episodes']) == len(frozen['assigned']) >= 1
    assert all(ep['provider_pin'] == CLI_PIN for ep in recorded['episodes'])
    assert recorded['clients'][0]['provider_pin'] == ROUTE_PIN


def test_main_passes_openrouter_provider_to_run_live(tmp_path, monkeypatch):
    seen = {}

    def fake_run_live(frozen, output, **kw):
        seen.update(kw)
        return [{'condition': 'natural-looking'}]

    routes = tmp_path / 'routes.json'
    routes.write_text(json.dumps({'proposer': route()}))
    budget = tmp_path / 'proxy-budget.json'
    budget.write_text(json.dumps(LIVE_BUDGET))
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({**asdict(launcher.policy_from()), 'microusd_per_token': 1}))
    monkeypatch.setenv('TEST_PROXY_KEY', 'proxy-key')
    monkeypatch.setenv('TEST_TARGET_KEY', 'target-key')
    monkeypatch.setattr(launcher, 'run_live', fake_run_live)
    monkeypatch.setattr(sys, 'argv', ['artifact_upload.py', 'run', '--proxy-mode', 'llm', '--condition', 'natural-looking',
                                      '--instance', str(INSTANCE), '--mode', 'develop', '--label', 'live-preview',
                                      '--blocks', '1', '--provider', 'https://openrouter.ai/api',
                                      '--model', 'z-ai/glm-5.3-flash', '--openrouter-provider', CLI_PIN[0],
                                      '--target-api-key-env', 'TEST_TARGET_KEY', '--budget', str(policy),
                                      '--proxy-routes', str(routes), '--proxy-budget', str(budget),
                                      '--output', str(tmp_path / 'out')])
    assert launcher.main() == 0
    assert seen['provider_pin'] == CLI_PIN
    assert seen['target_api_key'] == 'target-key'
