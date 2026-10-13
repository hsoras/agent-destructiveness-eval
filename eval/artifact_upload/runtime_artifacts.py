"""Explicit development artifacts; never substitutes for measurement pins.

The live-preview caller must opt in by constructing this object from a build
receipt and passing it as ``runtime=``. PinnedRuntime remains independent.

Canonical API: ``DevelopmentRuntime.from_receipt(path)`` returns a verified
object exposing ``binary``, ``plugin_seed``, ``verify()``, ``expected_sha256``,
``version`` and ``provenance``. Receipt schema 1 records source identity/tree,
patch digests, Bun digest, build command/flags, channel, metadata snapshot and
binary digest. Only explicit LLM live-preview integration may use this artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess

from .host_adapter import RuntimeUnavailable

DEVELOPMENT_VERSION = "1.14.41"
DEVELOPMENT_SOURCE_COMMIT = "8ba2a9171597262df9d19516c82a5e14f18f5c63"


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class DevelopmentRuntime:
    binary: Path
    plugin_seed: Path
    expected_build_sha256: str
    source_commit: str
    source_tree_sha256: str
    receipt: Path
    artifact_class: str = "development-nonmeasurement"

    @property
    def expected_sha256(self) -> str:
        return self.expected_build_sha256

    @property
    def version(self) -> str:
        return DEVELOPMENT_VERSION

    @property
    def provenance(self) -> dict[str, object]:
        """JSON-safe identity for checkpoints and live-preview manifests."""
        return {"artifact_class": self.artifact_class, "version": self.version,
                "binary": str(self.binary), "binary_sha256": self.expected_sha256,
                "source_commit": self.source_commit, "source_tree_sha256": self.source_tree_sha256,
                "plugin_seed": str(self.plugin_seed), "build_receipt": str(self.receipt)}

    @classmethod
    def from_receipt(cls, receipt: str | Path) -> "DevelopmentRuntime":
        path = Path(receipt).resolve()
        try:
            data = json.loads(path.read_text())
            if (data["schema_version"] != 1 or data["status"] != "built"
                    or data["artifact_class"] != "development-nonmeasurement"
                    or data["version"] != DEVELOPMENT_VERSION or data["channel"] != "latest"):
                raise ValueError("receipt is not a completed 1.14.41 development build")
            runtime = cls(Path(data["binary"]), Path(data["plugin_seed"]), data["binary_sha256"],
                          data["source_commit"], data["patched_source_tree_sha256"], path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeUnavailable(f"invalid development build receipt {path}: {exc}") from exc
        return runtime.verify()

    def verify(self) -> "DevelopmentRuntime":
        if self.artifact_class != "development-nonmeasurement" or self.source_commit != DEVELOPMENT_SOURCE_COMMIT:
            raise RuntimeUnavailable("development runtime has unexpected source identity or artifact class")
        for digest in (self.expected_build_sha256, self.source_tree_sha256):
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise RuntimeUnavailable("development runtime requires explicit SHA-256 provenance")
        if not self.binary.is_file() or _sha256(self.binary) != self.expected_build_sha256:
            raise RuntimeUnavailable(f"development OpenCode binary missing or digest mismatch: {self.binary}")
        try:
            result = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeUnavailable(f"development OpenCode version probe failed: {exc}") from exc
        if result.stdout.strip() != DEVELOPMENT_VERSION:
            raise RuntimeUnavailable(f"unexpected development OpenCode version {result.stdout.strip()!r}")
        for package, expected in (("@opencode-ai/plugin", DEVELOPMENT_VERSION), ("zod", None)):
            manifest = self.plugin_seed / "node_modules" / package / "package.json"
            try:
                data = json.loads(manifest.read_text())
                if not data.get("version") or (expected and data["version"] != expected):
                    raise ValueError("version mismatch")
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeUnavailable(f"invalid offline development prerequisite {package}: {exc}") from exc
        return self
