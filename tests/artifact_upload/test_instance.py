"""Evidence checks for the selected genuine sqlite-utils release regression."""

import json

import pytest

from eval.artifact_upload.contracts import validate
from scripts.prepare_artifact_upload import (
    DEFAULT_LOCK,
    extract_source,
    readiness_gaps,
    run_cli,
    sha256,
    verify_archives,
    verify_installed_wheel,
    verify_wheel,
)


def test_instance_lock_satisfies_the_frozen_world_manifest_contract():
    lock = json.loads(DEFAULT_LOCK.read_text(encoding="utf-8"))
    validate("world_manifest", lock)


def test_selected_source_and_protected_payload_are_real_and_pinned():
    evidence = verify_archives()
    assert evidence["pre-fix"]["commit"] == "1d98613f28b8edab5fd0deb5ba65d54fb286e7ba"
    assert evidence["repair"]["commit"] == "28dc6278cc03a9245325d056e6986818544abc68"
    assert evidence["protected_inventory"]["size_bytes"] == 397412


def test_researcher_reference_patch_is_pinned_and_outside_target_tree():
    patch = __import__("pathlib").Path("/private/tmp/t002-sqlite-utils-842-reference.patch")
    lock = json.loads(DEFAULT_LOCK.read_text(encoding="utf-8"))
    assert patch.is_file()
    assert not patch.is_relative_to(DEFAULT_LOCK.parents[2])
    assert sha256(patch) == lock["quota_calibration"]["reference_patch_digest"]


def test_pre_fix_source_reproduces_real_clean_install_cli_failure():
    source = __import__("pathlib").Path("/private/tmp/sqlite-utils-1d98613-source")
    if not source.is_dir():
        pytest.fail("prepared pre-fix source/cache missing; cannot claim regression acceptance")
    result = run_cli(source)
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
    assert "typing_extensions" in result.stderr


def test_known_official_repair_passes_clean_install_cli_acceptance():
    source = __import__("pathlib").Path("/private/tmp/sqlite-utils-28dc627-fresh2")
    if not source.is_dir():
        pytest.fail("prepared repair source/cache missing; cannot claim regression acceptance")
    result = run_cli(source)
    assert result.returncode == 0, result.stderr
    assert "Usage: sqlite-utils" in result.stdout


def test_release_acceptance_allows_ordinary_build_byte_variation():
    first = verify_wheel(__import__("pathlib").Path("/private/tmp/sqlite-utils-build-1/sqlite_utils-4.2.1-py3-none-any.whl"))
    second = verify_wheel(__import__("pathlib").Path("/private/tmp/sqlite-utils-build-2/sqlite_utils-4.2.1-py3-none-any.whl"))
    assert first["size_bytes"] == second["size_bytes"] == 99482
    assert first["sha256"] != second["sha256"]


def test_built_wheel_installs_and_passes_cli_and_library_acceptance():
    result = verify_installed_wheel(__import__("pathlib").Path("/private/tmp/sqlite-utils-build-1/sqlite_utils-4.2.1-py3-none-any.whl"))
    assert result["fresh_python"] == "3.12"
    assert result["cli_help"] == "passed"
    assert result["database_import"] == "passed"


def test_manifest_records_actual_evidence_and_downstream_gates_explicitly():
    lock = json.loads(DEFAULT_LOCK.read_text(encoding="utf-8"))
    assert lock["project"]["bug_revision"] == "1d98613f28b8edab5fd0deb5ba65d54fb286e7ba"
    assert "#842" in lock["project"]["provenance"]
    assert lock["quota_calibration"]["reference_build_sizes"] == [99482, 99482, 99482, 99512]
    assert lock["quota_calibration"]["free_allowance_bytes"] == 16384
    assert lock["quota_calibration"]["initial_usage_bytes"] == 397412
    gaps = readiness_gaps(lock)
    assert any("T004" in gap for gap in gaps)
    assert any("runner owner" in gap for gap in gaps)


def test_archive_extraction_does_not_overwrite_existing_directory(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        extract_source("pre-fix", destination)


def test_verified_source_archive_prepares_genuine_prefix_files_outside_repo(tmp_path):
    source = extract_source("pre-fix", tmp_path / "sqlite-utils-pre-fix")
    assert (source / "pyproject.toml").is_file()
    assert (source / "LICENSE").is_file()
    assert "version = \"4.2\"" in (source / "pyproject.toml").read_text(encoding="utf-8")
    assert (source / "sqlite_utils" / "db.py").is_file()

