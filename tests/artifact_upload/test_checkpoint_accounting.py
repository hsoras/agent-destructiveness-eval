from eval.artifact_upload.continuation import capture_checkpoint, load_checkpoint


def test_checkpoint_keeps_allowances_consumption_and_environment_refs(tmp_path):
    capture_checkpoint(tmp_path, run_id='run', session_id=None, condition='natural-looking',
        model_provider_refs={'api_key_env': 'TARGET_API_KEY', 'api_key': 'secret-literal'},
        workflow={}, resources={}, ledger={
            'allowances': {'upload': {'tokens': 100, 'wall_seconds': 20}},
            'usage': {'spent': {'upload': {'tokens': 40, 'wall_seconds': 4}}}},
        secrets=('secret-literal',))
    saved = load_checkpoint(tmp_path)
    assert saved['ledger']['allowances']['upload']['tokens'] == 100
    assert saved['ledger']['usage']['spent']['upload']['tokens'] == 40
    assert saved['model_provider_refs']['api_key_env'] == 'TARGET_API_KEY'
    assert saved['model_provider_refs']['api_key'] == '[redacted]'
