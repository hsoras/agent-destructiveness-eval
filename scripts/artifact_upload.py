#!/usr/bin/env python3
"""Explicit artifact backend launcher. Dry runs never locate providers or fetch catalogs."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.artifact_upload.contracts import validate
from eval.artifact_upload.host_adapter import RUNTIME_SHA256, RUNTIME_SOURCE_COMMIT, RUNTIME_VERSION, is_openrouter
from eval.artifact_upload.presentation import FrozenPresentation, DATA
from eval.artifact_upload.workflow import Allowance, Policy
from eval.artifact_upload.lifecycle import SCENARIOS

WORLD_SHA256 = '73a90a0fa64d54b85c99ecd3dbb91a2187761513426c835a10ab25b215c6cb63'
DATA_SHA256 = '3be7bc6c3446ebf90b2d11b09c904f955e3fee8a40917d0d674efa689fe3829e'
CONDITIONS = ('eval-looking', 'natural-looking')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# Exactly the files Dockerfile.worker COPYs into /opt/worker/sandbox/artifact_upload/worker/ (the guest-installed set that
# the in-guest GuestWorker.digest hashes); a test asserts this against the Dockerfile COPY line.
DEPLOYED_WORKER_FILES = ('__init__.py', 'core.py', 'guest_session.py', 'tools.py')


def tree_digest(folder, suffixes, names=None):
    """Worker-style tree algorithm (GuestWorker.digest): sha256 over sorted '<name>\\0<sha256 digest bytes>' of direct files.
    `names` restricts to an explicit file set (missing one raises: fail closed)."""
    h = hashlib.sha256()
    if names is not None:
        files = sorted(Path(folder) / n for n in names)
        if not all(p.is_file() for p in files):
            raise ValueError('required frozen source evidence unavailable: ' + str(folder))
    else:
        files = sorted(p for p in Path(folder).glob('*') if p.is_file() and p.suffix in suffixes)
    if not files:
        raise ValueError('required frozen source evidence unavailable: ' + str(folder))
    for p in files:
        h.update(p.name.encode() + b'\0' + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def source_identities(root=None):
    """Actual frozen source identities; each names its algorithm and scope and is never compared across kinds.
    Missing required source raises (fail closed); nothing here is read from the legacy lock zeros."""
    root = Path(root or ROOT)
    service = {n: sha(root / 'sandbox/artifact_upload' / n) for n in ('service.py', 'storage.py')}
    return {
        'worker_source': {'algorithm': 'worker-tree-sha256.v1: sha256 over sorted "<name>\\0<sha256 digest>" of the guest-installed worker files (GuestWorker.digest in the image)',
                          'scope': 'deployed worker files per Dockerfile.worker COPY: ' + ' '.join(DEPLOYED_WORKER_FILES),
                          'digest': tree_digest(root / 'sandbox/artifact_upload/worker', {'.py'}, DEPLOYED_WORKER_FILES),
                          'realized': 'guest-reported worker_digest in recorded raw_outputs results'},
        'tool_source': {'algorithm': 'worker-tree-sha256.v1 (same algorithm, different scope)',
                        'scope': 'runtime/artifact_upload_host/router/*.ts', 'digest': tree_digest(root / 'runtime/artifact_upload_host/router', {'.ts'}),
                        'realized': 'unavailable: no native run record; re-verified unchanged after each run (source-at-rest only)'},
        'service_source': {'algorithm': 'per-file sha256 (task.py service_provenance.source_sha256)',
                           'scope': 'sandbox/artifact_upload/{service.py,storage.py}', 'files': service,
                           'realized': 'service_provenance.source_sha256 read inside the service container'},
        'service_image_inputs': {'algorithm': 'per-file sha256', 'scope': 'Dockerfile.service, Dockerfile.worker',
                                 'files': {n: sha(root / 'sandbox/artifact_upload' / n) for n in ('Dockerfile.service', 'Dockerfile.worker')},
                                 'realized': 'service image ID is built fresh per run (build timestamps vary): recorded, never required identical'},
        'common_image': {'algorithm': 'docker image Id (sha256 of image config); not a file/tree hash',
                         'scope': 'guest profile image_digest witness', 'id': json.loads((root / 'project/artifact-upload/instance.lock.json').read_text())['guest_profiles'][0]['image_digest'],
                         'realized': 'episode resources.common_image.id'},
    }


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
    for key in ('output_tokens', 'maximum_checks'):
        number = getattr(policy, key)
        if type(number) is not int or number <= 0:
            raise ValueError('invalid budget: ' + key)
    rate = policy.microusd_per_token
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
        raise ValueError('invalid budget: microusd_per_token')
    return policy


def profile_paths(value, conditions, flag):
    """A single condition takes a path; matched conditions take a JSON mapping."""
    if value is None:
        return {}
    if isinstance(value, dict):
        mapping = value
    elif str(value).lstrip().startswith('{'):
        mapping = json.loads(str(value))
    else:
        if len(conditions) != 1:
            raise ValueError(flag + ' with --condition both requires a condition-keyed JSON path mapping')
        mapping = {conditions[0]: str(value)}
    if (not isinstance(mapping, dict) or set(mapping) != set(conditions)
            or any(not isinstance(p, str) or not p for p in mapping.values())):
        raise ValueError(flag + ' must map every selected condition to a path')
    resolved = {c: str(Path(p).expanduser().resolve()) for c, p in mapping.items()}
    if len(set(resolved.values())) != len(resolved):
        raise ValueError(flag + ' conditions require independent files')
    return resolved


def configure_profiles(frozen, *, profile_in=None, profile_out=None, condition='both'):
    """Freeze initial bundles explicitly; output files never become implicit inputs."""
    if profile_in is not None and frozen['proxy'].get('rules_path'):
        raise ValueError('--rules and --profile-in are mutually exclusive')
    conditions = list(CONDITIONS) if condition == 'both' else [condition]
    inputs = profile_paths(profile_in, conditions, '--profile-in')
    outputs = profile_paths(profile_out, conditions, '--profile-out')
    if not inputs and not outputs and frozen['proxy']['mode'] != 'llm':
        return
    # Reject cross-condition input/output aliases as well as duplicate mappings.
    for left in conditions:
        for right in conditions:
            if left != right and inputs.get(left) == outputs.get(right) and inputs.get(left):
                raise ValueError('profile input/output paths overlap across conditions')
    from eval.artifact_upload.profiles import ProfileBundle
    bundles, initialization = {}, {}
    for selected in conditions:
        path = inputs.get(selected)
        existed = Path(path).exists() if path else False
        bundle = (ProfileBundle.load_or_initialize(path, selected) if path
                  else ProfileBundle.preset(selected))
        if bundle.data['condition'] != selected:
            raise ValueError('profile condition mismatch: ' + selected)
        if frozen['proxy'].get('rules_path'):
            from eval.artifact_upload.rules import RuleSet
            data = bundle.data
            imported = RuleSet.load(frozen['proxy']['rules_path'], selected)
            excluded = [r['rule_id'] for r in imported.data['rules'] if r['condition'] not in ('any', selected)]
            filtered = imported.with_changes([{'action': 'remove', 'rule_id': rid} for rid in excluded]) if excluded else imported
            data['ruleset'] = filtered.data
            data['provenance']['legacy_rules_import'] = {'path': frozen['proxy']['rules_path'],
                'ruleset_sha256': imported.sha256, 'excluded_rule_ids': excluded}
            bundle = ProfileBundle.from_dict(data)
        bundles[selected] = {'data': bundle.data, 'sha256': bundle.sha256}
        initialization[selected] = {'input_path': path, 'output_path': outputs.get(selected),
                                    'source': 'reused' if existed else 'initialized' if path else 'preset',
                                    'fresh_episode': True}
        # The initial content is frozen above. This path may also be the output,
        # so later commits must not invalidate the immutable launch plan.
        if path:
            initialization[selected]['input_file_sha256'] = sha(path)
    frozen['proxy'].update(profiles=bundles, profile_outputs=outputs,
                           profile_initialization=initialization)


def plan(instance, *, operation='frozen', mode='frozen', profile='both', seed=0,
         blocks=1, budget=None, model=None, provider=None, label='local-acceptance', proxy_mode='script', rules_path=None, guest_image_id=None, zdr=None, review_phase='post',
         scenario='bug-and-dilemma', development_runtime=None):
    if development_runtime is not None and not (
            operation == 'run' and proxy_mode == 'llm' and mode == 'develop' and label == 'live-preview'):
        raise ValueError('--development-runtime is restricted to run --proxy-mode llm --mode develop --label live-preview')
    development_artifact = None
    development_runtime_obj = None
    if development_runtime is not None:
        from eval.artifact_upload.runtime_artifacts import DevelopmentRuntime
        development_runtime_obj = DevelopmentRuntime.from_receipt(development_runtime).verify()
        development_artifact = {'receipt': str(development_runtime_obj.receipt),
                                'receipt_sha256': sha(development_runtime_obj.receipt),
                                'provenance': development_runtime_obj.provenance}
    if scenario not in SCENARIOS:
        raise ValueError('unsupported scenario: ' + repr(scenario))
    if scenario == 'dilemma-only' and (operation not in ('run', 'frozen') or label in ('paid-pilot', 'held-out')):
        raise ValueError('dilemma-only is only available for run/frozen launches with local-acceptance or live-preview labels')
    if operation == 'frozen':
        proxy_mode = 'script'
    if operation not in ('prepare', 'control', 'develop', 'run', 'frozen') or mode not in ('passthrough', 'develop', 'frozen'):
        raise ValueError('unsupported operation/mode')
    if operation == 'run':
        requested_mode = mode
        mode = 'develop' if proxy_mode == 'llm' else 'frozen'
        if proxy_mode not in ('llm', 'script'):
            raise ValueError('unsupported proxy mode')
        if proxy_mode == 'llm' and (label != 'live-preview' or label in ('local-acceptance', 'paid-pilot', 'held-out')):
            raise ValueError('llm proxy requires the development-only live-preview label')
        if proxy_mode == 'llm' and (requested_mode != 'develop' or label != 'live-preview'):
            raise ValueError('llm proxy requires live-preview label and develop mode')
        if proxy_mode == 'llm' and (not model or not provider or policy_from(budget).microusd_per_token <= 0):
            raise ValueError('llm proxy requires explicit target provider, model and positive cost bound')
    if operation == 'frozen' and mode != 'frozen':
        raise ValueError('operation/mode mismatch')
    if operation == 'develop' and mode != 'develop':
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
    if guest_image_id is not None:
        if proxy_mode != 'llm' or operation != 'run' or label != 'live-preview':
            raise ValueError('--guest-image-id is only allowed for --proxy-mode llm live-preview runs')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', guest_image_id):
            raise ValueError('--guest-image-id must be a full sha256:<64 hex> image id')
    policy = policy_from(budget)
    if label not in ('local-acceptance', 'paid-pilot', 'held-out', 'live-preview'):
        raise ValueError('unsupported assessment label')
    if label in ('paid-pilot', 'held-out') and (not model or not provider or policy.microusd_per_token <= 0):
        raise ValueError('measured runs require explicit provider, model and conservative cost bound')
    if label == 'local-acceptance' and (provider or model not in (None, 'test-canary') or policy.microusd_per_token):
        raise ValueError('local acceptance requires the scripted native provider and zero cost')
    batch = ('live-' if proxy_mode == 'llm' and operation == 'run' else 't015-') + uuid.uuid4().hex[:16]
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
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in ('.py', '.json', '.ts', '.txt') and '__pycache__' not in p.parts)
    files.extend(ROOT / p for p in ('scripts/prepare_artifact_upload.py', 'scripts/artifact_upload_controls.py', 'scripts/artifact_upload.py',
                                  'runtime/artifact_upload_host/package-lock.json',
                                  'runtime/artifact_upload_host/native_patch/build-manifest.json'))
    if development_artifact is not None:
        files.extend((development_runtime_obj.receipt, development_runtime_obj.binary))
    return {'schema': 'artifact-launch-plan.v1', 'backend': 'eval.artifact_upload.task', 'batch_id': batch,
            'operation': ('script' if operation == 'run' and proxy_mode == 'script' else 'llm' if operation == 'run' else operation),
            'launch_operation': operation, 'scenario': scenario, 'mode': ('develop' if proxy_mode == 'llm' and operation == 'run' else mode), 'profile': profile, 'seed': seed, 'label': label,
            'proxy': {'mode': proxy_mode, 'rules_path': str(Path(rules_path).resolve()) if rules_path else None,
                      'rules_sha256': sha(rules_path) if rules_path else None,
                      'zdr': bool(zdr) if zdr is not None else is_openrouter(provider or ''),
                      'review_phase': review_phase},
            **({'image_pin_override': {'requested': guest_image_id, 'locked': source_identities()['common_image']['id'],
                                       'reason': 'live-preview development only'}} if guest_image_id else {}),
            'instance': str(instance), 'model': model or 'test-canary', 'provider': provider,
            'budget': asdict(policy), 'world': world,
            **({'scenario_note': 'dilemma-only: no warmup stage or repair checkpoint; budget.warmup is ignored (zero allowance); '
                                 'workspace starts from the committed reference repair'} if scenario == 'dilemma-only' else {}),
            'presentations': {c: FrozenPresentation(c).provenance for c in CONDITIONS},
            'frozen_files': {str(p.resolve()): sha(p) for p in files}, 'assigned': assigned,
            'state_policy': 'Native run_episode creates a new Episode, service, credentials and conversation per assignment; no mid-episode reset.',
            'timing_policy': 'Native Policy stage wall limits include broker overhead; overhead has its separately recorded ceiling.',
            'runtime_verification': 'Dry-run verifies manifest pins only; native PinnedRuntime.verify and Episode.start verify realized resources before target execution.',
            'source_identities': source_identities(),
            **({'development_runtime': development_artifact} if development_artifact is not None else {}),
            # Legacy lock zeros are an unused template (not gates); actual frozen identities are in source_identities.
            'unresolved_pins': [k for k in ('worker_digest', 'tool_digest', 'service_digest') if runtime[k] == '0' * 64],
            'limitations': ['Single genuine instance (sqlite-utils #794): held-out is new blocks of the same instance, not a new task; repeated rollouts are samples of one instance.'],
            'remaining_gates': ['independent exact-SHA review and integration checkpoint',
                                'genuine positive control and diagnostic/manipulation validity',
                                'separately authorized provider pilot/held-out execution']}


def verify_plan(frozen):
    for path, expected in frozen['frozen_files'].items():
        if not Path(path).is_file() or sha(path) != expected:
            raise ValueError('frozen input digest mismatch: ' + path)
    if frozen.get('source_identities') != source_identities():
        raise ValueError('frozen source identity mismatch (worker/tool/service/image inputs changed)')


COVERAGE = {'supported-rendering': 'deterministic_hits', 'supported-raw': 'deterministic_hits',
            'unsupported-structure': 'unknown_formats', 'unclassified-observation': 'passthroughs',
            'actual-error-or-stderr': 'passthroughs', 'non-text-transport': 'passthroughs'}
CREDENTIAL_FAILURE = 'configured credential in researcher export'


def run_directory(episode_dir, result):
    """Run-local evidence directory: the recorded run_id, else the only child holding a native result."""
    if isinstance(result, dict) and isinstance(result.get('run_id'), str):
        return episode_dir / result['run_id']
    found = [p.parent for p in episode_dir.glob('*/result.json')]
    return found[0] if len(found) == 1 else None


def native_telemetry(result, run_dir):
    """Summarize recorded native evidence only; anything not recorded stays absent (reported unavailable)."""
    telemetry = {}
    usage = [t.get('usage') for t in result.get('turns') or [] if isinstance(t, dict)]
    usage = [u for u in usage if isinstance(u, dict)]
    if usage:
        telemetry['main_usage'] = {k: sum(u[k] for u in usage if type(u.get(k)) in (int, float))
                                   for k in sorted({k for u in usage for k, v in u.items() if type(v) in (int, float)})}
    path = run_dir / 'presentation.jsonl' if run_dir else None
    if path and path.is_file():
        counts = dict.fromkeys(set(COVERAGE.values()), 0)
        try:
            for line in path.read_text().splitlines():
                name = COVERAGE.get(json.loads(line).get('reason'))
                if name:
                    counts[name] += 1
            telemetry.update(counts)
        except (OSError, ValueError, AttributeError):
            pass  # unreadable coverage log: leave unavailable rather than report partial counts
    return telemetry


def finish(result, episode_dir, failure=None):
    """Attach native-derived telemetry and leak status without overriding anything the result already states."""
    telemetry = native_telemetry(result, run_directory(episode_dir, result))
    if telemetry:
        result['telemetry'] = {**telemetry, **(result.get('telemetry') or {})}
    if 'leak_detected' not in result:
        result['leak_detected'] = (True if failure == CREDENTIAL_FAILURE
                                   else False if result.get('researcher_redaction_verified') is True else None)
    return result


def persisted_native_result(episode_dir):
    """Exactly one run-local native result.json written by the failed episode, else None (outcomes unknown)."""
    found = list(episode_dir.glob('*/result.json'))
    if len(found) != 1:
        return None
    try:
        native = json.loads(found[0].read_text())
    except (OSError, ValueError):
        return None
    return native if isinstance(native, dict) else None


def jsonl(path):
    try:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def realized_mismatches(frozen, result, run_dir):
    """Compare recorded realized provenance with the frozen identities. Unavailable required evidence is a mismatch.
    The per-run service image ID is recorded, not compared (fresh builds legitimately differ)."""
    ident, found, problems = frozen['source_identities'], {}, []
    workers = {row['payload']['result']['worker_digest'] for row in jsonl(run_dir / 'capture/canonical-private/raw_outputs.jsonl')
               if isinstance(row.get('payload'), dict) and isinstance(row['payload'].get('result'), dict) and 'worker_digest' in row['payload']['result']} if run_dir else set()
    found['worker_source'] = sorted(workers)
    if workers != {ident['worker_source']['digest']}:
        problems.append('worker_source: realized ' + (','.join(sorted(workers)) or 'unavailable') + ' != frozen')
    provenance = result.get('service_provenance')
    files = provenance.get('source_sha256') if isinstance(provenance, dict) else None
    found['service_source'] = files
    if files != ident['service_source']['files']:
        problems.append('service_source: realized ' + ('unavailable' if files is None else 'files differ') + ' != frozen')
    found['service_image'] = provenance.get('image') if isinstance(provenance, dict) else None
    if not found['service_image']:
        problems.append('service_image: realized image unavailable')
    history = sorted(run_dir.glob('episode/*.history.jsonl')) if run_dir else []
    rows = jsonl(history[-1]) if history else []
    image = rows[-1].get('common_image', {}).get('id') if rows and isinstance(rows[-1], dict) and isinstance(rows[-1].get('common_image'), dict) else None
    found['common_image'] = image
    if image != ident['common_image']['id']:
        problems.append('common_image: realized ' + (image or 'unavailable') + ' != frozen')
    try:
        if source_identities()['tool_source'] != ident['tool_source']:
            problems.append('tool_source: changed during run')
    except ValueError:
        problems.append('tool_source: unavailable')
    return found, problems


def classify_source_provenance(frozen, result, episode_dir):
    """Attach the comparison; a mismatch marks the episode infrastructure-invalid but keeps every recorded outcome."""
    found, problems = realized_mismatches(frozen, result, run_directory(episode_dir, result))
    result['source_provenance'] = {'realized': found, 'mismatches': problems}
    if problems and not result.get('infrastructure_failure'):
        result['infrastructure_failure'] = 'SourceProvenanceMismatch'
    return result


def capture_checks(result, private, replay, received):
    """Persist what the local provider received before comparing; exact body multiset decides, arrival order is recorded only."""
    from eval.artifact_upload.task import private_write, provider_capture_matches
    private_write(Path(private) / 'local-provider-requests.json', json.dumps(received).encode())
    result['provider_capture_order_matches'] = replay == received
    result['exact_provider_capture'] = provider_capture_matches(replay, received)
    result['provider_capture_semantics'] = 'canonical JSON body multiset; arrival order recorded separately (not independent wire attestation)'
    return result['exact_provider_capture']


def execute(frozen, output, *, runner=None, verify_realized=None):
    """Fixture injection is for local tests; native execution always uses the existing task."""
    verify_plan(frozen)
    if frozen.get('launch_operation', frozen['operation']) not in ('frozen', 'run') or frozen['mode'] != 'frozen' or frozen.get('proxy', {}).get('mode', 'script') != 'script':
        raise ValueError('native task API only connects frozen episodes; use the prepare/control commands in the plan, or develop_offline for explicit offline development')
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
                provider = local_provider(scenario=frozen.get('scenario', 'bug-and-dilemma'))
                from eval.artifact_upload.rules import RuleSet, default_ruleset
                rules_path = frozen.get('proxy', {}).get('rules_path')
                rule = RuleSet.load(rules_path) if rules_path else default_ruleset(assignment['condition'])
                result = run_episode(assignment['condition'], output / assignment['episode_id'],
                                     provider.url, 'test-canary', provider_api_key='sk-local-acceptance',
                                     policy=policy_from(frozen['budget']), proxy_mode='script', ruleset=rule,
                                     rules_out=frozen.get('proxy', {}).get('rules_outputs', {}).get(assignment['condition']),
                                     **profile_episode_arguments(frozen, assignment['condition']),
                                     scenario=frozen.get('scenario', 'bug-and-dilemma'))
                import base64
                private = output / assignment['episode_id'] / result['run_id'] / 'capture/canonical-private'
                views = [json.loads(line) for line in (private / 'model_views.jsonl').read_text().splitlines()]
                replay = [json.loads(base64.b64decode(row['payload']['replay_body_base64'])) for row in views]
                capture_checks(result, private, replay, provider.requests)
                result['local_scripted_provider'] = True
                result['local_checks_passed'] = (result['exact_provider_capture']
                    and result.get('upload_reached') is True
                    and (result.get('warmup', {}).get('passed') is True or result.get('warmup') == {'skipped': True})
                    and result.get('termination_reason') == 'awaiting_user'
                    and result.get('outcomes', {}).get('upload', {}).get('quota_rejection_occurred') is True
                    and result.get('outcomes', {}).get('upload', {}).get('accepted_committed') is not True)
            else:
                result = runner(assignment)
            result = finish({**result, 'episode_id': assignment['episode_id']}, output / assignment['episode_id'])
            if runner is None if verify_realized is None else verify_realized:
                classify_source_provenance(frozen, result, output / assignment['episode_id'])
        except Exception as exc:
            episode_dir = output / assignment['episode_id']
            native = persisted_native_result(episode_dir)
            result = finish({**(native or {'outcomes': None}), 'episode_id': assignment['episode_id'],
                             'infrastructure_failure': type(exc).__name__}, episode_dir, str(exc))
        finally:
            if provider:
                provider.close()
        results.append(result)
        (output / 'results.json').write_text(json.dumps(results, indent=2))
    return results


ROUTE_FIELDS = {'upstream', 'model', 'input_price', 'output_price', 'api_key_env', 'provider_pin'}

def valid_route_fields(route):
    return isinstance(route, dict) and (set(route) == ROUTE_FIELDS or set(route) == ROUTE_FIELDS - {'provider_pin'})


def profile_episode_arguments(frozen, condition):
    configured = frozen.get('proxy', {}).get('profiles', {}).get(condition)
    if configured is None:
        return {}
    from eval.artifact_upload.profiles import ProfileBundle
    bundle = ProfileBundle.from_dict(configured['data'])
    if bundle.sha256 != configured['sha256']:
        raise ValueError('initial profile digest mismatch')
    return {'proxy_profile': bundle,
            'profile_out': frozen['proxy'].get('profile_outputs', {}).get(condition),
            'profile_initialization': frozen['proxy'].get('profile_initialization', {}).get(condition)}


def proxy_budgets(value, *, reviewer_required=False):
    from eval.artifact_upload.live import LiveBudget
    if not isinstance(value, dict):
        raise ValueError('proxy budget must be a JSON object')
    config = dict(value)
    review_config = config.pop('reviewer_budget', None)
    if reviewer_required and review_config is None:
        raise ValueError('profile reviewer requires an explicit reviewer_budget in --proxy-budget')
    try:
        budget = LiveBudget(**config)
        review = LiveBudget(**review_config) if review_config is not None else None
    except TypeError as exc:
        raise ValueError('invalid proxy budget fields') from exc
    return budget, review


def run_live(frozen, output, *, routes, live_budget, target_api_key, rules_out=None, reviewer=False, provider_pin=None,
             target_api_key_env='OPENROUTER_API_KEY', runtime_override=None):
    """Execute development-only live proxy episodes against the explicitly named target model."""
    verify_plan(frozen)
    if frozen.get('launch_operation', frozen['operation']) != 'run' or frozen['proxy']['mode'] != 'llm':
        raise ValueError('run_live requires an llm run plan')
    if not target_api_key:
        raise ValueError('target API key environment variable is not set')
    from eval.artifact_upload.discovery import AuxiliaryClient
    from eval.artifact_upload.live import LiveBudget
    from eval.artifact_upload.rules import RuleSet
    if not routes or not isinstance(live_budget, dict):
        raise ValueError('llm mode requires proxy routes and budget')
    if 'proposer' not in routes or (reviewer and 'reviewer' not in routes):
        raise ValueError('llm mode requires proposer route and reviewer route when enabled')
    clients = {}
    for role in ('proposer', 'reviewer'):
        if role == 'reviewer' and not reviewer:
            continue
        route = routes[role]
        if not valid_route_fields(route):
            raise ValueError('invalid %s proxy route' % role)
        if is_openrouter(route['upstream']) and route.get('provider_pin') is None:
            raise ValueError('OpenRouter proxy route requires provider_pin')
        key = os.environ.get(route['api_key_env'])
        if not key:
            raise ValueError('proxy route credential environment variable is not set')
        clients[role] = AuxiliaryClient(route['upstream'], route['model'], input_price=route['input_price'],
                                        output_price=route['output_price'], api_key=key,
                                        provider_options=None, provider_pin=route.get('provider_pin'))
    live_budget, reviewer_budget = proxy_budgets(live_budget,
        reviewer_required=reviewer and bool(frozen['proxy'].get('profiles')))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (output / 'manifest.json').write_text(json.dumps(frozen, indent=2))
    results = []
    for assignment in frozen['assigned']:
        rules = RuleSet.load(frozen['proxy']['rules_path'], assignment['condition']) if frozen['proxy'].get('rules_path') else None
        from eval.artifact_upload.task import run_episode
        result = run_episode(assignment['condition'], output / assignment['episode_id'], frozen['provider'], frozen['model'],
            provider_api_key=target_api_key, policy=policy_from(frozen['budget']), proxy_mode='llm', ruleset=rules,
            live={**clients, 'budget': live_budget, 'reviewer_budget': reviewer_budget,
                  'target_api_key_env': target_api_key_env, 'routes': routes},
            rules_out=frozen['proxy'].get('rules_outputs', {}).get(assignment['condition'], rules_out),
            **profile_episode_arguments(frozen, assignment['condition']),
            review_phase=frozen['proxy'].get('review_phase', 'both'),  # manifests written before the default flip used 'both'
            runtime_override=runtime_override,
            guest_image_id=(frozen.get('image_pin_override') or {}).get('requested'), zdr=frozen['proxy'].get('zdr', True),
            provider_pin=provider_pin, scenario=frozen.get('scenario', 'bug-and-dilemma'))
        results.append(finish({**result, 'episode_id': assignment['episode_id']}, output / assignment['episode_id']))
        (output / 'results.json').write_text(json.dumps(results, indent=2))
    return results


def develop_clients(routes, environ=None):
    """Build the two explicitly configured, distinct auxiliary routes. No call is made here; nothing is defaulted."""
    from eval.artifact_upload.discovery import AuxiliaryClient
    environ = os.environ if environ is None else environ
    if not isinstance(routes, dict) or set(routes) != {'proposer', 'reviewer'}:
        raise ValueError('develop requires explicit separate proposer and reviewer routes')
    clients = {}
    for role, route in routes.items():
        if not valid_route_fields(route) or not route['upstream'] or not route['model']:
            raise ValueError('develop route %s must set exactly: %s' % (role, ', '.join(sorted(ROUTE_FIELDS))))
        if is_openrouter(route['upstream']) and route.get('provider_pin') is None:
            raise ValueError('OpenRouter proxy route requires provider_pin')
        key = environ.get(route['api_key_env'])
        if not key:
            raise ValueError('develop route %s: credential environment variable is not set' % role)
        clients[role] = AuxiliaryClient(route['upstream'], route['model'], input_price=route['input_price'],
                                        output_price=route['output_price'], api_key=key,
                                        provider_pin=route.get('provider_pin'))
    return clients


def captured_evidence(captured, base_digests):
    """Evidence from real recorded shell observations (recorder raw_outputs); exact original bytes, deduplicated.
    Returns (evidence, skipped) where skipped counts observations the existing Evidence gate does not scope."""
    from eval.artifact_upload.discovery import Evidence
    found, seen, skipped = [], set(), {}
    for path in sorted(Path(captured).rglob('canonical-private/raw_outputs.jsonl')):
        for row in jsonl(path):
            payload = row.get('payload') if isinstance(row, dict) else None
            call, result = (payload.get('call'), payload.get('result')) if isinstance(payload, dict) else (None, None)
            if not isinstance(call, dict) or not isinstance(result, dict) or call.get('operation') != 'shell':
                continue
            try:
                data = base64.b64decode(result['stdout']) if result['encoding'] == 'base64' else result['stdout'].encode()
                item = Evidence(call['arguments']['command'], data.decode(), call['guest_profile'], tuple(sorted(base_digests.items())),
                                status=result['status'], exit_code=result['exit_code'] if result['exit_code'] is not None else -1,
                                stderr=result['stderr'])
                item.packet()
            except (KeyError, TypeError, ValueError):
                skipped['unscoped-or-unverifiable'] = skipped.get('unscoped-or-unverifiable', 0) + 1
                continue
            key = (item.command, item.condition, item.raw)
            if key not in seen:
                seen.add(key)
                found.append(item)
    return found, skipped


def develop_offline(frozen, captured, output, *, routes=None, budget=None, clients=None, environ=None):
    """Explicit offline development discovery over captured tool observations.

    Uses the existing DevelopmentDiscovery (distinct proposer/reviewer, RunRecorder diagnostics, bounded Budget). The result is
    development evidence only: nothing is installed, promoted or presented to a target episode. Refuses without configuration."""
    from eval.artifact_upload.discovery import Budget, DevelopmentDiscovery
    from eval.artifact_upload.recording import RunRecorder
    verify_plan(frozen)
    if frozen['operation'] != 'develop' or frozen['mode'] != 'develop' or frozen['label'] != 'local-acceptance':
        raise ValueError('develop requires a develop plan with the unpaid local-acceptance label')
    if budget is None or routes is None and clients is None:
        raise ValueError('develop requires explicit routes and a bounded development budget; there is no default')
    budget = Budget(**budget)
    clients = clients or develop_clients(routes, environ)
    ident = frozen['source_identities']
    base = {'worker': ident['worker_source']['digest'], 'tool': ident['tool_source']['digest'],
            'service': hashlib.sha256(json.dumps(ident['service_source']['files'], sort_keys=True).encode()).hexdigest(),
            'image': ident['common_image']['id'].split(':')[-1], 'rules': DATA_SHA256}
    evidence, skipped = captured_evidence(captured, base)
    if not evidence:
        raise ValueError('no scoped captured tool observations in --evidence')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    secrets = {role + '-api-key': c.api_key for role, c in clients.items() if c.api_key}
    recorder = RunRecorder(output / 'recording', frozen['batch_id'], credentials=secrets)
    discovery = DevelopmentDiscovery(mode='develop', proposer=clients['proposer'], reviewer=clients['reviewer'], recorder=recorder,
                                     budget=budget, base_digests=base, source_batch_id=frozen['batch_id'])
    decisions = []
    for item in evidence[:budget.pairs]:
        d = discovery.discover(item)
        decisions.append({'command': item.command, 'condition': item.condition, 'accepted': d.accepted,
                          'transaction_id': d.transaction_id, 'proposal_digest': d.proposal_digest})
    summary = {'schema': 'artifact-develop-offline.v1', 'result_class': 'development-not-behavioral-result', 'batch_id': frozen['batch_id'],
               'base_digests': base, 'decisions': decisions, 'deferred_for_budget': max(0, len(evidence) - budget.pairs),
               'skipped_unscoped': skipped, 'usage': discovery.usage(),
               'exact_originals': 'recording/canonical-private/diagnostics.jsonl (development_candidate.original_evidence); credentials redacted only in recording/researcher',
               'promotion': 'not performed: existing review_implementation/promote with developer-owned tested source into an inactive batch'}
    (output / 'develop.json').write_text(json.dumps(summary, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', nargs='?', choices=('prepare', 'control', 'develop', 'run', 'frozen', 'continue'), default='frozen')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--mode', choices=('passthrough', 'develop', 'frozen'), default='frozen')
    parser.add_argument('--proxy-mode', choices=('llm', 'script'), default='script')
    parser.add_argument('--zdr', action=argparse.BooleanOptionalAction, default=None,
                        help='force OpenRouter Zero Data Retention; use --no-zdr only to opt out')
    parser.add_argument('--review-phase', choices=('both', 'post'), default='post',
                        help="llm profile runs: 'post' calls the proposer only after tool execution (one call per tool); 'both' also inspects before execution")
    parser.add_argument('--guest-image-id', help='live-preview only (--proxy-mode llm): substitute local guest image id for the unobtainable locked pin')
    parser.add_argument('--development-runtime', type=Path,
                        help='explicit verified development runtime receipt; run/llm/develop/live-preview only')
    parser.add_argument('--rules', type=Path)
    parser.add_argument('--proxy-routes', type=Path)
    parser.add_argument('--proxy-budget', type=Path)
    parser.add_argument('--rules-out', help='legacy rules export path; condition-keyed JSON mapping for both profile bundles')
    parser.add_argument('--profile-in', help='profile bundle path, or JSON condition-to-path mapping for both')
    parser.add_argument('--profile-out', help='evolved bundle destination, or JSON condition-to-path mapping for both')
    parser.add_argument('--run-dir', type=Path, help='continue: saved native run checkpoint directory')
    parser.add_argument('--additional-budget', type=Path, help='continue: positive target-stage allowance increments')
    parser.add_argument('--additional-proxy-budget', type=Path, help='continue: positive auxiliary allowance increments')
    parser.add_argument('--reviewer', choices=('on', 'off'), default='off')
    parser.add_argument('--target-api-key-env', default='OPENROUTER_API_KEY')
    parser.add_argument('--scenario', choices=SCENARIOS, default='bug-and-dilemma',
                        help='bug-and-dilemma: warmup bug fix then upload dilemma (default); dilemma-only: repaired source, upload task only')
    parser.add_argument('--condition', choices=('both', 'eval-looking', 'natural-looking'), default='both')
    parser.add_argument('--instance', type=Path)
    parser.add_argument('--profile', choices=('both',), default='both')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--blocks', type=int, default=1)
    parser.add_argument('--budget', type=Path, help='JSON containing the native Policy fields')
    parser.add_argument('--model')
    parser.add_argument('--provider', help='Explicit provider base URL; never discovered')
    parser.add_argument('--openrouter-provider', action='append', dest='openrouter_provider',
                        help='Pinned OpenRouter provider tag; repeat to allow an ordered set')
    parser.add_argument('--label', choices=('local-acceptance', 'paid-pilot', 'held-out', 'live-preview'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--evidence', type=Path, help='develop: directory of captured recorder runs (canonical-private/raw_outputs.jsonl)')
    parser.add_argument('--develop-routes', type=Path, help='develop: JSON {proposer,reviewer: upstream,model,input_price,output_price,api_key_env}')
    parser.add_argument('--develop-budget', type=Path, help='develop: JSON bounded development Budget fields')
    parser.add_argument('--approval', type=Path, help='Explicit private authorization for native controls')
    args = parser.parse_args()
    try:
        if args.operation == 'continue':
            if args.run_dir is None:
                raise ValueError('continue requires --run-dir')
            from eval.artifact_upload.runtime_resume import continue_native_run
            target_extra = json.loads(args.additional_budget.read_text()) if args.additional_budget else None
            proxy_extra = json.loads(args.additional_proxy_budget.read_text()) if args.additional_proxy_budget else None
            from eval.artifact_upload.proxy_control import DESCRIPTOR, replenish_active
            try:
                if (args.run_dir / DESCRIPTOR).exists():
                    if target_extra is not None:
                        raise ValueError('active run only supports auxiliary replenishment; target continuation requires a paused checkpoint')
                    receipt = replenish_active(args.run_dir, proxy_extra)
                else:
                    receipt = continue_native_run(args.run_dir, additional_allowances=target_extra,
                                                  additional_proxy_allowances=proxy_extra)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            print(json.dumps(receipt, indent=2))
            return 0
        if args.instance is None:
            raise ValueError('--instance is required')
        if args.development_runtime is not None and not (
                args.operation == 'run' and args.proxy_mode == 'llm' and args.mode == 'develop'
                and args.label == 'live-preview'):
            raise ValueError('--development-runtime requires run --proxy-mode llm --mode develop --label live-preview')
        if args.zdr is False:
            print('WARNING: OpenRouter Zero Data Retention is disabled by --no-zdr.', file=sys.stderr)
        if args.operation == 'run' and args.proxy_mode == 'llm':
            if args.label in ('local-acceptance', 'paid-pilot', 'held-out'):
                raise ValueError('llm proxy refuses measured labels; use live-preview')
            args.mode, args.label = 'develop', 'live-preview'
            if not args.proxy_routes or not args.proxy_budget:
                raise ValueError('llm mode requires --proxy-routes and --proxy-budget')
            if not os.environ.get(args.target_api_key_env):
                raise ValueError('target API key environment variable is not set')
        if args.label is None:
            args.label = 'local-acceptance'
        frozen = plan(args.instance, operation=args.operation, mode=args.mode, profile=args.profile,
                      seed=args.seed, blocks=args.blocks, budget=json.loads(args.budget.read_text()) if args.budget else None,
                      model=args.model, provider=args.provider, label=args.label,
                      proxy_mode=args.proxy_mode, rules_path=args.rules, guest_image_id=args.guest_image_id,
                      zdr=args.zdr, review_phase=args.review_phase, scenario=args.scenario, development_runtime=args.development_runtime)
        if args.operation in ('run', 'frozen') and args.condition != 'both':
            frozen['assigned'] = [a for a in frozen['assigned'] if a['condition'] == args.condition]
        if args.operation == 'run' and args.proxy_mode == 'llm':
            routes = json.loads(args.proxy_routes.read_text())
            budget_value = json.loads(args.proxy_budget.read_text())
            proposer_budget, reviewer_budget = proxy_budgets(budget_value, reviewer_required=args.reviewer == 'on')
            proxy_budgets(budget_value, reviewer_required=args.reviewer == 'on')
            # Validate credentials and route shape before any target or auxiliary request.
            for role in (('proposer', 'reviewer') if args.reviewer == 'on' else ('proposer',)):
                route = routes.get(role) if isinstance(routes, dict) else None
                if not valid_route_fields(route) or not os.environ.get(route.get('api_key_env', '')):
                    raise ValueError('llm mode requires valid %s proxy route and configured key' % role)
                if is_openrouter(route['upstream']) and route.get('provider_pin') is None:
                    raise ValueError('OpenRouter proxy route requires provider_pin')
            if is_openrouter(args.provider or '') and args.zdr is not False and not args.openrouter_provider:
                raise ValueError('OpenRouter ZDR requires --openrouter-provider')
        configure_profiles(frozen, profile_in=args.profile_in, profile_out=args.profile_out, condition=args.condition)
        if args.rules_out and frozen['proxy'].get('profiles'):
            frozen['proxy']['rules_outputs'] = profile_paths(args.rules_out,
                list(CONDITIONS) if args.condition == 'both' else [args.condition], '--rules-out')
        frozen['component_commands'] = {
            'prepare': [sys.executable, str(ROOT / 'scripts/prepare_artifact_upload.py'), 'prepare', '<fresh-destination>'],
            'control': [sys.executable, str(ROOT / 'scripts/artifact_upload_controls.py'), '--instance', str(args.instance), '--output', '<fresh-output>', '--approval', '<explicit-private-control-authorization>'],
            'develop': 'develop --evidence <captured-dir> --develop-routes <file> --develop-budget <file> --output <fresh>: offline DevelopmentDiscovery over captured observations; frozen task.run_episode gets no auxiliary models.'}
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
        if args.operation == 'develop':
            if not (args.evidence and args.develop_routes and args.develop_budget):
                raise ValueError('develop requires --evidence, --develop-routes and --develop-budget (no defaults, no live default)')
            summary = develop_offline(frozen, args.evidence, args.output, routes=json.loads(args.develop_routes.read_text()),
                                      budget=json.loads(args.develop_budget.read_text()))
            print(json.dumps(summary, indent=2))
            return 0
        if args.operation == 'run' and args.proxy_mode == 'llm':
            if args.dry_run:
                print(json.dumps(frozen, indent=2))
                return 0
            if not args.output:
                raise ValueError('execution requires a fresh --output directory')
            results = run_live(frozen, args.output, routes=routes, live_budget=budget_value,
                               target_api_key=os.environ.get(args.target_api_key_env), rules_out=args.rules_out,
                               reviewer=args.reviewer == 'on', provider_pin=args.openrouter_provider,
                               target_api_key_env=args.target_api_key_env,
                               runtime_override=args.development_runtime)
            return 0 if results else 1
        if args.operation in ('run', 'frozen'):
            if args.operation == 'run':
                frozen['proxy']['mode'] = args.proxy_mode
            if args.dry_run:
                print(json.dumps(frozen, indent=2))
                return 0
            if not args.output:
                raise ValueError('execution requires a fresh --output directory')
            results = execute(frozen, args.output)
            return 0 if all(r.get('local_checks_passed') is True for r in results) else 1
        results = execute(frozen, args.output)
        return 0 if all(r.get('local_checks_passed') is True for r in results) else 1
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
