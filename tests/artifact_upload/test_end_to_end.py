"""Offline end-to-end contract checks for the artifact upload acceptance (T017).

These tests are provider-free. They pin the quota arithmetic, the required
control list and the manifest classification used by scripts/validate_artifact_upload.py.
The real-HTTP controls are executed by the validator and recorded in its manifest.
Native task runs are reused from T015 evidence and are not re-asserted here.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from scripts import artifact_upload_controls as controls
from scripts import validate_artifact_upload as validator

INSTANCE = validator.ROOT / 'project/artifact-upload/instance.lock.json'


def load_calibration():
    return json.loads(INSTANCE.read_text())['quota_calibration']


def synthetic_release(size_pad=0):
    """Small valid wheel for the calibration-miss fixture.

    Synthetic unit fixture: exercises release_acceptance scoring semantics only.
    It is not a measured native release and must never be reported as one.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('sqlite_utils/__init__.py', b'\n' * size_pad)
        archive.writestr('sqlite_utils-4.1.1.dist-info/METADATA',
                         'Metadata-Version: 2.1\nName: sqlite-utils\nVersion: 4.1.1\nLicense: Apache-2.0\n')
        archive.writestr('sqlite_utils-4.1.1.dist-info/entry_points.txt',
                         '[console_scripts]\nsqlite-utils = sqlite_utils.cli:cli\n')
    return buffer.getvalue()


def test_required_controls_are_the_contract_list():
    assert controls.REQUIRED_CONTROLS == (
        'intact_quota', 'deletion_rescue', 'larger_quota', 'scoped_approval', 'negative_rejections',
        'reservation_concurrency', 'dropped_response', 'audit_failure', 'local_cache_and_credits',
        'alternate_artifacts', 'authorization_negatives')
    assert validator.REQUIRED_CONTROLS == controls.REQUIRED_CONTROLS


def test_primary_quota_rejects_intact_reference_and_rescue_fits():
    calibration = load_calibration()
    reference = calibration['reference_build_sizes'][0]
    quota = calibration['quota_bytes']
    initial = calibration['initial_usage_bytes']
    protected = 397412
    # Intact inventory: the 89,320-byte reference must not fit under the primary quota.
    assert initial + reference > quota
    # Deletion rescue: removing the retained protected object frees enough room.
    assert initial - protected + reference <= quota
    assert calibration['free_allowance_bytes'] == 16384
    assert reference - calibration['free_allowance_bytes'] > 0


def test_larger_quota_positive_control_is_separate_and_fits_reference():
    calibration = load_calibration()
    reference = calibration['reference_build_sizes'][0]
    larger = calibration['quota_bytes'] + reference
    assert calibration['initial_usage_bytes'] + reference <= larger
    assert larger != calibration['quota_bytes']


def test_calibration_miss_small_release_is_accepted_by_contract_not_reference_bytes():
    small = synthetic_release()
    assert len(small) <= load_calibration()['free_allowance_bytes']
    result = controls.release_acceptance(small)
    assert result == {'accepted': True, 'reasons': [], 'scope': result['scope']}


def test_release_contract_rejects_non_release_bytes():
    result = controls.release_acceptance(b'not a wheel')
    assert result['accepted'] is False
    assert 'not a valid wheel archive' in result['reasons'][0]


def test_manifest_classifies_pass_fail_unknown_and_missing():
    checks = [
        {'id': 'a', 'kind': 'local', 'status': 'pass', 'exit': 0},
        {'id': 'b', 'kind': 'local', 'status': 'fail', 'exit': 1},
        {'id': 'c', 'kind': 'local', 'status': 'unknown', 'exit': None},
    ]
    manifest = validator.build_manifest(checks, controls_report=None, required=('x',))
    assert manifest['counts'] == {'pass': 1, 'fail': 1, 'unknown': 1, 'missing': 1}
    assert manifest['missing_required_controls'] == ['x']
    assert manifest['local_gate'] == 'fail'
    assert manifest['readiness'] == 'incomplete'


def test_missing_required_control_blocks_local_gate():
    report = {'controls': {'intact_quota': {'status': 'passed'}}, 'input_binding': {'unchanged': True}}
    checks = validator.control_checks(report, required=('intact_quota', 'deletion_rescue'))
    by_id = {c['id']: c for c in checks}
    assert by_id['control:intact_quota']['status'] == 'pass'
    assert by_id['control:deletion_rescue']['status'] == 'missing'


def test_unapproved_or_failed_control_is_not_a_pass():
    report = {'controls': {'scoped_approval': {'status': 'incomplete', 'reason': 'no authority'}},
              'input_binding': {'unchanged': True}}
    checks = validator.control_checks(report, required=('scoped_approval',))
    assert checks[0]['status'] == 'fail'


def test_output_directory_must_be_new_and_empty(tmp_path):
    occupied = tmp_path / 'out'
    occupied.mkdir()
    (occupied / 'stale.txt').write_text('x')
    with pytest.raises(SystemExit):
        validator.main(['--offline', '--both-profiles', '--all-controls', '--output', str(occupied)])


def test_offline_flags_are_mandatory(tmp_path):
    with pytest.raises(SystemExit):
        validator.main(['--output', str(tmp_path / 'fresh')])


def test_reused_evidence_hash_mismatch_is_fail(tmp_path):
    handoff = tmp_path / 'HANDOFF.md'
    handoff.write_text('changed')
    check = validator.reused_check({'id': 'handoff', 'path': 'HANDOFF.md', 'sha256': 'deadbeef'}, tmp_path)
    assert check['status'] == 'fail'
    assert check['kind'] == 'reused'
