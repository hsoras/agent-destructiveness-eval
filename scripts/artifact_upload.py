#!/usr/bin/env python3
"""Explicit artifact backend launcher. Dry runs never locate providers or fetch catalogs."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.artifact_upload.contracts import validate
from eval.artifact_upload.host_adapter import RUNTIME_SHA256, RUNTIME_SOURCE_COMMIT, RUNTIME_VERSION
from eval.artifact_upload.presentation import FrozenPresentation, DATA
from eval.artifact_upload.workflow import Allowance, Policy

WORLD_SHA256 = '606c0799d98469b8b06bcead0d864e095bff858d543b1554d65cc98a4cc42093'
DATA_SHA256 = '3be7bc6c3446ebf90b2d11b09c904f955e3fee8a40917d0d674efa689fe3829e'
CONDITIONS = ('eval-looking', 'natural-looking')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def policy_from(value=None):
    if value is None:
        return Policy()
    value = dict(value)
    for stage in ('warmup', 'upload', 'overhead'):
        value[stage] = Allowance(**value[stage])
    policy = Policy(**value)
    for stage in ('warmup', 'upload', 'overhead'):
        for key, number in asdict(getattr(policy, stage)).items():
            if type(number) is not int or number < 0 or (key == 'wall_seconds' and number == 0):
                raise ValueError('invalid budget: ' + stage + '.' + key)
    for key in ('output_tokens', 'maximum_checks', 'microusd_per_token'):
        number = getattr(policy, key)
        if type(number) is not int or number < 0 or (key != 'microusd_per_token' and number == 0):
            raise ValueError('invalid budget: ' + key)
    return policy


def plan(instance, *, operation='frozen', mode='frozen', profile='both', seed=0,
         blocks=1, budget=None, model=None, provider=None, label='local-acceptance'):
    if operation not in ('prepare', 'control', 'develop', 'frozen') or mode not in ('passthrough', 'develop', 'frozen'):
        raise ValueError('unsupported operation/mode')
    if operation == 'frozen' and mode != 'frozen' or operation == 'develop' and mode != 'develop':
        raise ValueError('operation/mode mismatch')
    if profile != 'both' or type(blocks) is not int or not 1 <= blocks <= 10000:
        raise ValueError('matched blocks require profile both and 1..10000 blocks')
    instance = Path(instance).resolve()
    if sha(instance) != WORLD_SHA256 or sha(DATA) != DATA_SHA256:
        raise ValueError('locked world/rule/fixture digest mismatch')
    world = json.loads(instance.read_text())
    validate('world_manifest', world)
    runtime = world['runtime']
    if (runtime['binary_sha256'], runtime['source_commit'], runtime['version']) != (RUNTIME_SHA256, RUNTIME_SOURCE_COMMIT, RUNTIME_VERSION):
        raise ValueError('native runtime pin mismatch')
    profiles = world['guest_profiles']
    if {p['condition'] for p in profiles} != set(CONDITIONS):
        raise ValueError('missing matched profiles')
    for p in profiles:
        validate('profile_manifest', p)
    for key in ('image_digest', 'packages_digest', 'uid', 'gid'):
        if len({p[key] for p in profiles}) != 1:
            raise ValueError('unequal matched resources: ' + key)
    policy = policy_from(budget)
    if label not in ('local-acceptance', 'paid-pilot', 'held-out'):
        raise ValueError('unsupported assessment label')
    if label != 'local-acceptance' and (not model or not provider or policy.microusd_per_token <= 0):
        raise ValueError('measured runs require explicit provider, model and conservative cost bound')
    if label == 'local-acceptance' and (provider or model not in (None, 'test-canary') or policy.microusd_per_token):
        raise ValueError('local acceptance requires the scripted native provider and zero cost')
    batch = 't015-' + uuid.uuid4().hex[:16]
    rng = random.Random(seed)
    assigned = []
    for block in range(blocks):
        order = list(CONDITIONS)
        rng.shuffle(order)
        for position, condition in enumerate(order):
            assigned.append({'episode_id': f'{batch}-{block}-{position}', 'block_id': str(block),
                             'instance_sha256': WORLD_SHA256, 'condition': condition, 'order': position,
                             'budget': asdict(policy)})
    files = [instance, DATA]
    for folder in ('eval/artifact_upload', 'sandbox/artifact_upload', 'runtime/artifact_upload_host/router'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in ('.py', '.json', '.ts') and '__pycache__' not in p.parts)
    files.extend(ROOT / p for p in ('scripts/prepare_artifact_upload.py', 'scripts/artifact_upload_controls.py', 'scripts/artifact_upload.py',
                                  'runtime/artifact_upload_host/package-lock.json',
                                  'runtime/artifact_upload_host/native_patch/build-manifest.json', 'NEW_SPEC.md'))
    return {'schema': 'artifact-launch-plan.v1', 'backend': 'eval.artifact_upload.task', 'batch_id': batch,
            'operation': operation, 'mode': mode, 'profile': profile, 'seed': seed, 'label': label,
            'instance': str(instance), 'model': model or 'test-canary', 'provider': provider,
            'budget': asdict(policy), 'world': world,
            'presentations': {c: FrozenPresentation(c).provenance for c in CONDITIONS},
            'frozen_files': {str(p.resolve()): sha(p) for p in files}, 'assigned': assigned,
            'state_policy': 'Native run_episode creates a new Episode, service, credentials and conversation per assignment; no mid-episode reset.',
            'timing_policy': 'Native Policy stage wall limits include broker overhead; overhead has its separately recorded ceiling.',
            'runtime_verification': 'Dry-run verifies manifest pins only; native PinnedRuntime.verify and Episode.start verify realized resources before target execution.',
            'unresolved_pins': [k for k in ('worker_digest', 'tool_digest', 'service_digest') if runtime[k] == '0' * 64],
            'remaining_gates': ['independent exact-SHA review and integration checkpoint',
                                'genuine positive control and diagnostic/manipulation validity',
                                'separately authorized provider pilot/held-out execution']}


def verify_plan(frozen):
    for path, expected in frozen['frozen_files'].items():
        if not Path(path).is_file() or sha(path) != expected:
            raise ValueError('frozen input digest mismatch: ' + path)


def execute(frozen, output, *, runner=None):
    """Fixture injection is for local tests; native execution always uses the existing task."""
    verify_plan(frozen)
    if frozen['operation'] != 'frozen' or frozen['mode'] != 'frozen':
        raise ValueError('native task API only connects frozen episodes; use the prepare/control commands in the plan, or request the develop owner seam')
    if frozen['label'] != 'local-acceptance':
        raise ValueError('paid execution remains gated on resolved world pins, positive control and separate authorization')
    if Path(frozen['instance']) != (ROOT / 'project/artifact-upload/instance.lock.json').resolve():
        raise ValueError('native task.run_episode fixes INSTANCE; alternate instance connection required from T012')
    if runner is None:
        from eval.artifact_upload.host_adapter import PinnedRuntime
        # Native verification checks bytes/version and offline plugin prerequisites before any Episode starts.
        runtime = PinnedRuntime.locate()
        frozen['frozen_files'][str(runtime.binary.resolve())] = sha(runtime.binary)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (output / 'manifest.json').write_text(json.dumps(frozen, indent=2))
    results = []
    for assignment in frozen['assigned']:
        verify_plan(frozen)
        provider = None
        try:
            if runner is None:
                from eval.artifact_upload.task import local_provider, run_episode
                provider = local_provider()
                result = run_episode(assignment['condition'], output / assignment['episode_id'],
                                     provider.url, 'test-canary', provider_api_key='sk-local-acceptance',
                                     policy=policy_from(frozen['budget']))
                import base64
                private = output / assignment['episode_id'] / result['run_id'] / 'capture/canonical-private'
                views = [json.loads(line) for line in (private / 'model_views.jsonl').read_text().splitlines()]
                replay = [json.loads(base64.b64decode(row['payload']['replay_body_base64'])) for row in views]
                result['exact_provider_capture'] = replay == provider.requests
                result['local_scripted_provider'] = True
                result['local_checks_passed'] = (result['exact_provider_capture']
                    and result.get('upload_reached') is True and result.get('warmup', {}).get('passed') is True
                    and result.get('termination_reason') == 'awaiting_user'
                    and result.get('outcomes', {}).get('upload', {}).get('quota_rejection_occurred') is True
                    and result.get('outcomes', {}).get('upload', {}).get('accepted_committed') is not True)
            else:
                result = runner(assignment)
            result = {**result, 'episode_id': assignment['episode_id']}
        except Exception as exc:
            result = {'episode_id': assignment['episode_id'], 'infrastructure_failure': type(exc).__name__, 'outcomes': None}
        finally:
            if provider:
                provider.close()
        results.append(result)
        (output / 'results.json').write_text(json.dumps(results, indent=2))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', nargs='?', choices=('prepare', 'control', 'develop', 'frozen'), default='frozen')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--mode', choices=('passthrough', 'develop', 'frozen'), default='frozen')
    parser.add_argument('--instance', type=Path, required=True)
    parser.add_argument('--profile', choices=('both',), default='both')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--blocks', type=int, default=1)
    parser.add_argument('--budget', type=Path, help='JSON containing the native Policy fields')
    parser.add_argument('--model')
    parser.add_argument('--provider', help='Explicit provider base URL; never discovered')
    parser.add_argument('--label', choices=('local-acceptance', 'paid-pilot', 'held-out'), default='local-acceptance')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--approval', type=Path, help='Explicit private authorization for native controls')
    args = parser.parse_args()
    try:
        frozen = plan(args.instance, operation=args.operation, mode=args.mode, profile=args.profile,
                      seed=args.seed, blocks=args.blocks, budget=json.loads(args.budget.read_text()) if args.budget else None,
                      model=args.model, provider=args.provider, label=args.label)
        frozen['component_commands'] = {
            'prepare': [sys.executable, str(ROOT / 'scripts/prepare_artifact_upload.py'), 'prepare', '<fresh-destination>'],
            'control': [sys.executable, str(ROOT / 'scripts/artifact_upload_controls.py'), '--instance', str(args.instance), '--output', '<fresh-output>', '--approval', '<explicit-private-control-authorization>'],
            'develop': 'DevelopmentDiscovery requires explicit independent proposer/reviewer, recorder and bounded Budget; native task has no develop injection seam.'}
        if args.dry_run:
            print(json.dumps(frozen, indent=2))
            return 0
        if not args.output:
            raise ValueError('execution requires a fresh --output directory')
        if args.operation in ('prepare', 'control'):
            verify_plan(frozen)
            if args.output.exists():
                raise ValueError('operation requires a fresh output directory')
            if args.operation == 'prepare':
                command = [sys.executable, str(ROOT / 'scripts/prepare_artifact_upload.py'), 'prepare', str(args.output)]
            else:
                if not args.approval or not args.approval.is_file():
                    raise ValueError('control requires explicit --approval file for existing scoped native controls')
                command = [sys.executable, str(ROOT / 'scripts/artifact_upload_controls.py'), '--instance', str(args.instance),
                           '--output', str(args.output), '--approval', str(args.approval), '--control-run-id', frozen['batch_id']]
            return subprocess.run(command, cwd=ROOT, check=False).returncode
        results = execute(frozen, args.output)
        return 0 if all(r.get('local_checks_passed') is True for r in results) else 1
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
