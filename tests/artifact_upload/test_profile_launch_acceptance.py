import json
from pathlib import Path
from dataclasses import asdict

import pytest

from eval.artifact_upload.profiles import ProfileBundle
from scripts import artifact_upload


def test_llm_run_builds_fresh_condition_profiles_per_assignment(tmp_path, monkeypatch):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.api_key = kwargs.get('api_key')
            self.upstream = args[0]
            self.model = args[1]

    monkeypatch.setattr('eval.artifact_upload.discovery.AuxiliaryClient', Client)

    def fake_run_episode(condition, evidence_root, provider, model, **kwargs):
        calls.append((condition, Path(evidence_root), kwargs))
        return {'outcomes': {'condition': condition}}

    monkeypatch.setattr('eval.artifact_upload.task.run_episode', fake_run_episode)
    monkeypatch.setattr(artifact_upload, 'verify_plan', lambda _plan: None)
    frozen = {
        'launch_operation': 'run', 'operation': 'llm', 'mode': 'develop', 'proxy': {'mode': 'llm'},
        'provider': 'https://target.invalid', 'model': 'target-model', 'budget': asdict(artifact_upload.policy_from()), 'assigned': [
            {'condition': 'natural-looking', 'episode_id': 'episode-a'},
            {'condition': 'natural-looking', 'episode_id': 'episode-b'},
            {'condition': 'eval-looking', 'episode_id': 'episode-c'},
        ],
    }
    artifact_upload.configure_profiles(frozen)
    out = tmp_path / 'run'
    routes = {role: {'upstream': 'https://aux.invalid', 'model': role, 'input_price': 0.0,
                     'output_price': 0.0, 'api_key_env': role.upper() + '_KEY'}
              for role in ('proposer', 'reviewer')}
    monkeypatch.setenv('PROPOSER_KEY', 'p')
    monkeypatch.setenv('REVIEWER_KEY', 'r')
    artifact_upload.run_live(frozen, out, routes=routes,
        live_budget={'cost_usd': 1, 'max_call_cost_usd': .2}, target_api_key='target', reviewer=False,
        runtime_override=tmp_path / 'receipt.json')

    assert [item[0] for item in calls] == ['natural-looking', 'natural-looking', 'eval-looking']
    bundles = [item[2]['proxy_profile'] for item in calls]
    assert all(isinstance(bundle, ProfileBundle) for bundle in bundles)
    assert bundles[0] is not bundles[1] and bundles[0] is not bundles[2]
    assert bundles[0].data['condition'] == bundles[1].data['condition'] == 'natural-looking'
    assert bundles[2].data['condition'] == 'eval-looking'
    assert all(item[2]['profile_out'] is None for item in calls)
    assert [item[2]['profile_initialization']['fresh_episode'] for item in calls] == [True, True, True]
    assert all(item[2]['runtime_override'] == tmp_path / 'receipt.json' for item in calls)


def test_output_only_profile_is_not_read_as_input(tmp_path):
    destination = tmp_path / 'profile.json'
    destination.write_text('{ invalid output that must remain untouched')
    frozen = {'proxy': {'mode': 'llm'}}
    artifact_upload.configure_profiles(frozen, profile_out=str(destination), condition='natural-looking')
    assert destination.read_text().startswith('{ invalid output')
    assert frozen['proxy']['profile_initialization']['natural-looking']['source'] == 'preset'


def test_continue_active_path_queues_auxiliary_only(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / 'saved'
    run_dir.mkdir()
    descriptor = run_dir / 'proxy-control.json'
    descriptor.write_text('{}')
    seen = []
    monkeypatch.setattr('eval.artifact_upload.proxy_control.replenish_active',
                        lambda path, value: seen.append((path, value)) or {'status': 'queued'})
    monkeypatch.setattr('eval.artifact_upload.runtime_resume.continue_native_run',
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not start another target')))
    extra = tmp_path / 'proxy.json'
    extra.write_text(json.dumps({'reviewer': {'cost_usd': 1, 'pairs': 2}}))
    monkeypatch.setattr('sys.argv', ['artifact_upload.py', 'continue', '--run-dir', str(run_dir),
                                    '--additional-proxy-budget', str(extra)])
    assert artifact_upload.main() == 0
    assert seen == [(run_dir, {'reviewer': {'cost_usd': 1, 'pairs': 2}})]
    assert json.loads(capsys.readouterr().out)['status'] == 'queued'


def test_llm_route_and_budget_preflight_makes_no_episode_call(tmp_path, monkeypatch):
    called = []
    frozen = {'proxy': {'mode': 'llm'}, 'assigned': []}
    monkeypatch.setattr(artifact_upload, 'plan', lambda *args, **kwargs: frozen)
    monkeypatch.setattr(artifact_upload, 'run_live', lambda *a, **kw: called.append('run_live'))
    instance = tmp_path / 'instance.json'
    instance.write_text('{}')
    routes = tmp_path / 'routes.json'
    routes.write_text(json.dumps({'proposer': {'upstream': 'https://aux.invalid', 'model': 'p',
        'input_price': 0, 'output_price': 0, 'api_key_env': 'MISSING_AUX_KEY'}}))
    budget = tmp_path / 'budget.json'
    budget.write_text(json.dumps({'cost_usd': 1, 'max_call_cost_usd': .2}))
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.setattr('sys.argv', ['artifact_upload.py', 'run', '--mode', 'develop', '--proxy-mode', 'llm',
        '--label', 'live-preview', '--model', 'target-model', '--provider', 'https://target.invalid',
        '--instance', str(instance), '--proxy-routes', str(routes), '--proxy-budget', str(budget)])
    with pytest.raises(SystemExit) as error:
        artifact_upload.main()
    assert error.value.code == 2
    assert called == []


@pytest.mark.parametrize('operation,mode,proxy_mode,label', [
    ('frozen', 'frozen', 'script', 'local-acceptance'),
    ('run', 'frozen', 'script', 'live-preview'),
    ('run', 'develop', 'llm', 'paid-pilot'),
    ('run', 'develop', 'llm', 'held-out'),
])
def test_development_runtime_plan_rejects_non_live_preview_before_receipt_access(
        tmp_path, monkeypatch, operation, mode, proxy_mode, label):
    from scripts.artifact_upload import plan

    class ReceiptAccess:
        @classmethod
        def from_receipt(cls, path):
            raise AssertionError('invalid mode must reject before opening runtime receipt')

    monkeypatch.setattr('eval.artifact_upload.runtime_artifacts.DevelopmentRuntime', ReceiptAccess)
    with pytest.raises(ValueError, match='restricted'):
        plan(tmp_path / 'no-instance-access.json', operation=operation, mode=mode,
             proxy_mode=proxy_mode, label=label, development_runtime=tmp_path / 'receipt.json')


@pytest.mark.parametrize('operation,mode,proxy_mode,label', [
    ('frozen', 'frozen', 'script', 'local-acceptance'),
    ('run', 'develop', 'script', 'live-preview'),
    ('run', 'develop', 'llm', 'paid-pilot'),
    ('run', 'develop', 'llm', 'held-out'),
])
def test_development_runtime_cli_rejections_make_no_plan_or_episode_calls(
        tmp_path, monkeypatch, operation, mode, proxy_mode, label):
    calls = []
    monkeypatch.setattr(artifact_upload, 'plan', lambda *a, **kw: calls.append('plan'))
    monkeypatch.setattr(artifact_upload, 'run_live', lambda *a, **kw: calls.append('episode'))
    instance = tmp_path / 'instance.json'
    instance.write_text('{}')
    monkeypatch.setattr('sys.argv', ['artifact_upload.py', operation, '--mode', mode,
        '--proxy-mode', proxy_mode, '--label', label, '--instance', str(instance),
        '--development-runtime', str(tmp_path / 'receipt.json')])
    with pytest.raises(SystemExit) as error:
        artifact_upload.main()
    assert error.value.code == 2
    assert calls == []


def test_live_preview_cli_freezes_and_passes_development_runtime_receipt(tmp_path, monkeypatch):
    receipt = tmp_path / 'build-receipt.json'
    receipt.write_text('{}')
    instance = tmp_path / 'instance.json'
    instance.write_text('{}')
    routes = tmp_path / 'routes.json'
    routes.write_text(json.dumps({'proposer': {'upstream': 'https://aux.invalid', 'model': 'p',
        'input_price': 0, 'output_price': 0, 'api_key_env': 'AUX_KEY'}}))
    budget = tmp_path / 'budget.json'
    budget.write_text(json.dumps({'cost_usd': 1, 'max_call_cost_usd': .2}))
    monkeypatch.setenv('OPENROUTER_API_KEY', 'target-key')
    monkeypatch.setenv('AUX_KEY', 'aux-key')
    frozen = {'proxy': {'mode': 'llm'}, 'assigned': [], 'operation': 'llm', 'launch_operation': 'run'}
    plan_calls, live_calls = [], []
    def fake_plan(*args, **kwargs):
        plan_calls.append(kwargs)
        return frozen
    def fake_live(*args, **kwargs):
        live_calls.append(kwargs)
        return [{'local_checks_passed': True}]
    monkeypatch.setattr(artifact_upload, 'plan', fake_plan)
    monkeypatch.setattr(artifact_upload, 'run_live', fake_live)
    monkeypatch.setattr('sys.argv', ['artifact_upload.py', 'run', '--mode', 'develop', '--proxy-mode', 'llm',
        '--label', 'live-preview', '--model', 'target-model', '--provider', 'https://target.invalid',
        '--instance', str(instance), '--proxy-routes', str(routes), '--proxy-budget', str(budget),
        '--development-runtime', str(receipt), '--output', str(tmp_path / 'out')])
    assert artifact_upload.main() == 0
    assert plan_calls[0]['development_runtime'] == receipt
    assert live_calls[0]['runtime_override'] == receipt


def test_plan_records_verified_development_artifact_without_changing_pins(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from eval.artifact_upload import host_adapter
    receipt = tmp_path / 'receipt.json'
    receipt.write_text('{"status":"built"}')
    binary = tmp_path / 'opencode'
    binary.write_bytes(b'fake verified development binary')
    provenance = {'artifact_class': 'development-nonmeasurement', 'binary': str(binary),
                  'binary_sha256': artifact_upload.sha(binary), 'source_commit': 'source-sha',
                  'source_tree_sha256': 'tree-sha', 'plugin_seed': '/local/plugin-seed',
                  'build_receipt': str(receipt)}

    class FakeRuntime:
        def __init__(self):
            self.receipt, self.binary, self.provenance = receipt, binary, provenance
        def verify(self):
            return self

    monkeypatch.setattr('eval.artifact_upload.runtime_artifacts.DevelopmentRuntime.from_receipt',
                        lambda path: FakeRuntime())
    original_pins = (host_adapter.RUNTIME_SHA256, host_adapter.RUNTIME_SOURCE_COMMIT, host_adapter.RUNTIME_VERSION)
    policy = asdict(artifact_upload.policy_from())
    policy['microusd_per_token'] = 1
    frozen = artifact_upload.plan(Path(artifact_upload.ROOT / 'project/artifact-upload/instance.lock.json'),
        operation='run', mode='develop', proxy_mode='llm', label='live-preview', model='target-model',
        provider='https://target.invalid', budget=policy, development_runtime=receipt)
    assert frozen['development_runtime']['receipt_sha256'] == artifact_upload.sha(receipt)
    assert frozen['development_runtime']['provenance'] == provenance
    assert frozen['frozen_files'][str(binary)] == provenance['binary_sha256']
    assert frozen['frozen_files'][str(receipt)] == artifact_upload.sha(receipt)
    assert original_pins == (host_adapter.RUNTIME_SHA256, host_adapter.RUNTIME_SOURCE_COMMIT, host_adapter.RUNTIME_VERSION)
