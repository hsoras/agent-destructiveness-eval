import json

import pytest

from eval.artifact_upload import task
from eval.artifact_upload.continuation import exclusive_run_lease


def saved_run(tmp_path):
    run = next(tmp_path.iterdir())
    return run, json.loads((run / 'result.json').read_text())


def test_failed_cleanup_preserves_original_failure_result_and_releases_lease(tmp_path, monkeypatch):
    class Episode:
        def __init__(self, *args, **kwargs):
            self.resources = {}

        def start(self):
            raise RuntimeError('setup failed')

        def cleanup(self):
            raise RuntimeError('cleanup failed')

    monkeypatch.setattr(task, 'Episode', Episode)
    retained = None
    try:
        task.run_episode('natural-looking', tmp_path, 'http://127.0.0.1', 'test')
    except RuntimeError as exc:
        retained = exc
    assert str(retained) == 'setup failed'
    run, result = saved_run(tmp_path)
    assert result['cleanup_errors'] == [{'step': 'episode', 'failure_class': 'RuntimeError'}]
    with exclusive_run_lease(run):
        pass


def test_failed_adapter_close_still_closes_worker_and_episode(tmp_path, monkeypatch):
    closed = []
    profile = next(p for p in json.loads(task.INSTANCE.read_text())['guest_profiles'] if p['condition'] == 'natural-looking')

    class Episode:
        api_token = 'test-only-secret'
        receipt_health = staticmethod(lambda: {'healthy': True})

        def __init__(self, *args, **kwargs):
            self.resources = {}

        def start(self):
            return {'profile_manifest': profile, 'worker_container': 'fake'}

        def cleanup(self):
            closed.append('episode')

    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            closed.append('worker')

    class Adapter:
        guard = None
        runtime = type('Runtime', (), {'binary': task.INSTANCE})()

        def __init__(self, **kwargs):
            self.recorder = task.RunRecorder(kwargs['recording_root'], kwargs['run_id'])

        def close(self):
            closed.append('adapter')
            raise RuntimeError('close failed')

    class Workflow:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            raise RuntimeError('workflow failed')

    monkeypatch.setattr(task, 'Episode', Episode)
    monkeypatch.setattr(task, 'GuestWorkerSession', Worker)
    monkeypatch.setattr(task, 'ExecutionBroker', lambda *args: object())
    monkeypatch.setattr(task.PinnedRuntime, 'locate', lambda: Adapter.runtime)
    monkeypatch.setattr(task, 'WorkflowAdapter', Adapter)
    monkeypatch.setattr(task, 'ServiceEvidence', lambda *args: type('Service', (), {'snapshot': lambda self: {}})())
    monkeypatch.setattr(task, 'ProtectedCheckpoint', lambda *args: object())
    monkeypatch.setattr(task, 'TwoStageWorkflow', Workflow)
    with pytest.raises(RuntimeError, match='workflow failed'):
        task.run_episode('natural-looking', tmp_path, 'http://127.0.0.1', 'test')
    run, result = saved_run(tmp_path)
    assert closed == ['adapter', 'worker', 'episode']
    assert result['cleanup_errors'] == [{'step': 'adapter', 'failure_class': 'RuntimeError'}]
    with exclusive_run_lease(run):
        pass
