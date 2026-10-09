"""Reproduce the pinned OpenCode source digests directly from its official tarball."""
from __future__ import annotations

import hashlib
import json
import os
import tarfile
from pathlib import Path

ARCHIVE_SHA256 = "e8ad49328a79947855d3d6b6b3a7f40554c4037eab947246ae5dc144304ec84d"
FULL_TREE_SHA256 = "4be4862ac664888b3c4d408db8b9211d2dfc24fee582c24ef4eb9ded7256e04e"
OPENCODE_SUBTREE_SHA256 = "b370fb50940e5acb427f30f816c31083429444d9c12dacf7ff5272621df7b430"


def canonical_digest(archive: Path, prefix: str = "") -> tuple[str, int, int]:
    """Hash sorted archive members after removing its one top-level directory."""
    entries: list[tuple[str, str, tarfile.TarInfo]] = []
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            relative = member.name.split("/", 1)[1] if "/" in member.name else ""
            if not relative or (prefix and not relative.startswith(prefix)):
                continue
            if member.isfile():
                entries.append(("file", relative, member))
            elif member.issym():
                entries.append(("symlink", relative, member))
        digest = hashlib.sha256()
        regular_count = symlink_count = 0
        for kind, relative, member in sorted(entries, key=lambda entry: entry[1]):
            path = relative.encode("utf-8")
            if kind == "file":
                payload = source.extractfile(member)
                if payload is None:
                    raise ValueError(f"Cannot read archive member {member.name}")
                record = hashlib.sha256(payload.read()).hexdigest().encode("ascii")
                digest.update(b"file\0" + path + b"\0" + record + b"\n")
                regular_count += 1
            else:
                digest.update(b"symlink\0" + path + b"\0" + member.linkname.encode("utf-8") + b"\n")
                symlink_count += 1
    return digest.hexdigest(), regular_count, symlink_count


def verify(archive: Path) -> dict[str, object]:
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    if archive_hash != ARCHIVE_SHA256:
        raise ValueError(f"Pinned archive SHA-256 mismatch: {archive_hash}")
    full = canonical_digest(archive)
    subtree = canonical_digest(archive, "packages/opencode/")
    expected = ((FULL_TREE_SHA256, 4474, 62), (OPENCODE_SUBTREE_SHA256, 899, 1))
    if full != expected[0] or subtree != expected[1]:
        raise ValueError(f"Pinned source digest mismatch: full={full}, subtree={subtree}")
    return {"archive_sha256": archive_hash, "full_tree_sha256": full[0], "full_tree_files": full[1], "full_tree_symlinks": full[2], "opencode_subtree_sha256": subtree[0], "opencode_subtree_files": subtree[1], "opencode_subtree_symlinks": subtree[2]}


if __name__ == "__main__":
    archive = Path(os.environ.get("OPENCODE_SOURCE_ARCHIVE", "/private/tmp/opencode-8ba2a917.tar.gz"))
    print(json.dumps(verify(archive), sort_keys=True))
