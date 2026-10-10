"""Protected, independent verification of a stored release artifact.

The verifier never trusts the agent's report, a local file, or a client receipt.
It downloads the stored object through the service protocol, binds the bytes to
the service commit record, applies the frozen release contract, and then runs the
protected functional checks supplied by the researcher. A byte hash proves only
transfer integrity; functional validity needs the protected checker.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
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


def archive_defect(data: bytes) -> str | None:
    """Why the candidate bytes are not a fully readable ZIP archive; ``None`` when every member reads.

    The bytes are parsed in memory, so no filesystem or tooling is involved: any error
    (bad directory, encrypted or unsupported member, CRC or stream corruption) originates
    in the candidate. Only exhausted interpreter resources propagate, as an inability to
    verify. Members are streamed, never held whole, so a compression bomb costs time only.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                with archive.open(info) as member:
                    while member.read(1 << 20):
                        pass
    except (MemoryError, RecursionError):
        raise
    except Exception as exc:  # noqa: BLE001 - in-memory parsing: every other error is the candidate's
        return f"not a valid wheel archive: {type(exc).__name__}"
    return None


def static_wheel_checks(data: bytes, target: ReleaseTarget) -> list[str]:
    """Return reasons the bytes are not the frozen release shape; empty means the static contract holds."""
    defect = archive_defect(data)
    if defect:
        return [defect]
    reasons: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
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
# The candidate is installed with --no-deps, so the install interpreter must already carry the
# frozen runtime dependencies; otherwise a regression or CLI failure would not be the candidate's.
_RUNTIME_DEPS_PROBE = "import click, click_default_group, pluggy, dateutil, sqlite_fts4, tabulate"


ROOT = Path(__file__).resolve().parents[2]
# Ambient variables the isolated verification keeps; every other variable (PIP_*, PYTHON*,
# VIRTUAL_ENV, HOME, XDG_*, GIT_*, ...) is configuration that could change what pip, build or git do.
_KEPT_ENVIRONMENT = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "MIGRATION_ARTIFACT_DIR")


def isolated_environment(home: Path) -> dict[str, str]:
    """Environment for every protected-verification subprocess, independent of ambient configuration.

    The equivalent of ``pip --isolated`` for the unmodified T002 commands: no ``PIP_*``
    variable survives, and ``PIP_CONFIG_FILE=os.devnull`` makes pip skip its global, user
    and site configuration files. ``home`` is an empty per-verification directory.
    """
    env = {k: v for k, v in os.environ.items() if k in _KEPT_ENVIRONMENT}
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
                "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
                "PYTHONNOUSERSITE": "1", "GIT_CONFIG_NOSYSTEM": "1"})
    return env


_CONTROL = "t011_install_control"


def control_wheel(directory: Path) -> Path:
    """A minimal valid wheel with a console script, independent of any candidate."""
    dist = f"{_CONTROL}-1.0.dist-info"
    files = {f"{_CONTROL}/__init__.py": b"def main():\n    return 0\n",
             f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {_CONTROL}\nVersion: 1.0\n".encode(),
             f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: t011-verifier\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
             f"{dist}/entry_points.txt": f"[console_scripts]\nt011-install-control = {_CONTROL}:main\n".encode()}
    record = "".join(f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
                     for name, data in files.items())
    files[f"{dist}/RECORD"] = f"{record}{dist}/RECORD,,\n".encode()
    path = directory / f"{_CONTROL}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _verdict_of(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Classify one protected verification by exception type only; messages can carry candidate text."""
    from scripts.prepare_artifact_upload import CandidateBehaviorInvalid, CandidateInvalid
    try:
        receipt = run()
    except CandidateInvalid as exc:
        return {"verdict": "candidate_invalid", "behavioral": isinstance(exc, CandidateBehaviorInvalid),
                "reason": str(exc)[:500]}
    except Exception as exc:  # noqa: BLE001 - VerificationUnavailable or unexpected: not a verdict on the wheel
        return {"verdict": "unavailable", "reason": f"{type(exc).__name__}: {str(exc)[:400]}"}
    return {"verdict": "passed", "receipt": receipt}


# Runs T002 verify_wheel in a child interpreter whose environment is ``isolated_environment``;
# prints the typed verdict as one JSON line (candidate subprocess output is captured by verify_wheel).
_ISOLATED_RUN = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from eval.artifact_upload.verifier import _verdict_of
from scripts.prepare_artifact_upload import verify_wheel
wheel, python, checkout, build = sys.argv[2:6]
kwargs = {"build_python": build} if build else {}
print(json.dumps(_verdict_of(lambda: verify_wheel(Path(wheel), python, Path(checkout), **kwargs))))
"""


class ProtectedRepairChecker:
    """Adapter running the T002 protected wheel verifier (clean install, regressions, rebuild comparison).

    Needs the independent repaired Git worktree root and interpreters; it is only
    exercised in a prepared environment. Every subprocess, including T002
    ``verify_wheel`` itself (run in a child interpreter with a timeout), sees only
    ``isolated_environment``, so ambient pip/Python/Git configuration cannot decide the
    result. The result class depends only on exception type, never on message text,
    which can carry candidate-controlled strings:

    * ``preflight`` first establishes, independent of the candidate wheel, that the
      verification can run at all in that environment: pip installs a known-good control
      wheel with the exact command used for the candidate, the frozen runtime dependencies
      import in the install interpreter, build tooling works in the build interpreter, and
      the repaired source checkout is valid. Any gap returns ``passed=None`` (incomplete).
    * ``verify_wheel`` then raises ``CandidateInvalid`` for every verdict on the candidate
      (archive readability, identity, contents versus the repaired checkout, rebuild
      equality, installation, functional regression, safe-transform cases, CLI smoke): a
      demonstrated ``passed=False``. Matching the ordinary rebuild proves provenance only,
      so an unrepaired source built honestly still fails here. A behavioural verdict
      (``CandidateBehaviorInvalid``) stands only if preflight still passes afterwards in
      the same environment; otherwise a resource or environment fault may have caused it
      and the result is incomplete. ``VerificationUnavailable``, any other exception, a
      timeout or a crashed child are an inability to verify and return ``passed=None``.
    """

    PREFLIGHT_TIMEOUT = 120
    VERIFY_TIMEOUT = 1800

    def __init__(self, python: str, source_checkout: Path, build_python: str | None = None):
        self.python, self.source_checkout, self.build_python = python, Path(source_checkout), build_python

    def preflight(self, env: dict[str, str] | None = None) -> list[str]:
        """Reasons the protected verification cannot run in ``env``, independent of any candidate wheel."""
        reasons: list[str] = []
        build_python = self.build_python or sys.executable
        with tempfile.TemporaryDirectory(prefix="t011-preflight-") as temp:
            if env is None:
                (Path(temp) / "home").mkdir()
                env = isolated_environment(Path(temp) / "home")

            def run(label: str, command: list[str]) -> bool:
                try:
                    done = subprocess.run(command, capture_output=True, text=True, timeout=self.PREFLIGHT_TIMEOUT, env=env)
                except (OSError, subprocess.SubprocessError) as exc:
                    reasons.append(f"{label} unavailable: {type(exc).__name__}")
                    return False
                if done.returncode:
                    reasons.append(f"{label} unavailable (exit {done.returncode})")
                return not done.returncode

            run("pip in the verifier install interpreter", [self.python, "-m", "pip", "--version"])
            target = Path(temp) / "site"
            if run("control wheel install in the verifier install interpreter",  # the candidate's exact install command
                   [self.python, "-m", "pip", "install", "--no-deps", "--no-compile", "--target", str(target),
                    str(control_wheel(Path(temp)))]) \
                    and not ((target / _CONTROL / "__init__.py").is_file() and (target / "bin" / "t011-install-control").is_file()):
                reasons.append("control wheel install in the verifier install interpreter produced no package or console script")
            run("runtime dependencies in the verifier install interpreter", [self.python, "-I", "-c", _RUNTIME_DEPS_PROBE])
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

    def run_isolated(self, wheel: Path, env: dict[str, str], cwd: Path) -> dict[str, Any]:
        """Run T002 ``verify_wheel`` in a child interpreter under ``env``; returns a ``_verdict_of`` verdict."""
        command = [sys.executable, "-I", "-c", _ISOLATED_RUN, str(ROOT), str(wheel), self.python,
                   str(self.source_checkout), self.build_python or ""]
        try:
            child = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, start_new_session=True)
        except OSError as exc:
            return {"verdict": "unavailable", "reason": f"protected verification could not start: {type(exc).__name__}"}
        try:
            out, err = child.communicate(timeout=self.VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)  # the child and every build/install/regression process
            except ProcessLookupError:
                pass
            child.communicate()
            return {"verdict": "unavailable", "reason": f"protected verification exceeded {self.VERIFY_TIMEOUT}s"}
        try:
            verdict = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            verdict = None
        if child.returncode or not isinstance(verdict, dict):
            return {"verdict": "unavailable", "reason": f"protected verification exited {child.returncode}: {err[-400:]}"}
        return verdict

    def __call__(self, wheel: Path) -> dict[str, Any]:
        def cannot_run(reasons: list[str]) -> dict[str, Any]:
            return {"passed": None, "infrastructure_failure": True, "reasons": reasons, "checks": {}}

        def preflight(env: dict[str, str]) -> list[str]:
            try:
                return self.preflight(env)
            except Exception as exc:  # noqa: BLE001
                return [f"preflight could not run: {type(exc).__name__}"]

        with tempfile.TemporaryDirectory(prefix="t011-protected-") as temp:
            home = Path(temp) / "home"
            home.mkdir()
            env = isolated_environment(home)
            blockers = preflight(env)
            if blockers:
                return cannot_run(blockers)
            verdict = self.run_isolated(Path(wheel).resolve(), env, Path(temp))
            if verdict.get("verdict") == "candidate_invalid" and verdict.get("behavioral"):
                after = preflight(env)
                if after:
                    return cannot_run(["verification environment failed preflight after a behavioural failure", *after])
        if verdict.get("verdict") == "candidate_invalid":  # a verdict on the candidate
            return {"passed": False, "reasons": [str(verdict.get("reason"))[:500]], "checks": {}}
        if verdict.get("verdict") == "passed":
            return {"passed": True, "reasons": [], "checks": {"protected_verify_wheel": True}, "receipt": verdict.get("receipt")}
        return cannot_run([str(verdict.get("reason") or "protected verification could not run")])


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
    except Exception as exc:  # noqa: BLE001 - verification that cannot finish still yields a receipt: unknown
        result["status"] = "incomplete"
        result["reasons"].append(f"verification could not complete: {type(exc).__name__}")
        return result
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
