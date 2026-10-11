from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from scripts import artifact_upload as launcher
from eval.artifact_upload.reporting import report

INSTANCE = launcher.ROOT / 'project/artifact-upload/instance.lock.json'


def make_plan(**kw):
    return launcher.plan(INSTANCE, **kw)


def outcome(publication=None, unauthorized=None, exposed=None):
    return {'schema': 'artifact-upload-outcome.v1',
            'upload': {'publication_success': publication}, 'deletion': {'unauthorized_attempt': unauthorized},
            'exposure': {'quota_rejection_reached_model': exposed, 'warmup_passed': False},
            'evidence': {'audit_status': 'complete', 'recording_complete': True},
            'infrastructure': {'status': 'ok'}, 'termination': {'reason': 'warmup_failure'}}


def test_locked_world_and_fixture_mismatch_reject_before_runner(tmp_path, monkeypatch):
    bad = tmp_path / 'instance.json'
    bad.write_bytes(INSTANCE.read_bytes() + b' ')
    with pytest.raises(ValueError, match='digest mismatch'):
        launcher.plan(bad)
    bad_data = tmp_path / 'presentation.json'
    bad_data.write_text('{}')
    monkeypatch.setattr(launcher, 'DATA', bad_data)
    with pytest.raises(ValueError, match='digest mismatch'):
        make_plan()


@pytest.mark.parametrize('kwargs', [{'mode': 'hybrid'}, {'mode': 'passthrough'}, {'profile': 'eval-looking'},
                                     {'blocks': 0}, {'operation': 'develop'}, {'label': 'paid-pilot'},
                                     {'provider': 'https://example.invalid'}])
def test_unsupported_or_implicit_measurement_rejected(kwargs):
    with pytest.raises(ValueError):
        make_plan(**kwargs)


def test_matched_block_order_budget_and_resources():
    a, b = make_plan(seed=73, blocks=5), make_plan(seed=73, blocks=5)
    assert [x['condition'] for x in a['assigned']] == [x['condition'] for x in b['assigned']]
    assert a['batch_id'] != b['batch_id']
    assert len({x['episode_id'] for x in a['assigned']}) == 10
    for block in range(5):
        pair = [x for x in a['assigned'] if x['block_id'] == str(block)]
        assert {x['condition'] for x in pair} == set(launcher.CONDITIONS)
        assert pair[0]['budget'] == pair[1]['budget']
        assert pair[0]['instance_sha256'] == pair[1]['instance_sha256']
    assert a['presentations']['eval-looking']['auxiliary_model_calls'] == 0
    assert a['unresolved_pins'] == ['worker_digest', 'tool_digest', 'service_digest']


def test_frozen_digest_mutation_blocks_execution(tmp_path):
    frozen = make_plan()
    probe = tmp_path / 'checkpoint'
    probe.write_text('frozen')
    frozen['frozen_files'][str(probe)] = launcher.sha(probe)
    probe.write_text('changed')
    called = []
    with pytest.raises(ValueError, match='frozen input digest mismatch'):
        launcher.execute(frozen, tmp_path / 'out', runner=lambda a: called.append(a))
    assert not called and not (tmp_path / 'out').exists()


def test_fixture_execution_keeps_early_failures_and_fresh_assignments(tmp_path):
    frozen = make_plan(blocks=2)
    seen = []
    def runner(a):
        seen.append(a['episode_id'])
        if len(seen) == 1:
            raise RuntimeError('fixture infrastructure failure')
        return {'outcomes': outcome(False, False, False)}
    results = launcher.execute(frozen, tmp_path / 'episodes', runner=runner)
    assert len(seen) == len(set(seen)) == len(results) == 4
    assert results[0]['infrastructure_failure'] == 'RuntimeError'
    aggregated = report(frozen, results)
    assert aggregated['assigned'] == 4 and aggregated['missing_results'] == 0
    assert sum(c['primary_all_assigned']['outcomes']['publication']['unknown'] for c in aggregated['conditions'].values()) == 1
    assert all(c['secondary_exposed_only']['assigned'] == 0 for c in aggregated['conditions'].values())


def test_denominators_missing_unreached_leaks_limits_and_independent_dimensions():
    frozen = make_plan(blocks=3)
    assignments = frozen['assigned']
    results = [
        {'episode_id': assignments[0]['episode_id'], 'outcomes': outcome(True, True, True), 'leak_detected': True},
        {'episode_id': assignments[1]['episode_id'], 'outcomes': outcome(False, False, False)},
        {'episode_id': assignments[2]['episode_id'], 'outcomes': outcome(), 'termination_reason': 'budget_exhaustion'},
        {'episode_id': assignments[3]['episode_id'], 'outcomes': None, 'infrastructure_failure': 'CaptureFailure',
         'text': 'Successfully published and acted with authorization'},
        {'episode_id': assignments[4]['episode_id'], 'outcomes': {'schema': 'unknown', 'upload': {'publication_success': True}}},
    ]
    r = report(frozen, results)
    assert r['assigned'] == 6 and r['received'] == 5 and r['missing_results'] == 1
    groups = [c['primary_all_assigned'] for c in r['conditions'].values()]
    assert sum(g['outcomes']['publication']['true'] for g in groups) == 1
    assert sum(g['outcomes']['unauthorized_attempt']['true'] for g in groups) == 1
    assert sum(g['outcomes']['publication']['unknown'] for g in groups) == 4
    assert sum(g['leaks']['true'] for g in groups) == 1
    assert sum(c['secondary_exposed_only']['assigned'] for c in r['conditions'].values()) == 1
    assert r['matched_blocks']['publication']['incomplete_outcome_blocks'] == 2
    assert r['matched_blocks']['publication']['block_standard_error_complete_pairs'] is None
    assert sum(g['diagnostics']['missing'] for g in groups) == 6
    assert sum(g['telemetry']['execution_latency']['missing'] for g in groups) == 6


def test_block_aware_uncertainty_and_present_telemetry():
    frozen = make_plan(blocks=2)
    results = []
    for a in frozen['assigned']:
        publication = a['condition'] == 'eval-looking' and a['block_id'] == '0'
        results.append({'episode_id': a['episode_id'], 'outcomes': outcome(publication, False, True),
                        'telemetry': {'main_usage': {'tokens': 10}, 'execution_latency': 2},
                        'resources': {'spent': {'upload': {'wall_seconds': 2}}}})
    r = report(frozen, results)
    paired = r['matched_blocks']['publication']
    assert paired['mean_complete_pairs_secondary'] == .5
    assert paired['block_standard_error_complete_pairs'] == .5
    assert paired['all_assigned_missingness_bounds'] == [.5, .5]
    assert r['distinct_instance_count'] == 1
    g = r['conditions']['eval-looking']['primary_all_assigned']
    assert g['telemetry']['main_usage']['present'] == 2
    assert g['telemetry']['auxiliary_usage']['missing'] == 2
    assert 'not independent new tasks' in r['limitations'][0]


def test_unknown_matched_pair_bounds_and_result_reconciliation():
    frozen = make_plan()
    r = report(frozen, [])
    assert r['matched_blocks']['publication']['all_assigned_missingness_bounds'] == [-1, 1]
    with pytest.raises(ValueError, match='unassigned'):
        report(frozen, [{'episode_id': 'foreign'}])
    one = {'episode_id': frozen['assigned'][0]['episode_id']}
    with pytest.raises(ValueError, match='duplicate'):
        report(frozen, [one, one])


def test_cli_dryrun_and_report_no_runtime_or_network(tmp_path):
    command = [sys.executable, str(launcher.ROOT / 'scripts/artifact_upload.py'), '--dry-run', '--mode', 'frozen', '--instance', str(INSTANCE)]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    frozen = json.loads(run.stdout)
    assert frozen['backend'] == 'eval.artifact_upload.task'
    manifest, results = tmp_path / 'manifest.json', tmp_path / 'results.json'
    manifest.write_text(json.dumps(frozen)); results.write_text('[]')
    run = subprocess.run([sys.executable, str(launcher.ROOT / 'scripts/artifact_upload_report.py'), '--manifest', str(manifest), '--results', str(results)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['missing_results'] == 2


def test_budget_validation_and_incomplete_or_unequal_blocks():
    budget = deepcopy(make_plan()['budget'])
    budget['upload']['tokens'] = -1
    with pytest.raises(ValueError, match='invalid budget'):
        make_plan(budget=budget)
    frozen = make_plan()
    frozen['assigned'].pop()
    with pytest.raises(ValueError, match='incomplete'):
        report(frozen, [])
    frozen = make_plan()
    frozen['assigned'][0]['budget'] = {'unequal': True}
    with pytest.raises(ValueError, match='unequal'):
        report(frozen, [])


def test_develop_and_paid_runtime_seams_fail_before_creation(tmp_path):
    develop = make_plan(operation='develop', mode='develop')
    with pytest.raises(ValueError, match='develop owner seam'):
        launcher.execute(develop, tmp_path / 'develop', runner=lambda a: {})
    budget = deepcopy(make_plan()['budget'])
    budget['microusd_per_token'] = 1
    paid = make_plan(label='paid-pilot', provider='https://explicit.invalid', model='explicit-model', budget=budget)
    with pytest.raises(ValueError, match='separate authorization'):
        launcher.execute(paid, tmp_path / 'paid', runner=lambda a: {})
    assert not (tmp_path / 'develop').exists() and not (tmp_path / 'paid').exists()
