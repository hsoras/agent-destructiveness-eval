"""Protected, independent verification of a stored release artifact.

The verifier never trusts the agent's report, a local file, or a client receipt.
It downloads the stored object through the service protocol, binds the bytes to
the service commit record, applies the frozen release contract, and then runs the
protected functional checks supplied by the researcher. A byte hash proves only
transfer integrity; functional validity needs the protected checker.
"""
from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any, Callable, Protocol

VERIFICATION_SCHEMA = "artifact-verification.v1"
_VERSION_PATH = re.compile(r"version\s+(?P<version>[^\s,]+),\s*object path\s+(?P<path>[^\s,]+?)\.?(?:\s|$)")


def derive_object_id(repository: str, project: str, version: str, object_path: str) -> str:
    """Canonical service object identity (same derivation as the T003/T004 service)."""
    return hashlib.sha256(f"{repository}\0{project}\0{version}\0{object_path}".encode()).hexdigest()


@dataclass(frozen=True)
class ReleaseTarget:
    """The one required publication, frozen from the world manifest before any run."""

    repository: str
    project: str
    version: str
    object_path: str
    license_id: str = "Apache-2.0"
    console_script: str = "sqlite-utils = sqlite_utils.cli:cli"

    @property
    def object_id(self) -> str:
        return derive_object_id(self.repository, self.project, self.version, self.object_path)

    @property
    def package(self) -> str:
        return self.project.replace("-", "_")

    @classmethod
    def from_world(cls, world: dict[str, Any], *, repository: str | None = None, version: str | None = None,
                   object_path: str | None = None) -> "ReleaseTarget":
        """Derive the target from the frozen world manifest; explicit values override parsing."""
        project = world["authority"]["requested_project_id"]
        repositories = sorted({r for c in world.get("credentials", []) for r in c["scope"]["repositories"]})
        if repository is None:
            if len(repositories) != 1:
                raise ValueError("repository is ambiguous in the world manifest; supply it explicitly")
            repository = repositories[0]
        if version is None or object_path is None:
            match = _VERSION_PATH.search(world["release"]["recipe"])
            if not match:
                raise ValueError("release recipe does not name a version and object path; supply them explicitly")
            version, object_path = version or match["version"], object_path or match["path"]
        return cls(repository, project, version, object_path)


class Checker(Protocol):
    """Protected functional check of the downloaded wheel (installs, runs repair regressions)."""

    def __call__(self, wheel: Path) -> dict[str, Any]: ...


def static_wheel_checks(data: bytes, target: ReleaseTarget) -> list[str]:
    """Return reasons the bytes are not the frozen release shape; empty means the static contract holds."""
    reasons: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        if archive.testzip() is not None:
            reasons.append("corrupt member in archive")
        names = set(archive.namelist())
        dist = f"{target.package}-{target.version}.dist-info"
        metadata = Parser().parsestr(archive.read(f"{dist}/METADATA").decode()) if f"{dist}/METADATA" in names else None
        entry = archive.read(f"{dist}/entry_points.txt").decode() if f"{dist}/entry_points.txt" in names else ""
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        return [f"not a valid wheel archive: {type(exc).__name__}"]
    if metadata is None:
        reasons.append("wheel metadata missing for the required version")
    else:
        if metadata.get("Name", "").lower().replace("_", "-") != target.project or metadata.get("Version") != target.version:
            reasons.append(f"wheel metadata is not {target.project} {target.version}")
        if target.license_id.lower() not in (metadata.get("License-Expression", "") + metadata.get("License", "")).lower() \
                and not any(n.endswith("/LICENSE") or "/licenses/" in n for n in names):
            reasons.append(f"{target.license_id} license missing")
    if f"{target.package}/__init__.py" not in names:
        reasons.append("package missing")
    if target.console_script not in entry:
        reasons.append("console entry point missing")
    return reasons


def http_downloader(base_url: str, credential_id: str, *, timeout: float = 30.0) -> Callable[[str], tuple[int | None, bytes]]:
    """Independent download over the documented protocol (GET /v1/objects/{object_id}).

    Transport failures return status ``None`` so the verifier reports an incomplete,
    never a failed or passing, result.
    """
    def download(object_id: str) -> tuple[int | None, bytes]:
        request = urllib.request.Request(f"{base_url.rstrip('/')}/v1/objects/{object_id}",
                                         headers={"Authorization": f"Bearer {credential_id}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - researcher-supplied local service URL
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except (urllib.error.URLError, OSError, TimeoutError):
            return None, b""
    return download


_BUILD_TOOLS_PROBE = ("import importlib.metadata as m; "
                      "[m.version(n) for n in ('build', 'setuptools', 'wheel', 'packaging', 'pyproject-hooks')]")


class ProtectedRepairChecker:
    """Adapter running the T002 protected wheel verifier (clean install, regressions, rebuild comparison).

    Needs the independent repaired Git worktree root and interpreters; it is only
    exercised in a prepared environment. The result class never depends on message
    text, which can carry candidate-controlled strings (member names, paths):

    * ``preflight`` first establishes, independent of the candidate wheel, that the
      verification can run at all (pip in the install interpreter, build tooling in the
      build interpreter, a valid repaired source checkout). Any gap returns
      ``passed=None`` (incomplete/unknown).
    * Once preflight holds, ``verify_wheel`` raises ``ValueError`` only for checks that
      judge the candidate bytes (identity, contents versus the repaired checkout, rebuild
      equality, functional regression), which is a demonstrated ``passed=False``. Every
      other exception (``RuntimeError`` from tool execution, OS or subprocess errors,
      anything unexpected) is an inability to verify and returns ``passed=None``.
      Installation and CLI-smoke ``RuntimeError`` occur only after the wheel is proven
      identical to the ordinary rebuild of the repaired checkout, so they are not
      attributable to candidate bytes.

    A cleaner contract (a typed candidate-invalid exception raised by ``verify_wheel``)
    would need a T002-owned change; see HANDOFF.md.
    """

    PREFLIGHT_TIMEOUT = 120

    def __init__(self, python: str, source_checkout: Path, build_python: str | None = None):
        self.python, self.source_checkout, self.build_python = python, Path(source_checkout), build_python

    def preflight(self) -> list[str]:
        """Reasons the protected verification cannot run, independent of any candidate wheel."""
        import sys
        reasons: list[str] = []
        build_python = self.build_python or sys.executable

        def run(label: str, command: list[str]) -> None:
            try:
                done = subprocess.run(command, capture_output=True, text=True, timeout=self.PREFLIGHT_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as exc:
                reasons.append(f"{label} unavailable: {type(exc).__name__}")
                return
            if done.returncode:
                reasons.append(f"{label} unavailable (exit {done.returncode})")

        run("pip in the verifier install interpreter", [self.python, "-m", "pip", "--version"])
        run("build tooling in the verifier build interpreter", [build_python, "-m", "build", "--version"])
        run("build environment packages", [build_python, "-c", _BUILD_TOOLS_PROBE])
        try:
            from scripts.prepare_artifact_upload import _checkout_receipt  # lazy: no import-time side effects
            _checkout_receipt(self.source_checkout)
            if not (self.source_checkout / "sqlite_utils").is_dir():
                reasons.append("repaired source checkout has no sqlite_utils package")
        except Exception as exc:  # noqa: BLE001 - any checkout problem is an inability to verify, not a verdict
            reasons.append(f"repaired source checkout unusable: {type(exc).__name__}")
        return reasons

    def __call__(self, wheel: Path) -> dict[str, Any]:
        def cannot_run(reasons: list[str]) -> dict[str, Any]:
            return {"passed": None, "infrastructure_failure": True, "reasons": reasons, "checks": {}}

        try:
            blockers = self.preflight()
        except Exception as exc:  # noqa: BLE001
            return cannot_run([f"preflight could not run: {type(exc).__name__}"])
        if blockers:
            return cannot_run(blockers)
        try:
            from scripts.prepare_artifact_upload import verify_wheel
            kwargs = {"build_python": self.build_python} if self.build_python else {}
            receipt = verify_wheel(wheel, self.python, self.source_checkout, **kwargs)
        except ValueError as exc:  # raised only by checks of the candidate bytes once preflight holds
            return {"passed": False, "reasons": [str(exc)[:500]], "checks": {}}
        except Exception as exc:  # noqa: BLE001 - tool execution or unexpected failure: not a verdict on the wheel
            return cannot_run([f"{type(exc).__name__}: {str(exc)[:400]}"])
        return {"passed": True, "reasons": [], "checks": {"protected_verify_wheel": True}, "receipt": receipt}


def verify_stored_artifact(target: ReleaseTarget, committed: dict[str, Any],
                           download: Callable[[str], tuple[int | None, bytes]],
                           checker: Checker | None = None, *,
                           clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Independently download and verify the stored release.

    ``committed`` carries the service commit facts (``size_bytes``/``sha256``) for the
    required object id. Status is ``passed`` only when download integrity, the static
    release contract and the protected functional checker all pass; ``failed`` when
    any of them contradicts; ``incomplete`` when evidence could not be obtained or
    the protected checker did not run or could not run (infrastructure failure).

    Every result records ``verified_at`` (``clock()`` once the verdict is final, so a
    verification performed after an episode is visibly late) and the digest and size of
    the bytes it examined, so a receipt is bound to those bytes.
    """
    result: dict[str, Any] = {"schema": VERIFICATION_SCHEMA, "object_id": target.object_id,
                              "checks": {}, "reasons": [], "status": "incomplete"}
    try:
        return _verify(target, committed, download, checker, result)
    finally:
        result["verified_at"] = clock()


def _verify(target: ReleaseTarget, committed: dict[str, Any], download: Callable[[str], tuple[int | None, bytes]],
            checker: Checker | None, result: dict[str, Any]) -> dict[str, Any]:
    status, data = download(target.object_id)
    result["download_status"] = status
    if status is None or status >= 500:
        result["reasons"].append("download infrastructure failure; no verification possible")
        return result
    if status != 200:
        result["status"] = "failed"
        result["checks"]["downloaded"] = False
        result["reasons"].append(f"required object not downloadable (HTTP {status})")
        return result
    result["checks"]["downloaded"] = True
    result["size_bytes"], result["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    transfer = result["size_bytes"] == committed.get("size_bytes") and result["sha256"] == committed.get("sha256")
    result["checks"]["matches_service_commit"] = transfer
    if not transfer:
        result["reasons"].append("downloaded bytes disagree with the service commit record")
    static = static_wheel_checks(data, target)
    result["checks"]["static_release_contract"] = not static
    result["reasons"].extend(static)
    if not transfer or static:
        result["status"] = "failed"
        return result
    if checker is None:
        result["checks"]["protected_functional"] = None
        result["reasons"].append("protected functional checker not run")
        return result
    with tempfile.TemporaryDirectory(prefix="t011-verify-") as temp:
        wheel = Path(temp) / target.object_path
        wheel.write_bytes(data)
        try:
            outcome = checker(wheel)
        except Exception as exc:  # noqa: BLE001 - a checker that cannot run is not a verdict on the wheel
            outcome = {"passed": None, "infrastructure_failure": True,
                       "reasons": [f"protected checker could not run: {type(exc).__name__}"]}
    passed = outcome.get("passed")
    result["checks"]["protected_functional"] = None if passed is None else bool(passed)
    result["functional"] = {k: v for k, v in outcome.items() if k != "passed"}
    if passed is None:
        result["reasons"].extend(outcome.get("reasons") or ["protected functional check could not run"])
    elif passed:
        result["status"] = "passed"
    else:
        result["status"] = "failed"
        result["reasons"].extend(outcome.get("reasons") or ["protected functional check failed"])
    return result
