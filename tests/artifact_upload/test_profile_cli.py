import json

import pytest

from eval.artifact_upload.profiles import ProfileBundle
from scripts.artifact_upload import configure_profiles, profile_paths


def launch(mode='script'):
    return {'proxy': {'mode': mode, 'rules_path': None}, 'frozen_files': {}}


def test_missing_input_created_and_output_not_loaded(tmp_path):
    initial = tmp_path / 'nested' / 'in.json'
    output = tmp_path / 'out.json'
    output.write_text('invalid output is never input')
    plan = launch()
    configure_profiles(plan, profile_in=str(initial), profile_out=str(output), condition='natural-looking')
    bundle = ProfileBundle.load(initial)
    assert plan['proxy']['profiles']['natural-looking']['sha256'] == bundle.sha256
    assert plan['proxy']['profile_initialization']['natural-looking']['source'] == 'initialized'
    assert output.read_text() == 'invalid output is never input'


def test_invalid_existing_input_is_error(tmp_path):
    initial = tmp_path / 'in.json'
    initial.write_text('{}')
    with pytest.raises(ValueError):
        configure_profiles(launch(), profile_in=str(initial), condition='natural-looking')
    assert initial.read_text() == '{}'


def test_both_requires_disjoint_mapping(tmp_path):
    with pytest.raises(ValueError, match='mapping'):
        profile_paths(str(tmp_path / 'profile.json'), ['natural-looking', 'eval-looking'], '--profile-in')
    with pytest.raises(ValueError, match='independent'):
        profile_paths({'natural-looking': str(tmp_path / 'same'), 'eval-looking': str(tmp_path / 'same')},
                      ['natural-looking', 'eval-looking'], '--profile-out')
    plan = launch('llm')
    configure_profiles(plan)
    personal = plan['proxy']['profiles']['natural-looking']['data']
    evaluation = plan['proxy']['profiles']['eval-looking']['data']
    personal['desired_identity']['hostname'] = 'changed'
    assert evaluation['desired_identity']['hostname'] != 'changed'


def test_rules_and_profile_input_conflict():
    plan = launch()
    plan['proxy']['rules_path'] = 'rules.json'
    with pytest.raises(ValueError, match='mutually exclusive'):
        configure_profiles(plan, profile_in='profile.json', condition='natural-looking')


def test_script_default_does_not_opt_into_bundle():
    plan = launch()
    configure_profiles(plan)
    assert 'profiles' not in plan['proxy']


def test_legacy_rules_import_filters_each_condition(tmp_path):
    from eval.artifact_upload.rules import default_ruleset
    rules = tmp_path / 'rules.json'
    original = default_ruleset('natural-looking')
    rules.write_text(original.dumps())
    plan = launch('llm')
    plan['proxy']['rules_path'] = str(rules)
    configure_profiles(plan)
    for condition, record in plan['proxy']['profiles'].items():
        bundle = ProfileBundle.from_dict(record['data'])
        assert all(r['condition'] in ('any', condition) for r in bundle.ruleset.data['rules'])
        assert bundle.data['provenance']['legacy_rules_import']['ruleset_sha256'] == original.sha256


@pytest.mark.parametrize('value', [None, {'cost_usd': 1},
    {'cost_usd': float('inf'), 'max_call_cost_usd': 1},
    {'cost_usd': 1, 'max_call_cost_usd': .1, 'unknown': True}])
def test_invalid_proxy_budget_fails_preflight(value):
    from scripts.artifact_upload import proxy_budgets
    with pytest.raises(ValueError):
        proxy_budgets(value)


def test_reviewer_budget_is_explicit_and_independent():
    from scripts.artifact_upload import proxy_budgets
    base = {'cost_usd': 1, 'max_call_cost_usd': .1}
    with pytest.raises(ValueError, match='reviewer_budget'):
        proxy_budgets(base, reviewer_required=True)
    proposer, reviewer = proxy_budgets({**base, 'reviewer_budget': {'cost_usd': 2, 'max_call_cost_usd': .2}},
                                      reviewer_required=True)
    assert proposer.cost_usd == 1 and reviewer.cost_usd == 2
