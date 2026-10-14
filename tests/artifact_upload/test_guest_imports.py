"""The guest image copies eval/artifact_upload but not the host instance lock; imports must not need it."""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_guest_session_imports_without_host_lock(tmp_path):
    shutil.copytree(ROOT / "eval/artifact_upload", tmp_path / "eval/artifact_upload")
    shutil.copytree(ROOT / "sandbox/artifact_upload/worker", tmp_path / "sandbox/artifact_upload/worker")
    for d in ("eval", "sandbox", "sandbox/artifact_upload"):
        (tmp_path / d / "__init__.py").write_text("")
    assert not (tmp_path / "project").exists()
    r = subprocess.run([sys.executable, "-E", "-c", "import sandbox.artifact_upload.worker.guest_session"],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
