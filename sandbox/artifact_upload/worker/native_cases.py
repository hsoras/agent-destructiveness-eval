"""Differential fixture cases shared by the pinned native runner and the guest worker.

Every case is pure JSON data: a tool call plus an ordered ``setup`` list that
is applied to an emptied scratch project before the call.  ``{ROOT}`` is the
project directory (the tool cwd) and ``{OUT}`` a sibling directory outside it.
The same list is executed by the pinned OpenCode 1.14.41 executable and by the
guest worker; results and post-call filesystem snapshots are compared.
"""
from __future__ import annotations

import base64
from typing import Any

BOM = "\ufeff"
LONG_TIME = 1_700_000_000


def F(path: str, text: str = "", mtime: int | None = None) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [{"op": "write", "path": "{ROOT}/" + path, "text": text}]
    if mtime is not None:
        ops.append({"op": "utime", "path": "{ROOT}/" + path, "mtime": mtime})
    return ops


def FB(path: str, data: bytes, mtime: int | None = None) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [{"op": "write", "path": "{ROOT}/" + path, "b64": base64.b64encode(data).decode()}]
    if mtime is not None:
        ops.append({"op": "utime", "path": "{ROOT}/" + path, "mtime": mtime})
    return ops


def FO(path: str, text: str) -> list[dict[str, Any]]:
    return [{"op": "write", "path": "{OUT}/" + path, "text": text}]


def D(path: str) -> list[dict[str, Any]]:
    return [{"op": "mkdir", "path": "{ROOT}/" + path}]


def L(path: str, target: str) -> list[dict[str, Any]]:
    return [{"op": "symlink", "path": "{ROOT}/" + path, "target": target}]


def M(path: str, mode: int) -> list[dict[str, Any]]:
    return [{"op": "chmod", "path": "{ROOT}/" + path, "mode": mode}]


def case(cid: str, tool: str, args: dict[str, Any], *setup: list[dict[str, Any]], compare: str = "exact", variant: str | None = None) -> dict[str, Any]:
    ops = [op for group in setup for op in group]
    return {"id": cid, "tool": tool, "args": args, "setup": ops, "compare": compare, "variant": variant or ("edit" if tool in ("edit", "write") else "patch")}


def patch(*lines: str) -> str:
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"])


def _numbered(n: int, width: int = 1) -> str:
    return "".join(f"line{i:0{width}d}\n" for i in range(1, n + 1))


def read_cases() -> list[dict[str, Any]]:
    txt = F("a.txt", "one\ntwo\nthree\n")
    return [
        case("read-basic", "read", {"filePath": "a.txt"}, txt),
        case("read-absolute", "read", {"filePath": "{ROOT}/a.txt"}, txt),
        case("read-offset-0", "read", {"filePath": "a.txt", "offset": 0}, txt),
        case("read-offset-1", "read", {"filePath": "a.txt", "offset": 1, "limit": 1}, txt),
        case("read-offset-2", "read", {"filePath": "a.txt", "offset": 2}, txt),
        case("read-offset-last", "read", {"filePath": "a.txt", "offset": 3}, txt),
        case("read-offset-out-of-range", "read", {"filePath": "a.txt", "offset": 9}, txt),
        case("read-limit-zero", "read", {"filePath": "a.txt", "limit": 0}, txt),
        case("read-limit-two", "read", {"filePath": "a.txt", "limit": 2}, txt),
        case("read-empty-offset-1", "read", {"filePath": "e.txt", "offset": 1}, F("e.txt", "")),
        case("read-empty-offset-2", "read", {"filePath": "e.txt", "offset": 2}, F("e.txt", "")),
        case("read-blank-lines", "read", {"filePath": "b.txt"}, F("b.txt", "a\n\n\nb\n\n")),
        case("read-no-trailing-newline", "read", {"filePath": "n.txt"}, F("n.txt", "x\ny")),
        case("read-crlf", "read", {"filePath": "c.txt"}, F("c.txt", "one\r\ntwo\r\n")),
        case("read-lone-cr-and-unicode-separators", "read", {"filePath": "c.txt"}, F("c.txt", "one\rtwo\u2028three\x0bfour\nfive\n")),
        case("read-bom", "read", {"filePath": "bom.txt"}, F("bom.txt", BOM + "one\ntwo\n")),
        case("read-default-2000-lines", "read", {"filePath": "big.txt"}, F("big.txt", _numbered(2005, 4))),
        case("read-default-2000-lines-continue", "read", {"filePath": "big.txt", "offset": 2001}, F("big.txt", _numbered(2005, 4))),
        case("read-long-line-truncated", "read", {"filePath": "long.txt"}, F("long.txt", "x" * 2500 + "\nshort\n")),
        case("read-50kb-cap", "read", {"filePath": "wide.txt"}, F("wide.txt", ("y" * 99 + "\n") * 700)),
        case("read-invalid-utf8-text", "read", {"filePath": "latin.txt"}, FB("latin.txt", b"caf\xe9 ok\n\xff\xfe tail\n")),
        case("read-nul-binary", "read", {"filePath": "nul.txt"}, FB("nul.txt", b"abc\x00def\n")),
        case("read-nul-after-sample-window", "read", {"filePath": "late-nul.txt"}, FB("late-nul.txt", b"a" * 5000 + b"\x00tail\n")),
        case("read-binary-extension", "read", {"filePath": "x.bin"}, F("x.bin", "text content\n")),
        case("read-control-characters", "read", {"filePath": "ctl.txt"}, FB("ctl.txt", bytes([1, 2, 3, 4, 5, 6, 7, 8, 1, 2]))),
        case("read-directory", "read", {"filePath": "d"}, F("d/b.txt", "b"), F("d/A.txt", "a"), D("d/sub"), F("d/_u.txt", "u"), F("d/1.txt", "1"), F("d/.hid", "h"), F("d/Zed.txt", "z"), L("d/link", "sub"), L("d/flink", "b.txt")),
        case("read-directory-offset-limit", "read", {"filePath": "d", "offset": 2, "limit": 2}, F("d/a", "a"), F("d/b", "b"), F("d/c", "c"), F("d/d", "d")),
        case("read-directory-zero-limit", "read", {"filePath": "d", "limit": 0}, F("d/a", "a")),
        case("read-root-directory", "read", {"filePath": "."}, F("only.txt", "o")),
        case("read-missing", "read", {"filePath": "zzz.txt"}),
        case("read-missing-suggestions", "read", {"filePath": "notes.txt"}, F("notes.md", "n"), F("note", "n"), F("other", "o")),
        case("read-symlink-file", "read", {"filePath": "link.txt"}, F("real.txt", "target\n"), L("link.txt", "real.txt")),
        case("read-symlink-external", "read", {"filePath": "leak.txt"}, FO("secret.txt", "outside\n"), L("leak.txt", "{OUT}/secret.txt")),
        case("read-dangling-symlink", "read", {"filePath": "dead.txt"}, L("dead.txt", "nowhere")),
        case("read-parent-relative", "read", {"filePath": "d/../a.txt"}, txt, D("d")),
    ]


def edit_cases() -> list[dict[str, Any]]:
    base = F("f.txt", "alpha\nbeta\ngamma\n")
    return [
        case("edit-exact", "edit", {"filePath": "f.txt", "oldString": "beta", "newString": "BETA"}, base),
        case("edit-absolute", "edit", {"filePath": "{ROOT}/f.txt", "oldString": "beta", "newString": "BETA"}, base),
        case("edit-no-match", "edit", {"filePath": "f.txt", "oldString": "delta", "newString": "x"}, base),
        case("edit-multiple", "edit", {"filePath": "f.txt", "oldString": "a", "newString": "A"}, base),
        case("edit-replace-all", "edit", {"filePath": "f.txt", "oldString": "a", "newString": "A", "replaceAll": True}, base),
        case("edit-identical", "edit", {"filePath": "f.txt", "oldString": "beta", "newString": "beta"}, base),
        case("edit-empty-old-overwrite", "edit", {"filePath": "f.txt", "oldString": "", "newString": "fresh\n"}, base),
        case("edit-empty-old-create", "edit", {"filePath": "new/dir/f.txt", "oldString": "", "newString": "made\n"}),
        case("edit-empty-old-bom", "edit", {"filePath": "f.txt", "oldString": "", "newString": "plain\n"}, F("f.txt", BOM + "old\n")),
        case("edit-missing-file", "edit", {"filePath": "nope.txt", "oldString": "a", "newString": "b"}),
        case("edit-directory", "edit", {"filePath": "d", "oldString": "a", "newString": "b"}, D("d")),
        case("edit-bom-preserved", "edit", {"filePath": "f.txt", "oldString": "two", "newString": "2"}, F("f.txt", BOM + "one\ntwo\n")),
        case("edit-crlf-preserved", "edit", {"filePath": "f.txt", "oldString": "two\nthree", "newString": "2\n3"}, F("f.txt", "one\r\ntwo\r\nthree\r\n")),
        case("edit-crlf-old-string", "edit", {"filePath": "f.txt", "oldString": "two\r\nthree", "newString": "2\r\n3"}, F("f.txt", "one\ntwo\nthree\n")),
        case("edit-lone-cr-file", "edit", {"filePath": "f.txt", "oldString": "two", "newString": "2"}, F("f.txt", "one\rtwo\r")),
        case("edit-new-string-bom", "edit", {"filePath": "f.txt", "oldString": "one", "newString": BOM + "uno"}, F("f.txt", "one\n")),
        case("edit-readonly-file", "edit", {"filePath": "f.txt", "oldString": "one", "newString": "uno"}, F("f.txt", "one\n"), M("f.txt", 0o444)),
        case("edit-keeps-mode", "edit", {"filePath": "f.txt", "oldString": "one", "newString": "uno"}, F("f.txt", "one\n"), M("f.txt", 0o755)),
        case("edit-through-symlink", "edit", {"filePath": "link.txt", "oldString": "one", "newString": "uno"}, F("real.txt", "one\n"), L("link.txt", "real.txt")),
        case("edit-line-trimmed", "edit", {"filePath": "f.py", "oldString": "def f():\nreturn 1", "newString": "def f():\n    return 2"}, F("f.py", "  def f():\n      return 1  \n")),
        case("edit-block-anchor", "edit", {"filePath": "f.py", "oldString": "start\nmiddle wrong\nend", "newString": "start\nmid\nend"}, F("f.py", "start\nmiddle right\nend\n")),
        case("edit-whitespace-normalized", "edit", {"filePath": "f.py", "oldString": "a   b    c", "newString": "x"}, F("f.py", "a b c\n")),
        case("edit-indentation-flexible", "edit", {"filePath": "f.py", "oldString": "if x:\n    y()", "newString": "if z:\n    w()"}, F("f.py", "        if x:\n            y()\n")),
        case("edit-escape-normalized", "edit", {"filePath": "f.py", "oldString": "say \\\"hi\\\"\\n", "newString": "bye"}, F("f.py", "say \"hi\"\n\n")),
        case("edit-trimmed-boundary", "edit", {"filePath": "f.py", "oldString": "\n  mid  \n", "newString": "M"}, F("f.py", "a\nmid\nb\n")),
        case("edit-context-aware", "edit", {"filePath": "f.py", "oldString": "begin\nx1\nx2\nx3\nfinish", "newString": "replaced"}, F("f.py", "begin\nx1\nxx\nx3\nfinish\n")),
        case("edit-replace-all-dollar-patterns", "edit", {"filePath": "f.txt", "oldString": "beta", "newString": "[$&|$$|$`|$'|$1]", "replaceAll": True}, base),
        case("edit-replace-all-no-match", "edit", {"filePath": "f.txt", "oldString": "zz", "newString": "y", "replaceAll": True}, base),
        case("edit-failure-leaves-bytes", "edit", {"filePath": "f.txt", "oldString": "q", "newString": "y"}, FB("f.txt", b"\xef\xbb\xbfa\r\nb\r\n")),
        case("edit-unicode", "edit", {"filePath": "f.txt", "oldString": "caf\u00e9", "newString": "tea"}, F("f.txt", "caf\u00e9 \U0001f600\n")),
    ]


def write_cases() -> list[dict[str, Any]]:
    return [
        case("write-new", "write", {"filePath": "w.txt", "content": "hello\n"}),
        case("write-nested-dirs", "write", {"filePath": "a/b/c.txt", "content": "deep"}),
        case("write-absolute", "write", {"filePath": "{ROOT}/w.txt", "content": "abs"}),
        case("write-overwrite", "write", {"filePath": "w.txt", "content": "new\n"}, F("w.txt", "old\n")),
        case("write-empty", "write", {"filePath": "w.txt", "content": ""}, F("w.txt", "old\n")),
        case("write-bom-preserved", "write", {"filePath": "w.txt", "content": "plain\n"}, F("w.txt", BOM + "old\n")),
        case("write-bom-content", "write", {"filePath": "w.txt", "content": BOM + "marked\n"}),
        case("write-crlf-content", "write", {"filePath": "w.txt", "content": "a\r\nb\r\n"}),
        case("write-readonly", "write", {"filePath": "w.txt", "content": "new"}, F("w.txt", "old\n"), M("w.txt", 0o444)),
        case("write-keeps-mode", "write", {"filePath": "w.txt", "content": "new"}, F("w.txt", "old\n"), M("w.txt", 0o755)),
        case("write-directory", "write", {"filePath": "d", "content": "x"}, D("d")),
        case("write-through-symlink", "write", {"filePath": "link.txt", "content": "via"}, F("real.txt", "old"), L("link.txt", "real.txt")),
        case("write-parent-is-file", "write", {"filePath": "f/x.txt", "content": "x"}, F("f", "file")),
        case("write-unicode", "write", {"filePath": "w.txt", "content": "caf\u00e9 \U0001f600\n"}),
    ]


def patch_cases() -> list[dict[str, Any]]:
    a = F("a.txt", "alpha\nbeta\ngamma\n")
    return [
        case("patch-add", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+x", "+y")}),
        case("patch-add-nested", "apply_patch", {"patchText": patch("*** Add File: a/b/n.txt", "+x")}),
        case("patch-add-overwrites-existing", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+new")}, F("n.txt", "old\n")),
        case("patch-add-empty-contents", "apply_patch", {"patchText": patch("*** Add File: n.txt")}),
        case("patch-add-bom", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+" + BOM + "x")}),
        case("patch-add-ignores-non-plus-lines", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+x", "junk", "+y")}),
        case("patch-add-absolute", "apply_patch", {"patchText": patch("*** Add File: {ROOT}/n.txt", "+x")}),
        case("patch-add-dot-slash", "apply_patch", {"patchText": patch("*** Add File: ./n.txt", "+x")}),
        case("patch-update", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", " alpha", "-beta", "+BETA")}, a),
        case("patch-update-context-header", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@ beta", "-gamma", "+GAMMA")}, a),
        case("patch-update-missing-context", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@ nothere", "-gamma", "+GAMMA")}, a),
        case("patch-update-multi-chunk", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-alpha", "+ALPHA", "@@", "-gamma", "+GAMMA")}, a),
        case("patch-update-pure-addition", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "+tail")}, a),
        case("patch-update-eof-marker", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-gamma", "+GAMMA", "*** End of File")}, a),
        case("patch-update-no-match", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-nope", "+x")}, a),
        case("patch-update-rstrip-match", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-beta", "+B")}, F("a.txt", "alpha\nbeta   \ngamma\n")),
        case("patch-update-trim-match", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-beta", "+B")}, F("a.txt", "alpha\n   beta\ngamma\n")),
        case("patch-update-unicode-punctuation", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-it's", "+its")}, F("a.txt", "it\u2019s\nb\n")),
        case("patch-update-keeps-bom", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-b", "+B")}, F("a.txt", BOM + "a\nb\n")),
        case("patch-update-crlf-file", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-b", "+B")}, F("a.txt", "a\r\nb\r\nc\r\n")),
        case("patch-update-no-trailing-newline", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-b", "+B")}, F("a.txt", "a\nb")),
        case("patch-update-ignores-unknown-lines", "apply_patch", {"patchText": patch("*** Update File: a.txt", "stray", "@@", "-beta", "garbage", "+BETA")}, a),
        case("patch-update-no-chunks", "apply_patch", {"patchText": patch("*** Update File: a.txt")}, a),
        case("patch-update-missing-file", "apply_patch", {"patchText": patch("*** Update File: nope.txt", "@@", "-a", "+b")}),
        case("patch-update-directory", "apply_patch", {"patchText": patch("*** Update File: d", "@@", "-a", "+b")}, D("d")),
        case("patch-update-dot-slash", "apply_patch", {"patchText": patch("*** Update File: ./a.txt", "@@", "-beta", "+BETA")}, a),
        case("patch-update-absolute", "apply_patch", {"patchText": patch("*** Update File: {ROOT}/a.txt", "@@", "-beta", "+BETA")}, a),
        case("patch-move", "apply_patch", {"patchText": patch("*** Update File: a.txt", "*** Move to: moved/b.txt", "@@", "-beta", "+BETA")}, a),
        case("patch-move-no-chunks", "apply_patch", {"patchText": patch("*** Update File: a.txt", "*** Move to: b.txt")}, a),
        case("patch-move-over-existing", "apply_patch", {"patchText": patch("*** Update File: a.txt", "*** Move to: b.txt", "@@", "-beta", "+BETA")}, a, F("b.txt", "existing\n")),
        case("patch-delete", "apply_patch", {"patchText": patch("*** Delete File: a.txt")}, a),
        case("patch-delete-absolute", "apply_patch", {"patchText": patch("*** Delete File: {ROOT}/a.txt")}, a),
        case("patch-delete-dot-slash", "apply_patch", {"patchText": patch("*** Delete File: ./a.txt")}, a),
        case("patch-delete-missing", "apply_patch", {"patchText": patch("*** Delete File: nope.txt")}),
        case("patch-delete-directory", "apply_patch", {"patchText": patch("*** Delete File: d")}, D("d"), F("d/x", "x")),
        case("patch-add-then-delete-directory-leaves-state", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+n", "*** Delete File: d")}, D("d")),
        case("patch-update-then-failing-update-leaves-state", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-beta", "+BETA", "*** Update File: b.txt", "@@", "-nope", "+x")}, a, F("b.txt", "b\n")),
        case("patch-multi-file", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+n", "*** Update File: a.txt", "@@", "-beta", "+BETA", "*** Delete File: gone.txt")}, a, F("gone.txt", "g\n")),
        case("patch-in-git-worktree-relative-summary", "apply_patch", {"patchText": patch("*** Add File: sub/n.txt", "+x")}, [{"op": "git_init", "path": "{ROOT}", "commit": True}], compare="text"),
        case("patch-empty", "apply_patch", {"patchText": "*** Begin Patch\n*** End Patch"}),
        case("patch-no-hunks", "apply_patch", {"patchText": "*** Begin Patch\nnothing here\n*** End Patch"}),
        case("patch-missing-markers", "apply_patch", {"patchText": "*** Add File: n.txt\n+x"}),
        case("patch-missing-end-marker", "apply_patch", {"patchText": "*** Begin Patch\n*** Add File: n.txt\n+x"}),
        case("patch-heredoc-wrapper", "apply_patch", {"patchText": "cat <<'EOF'\n" + patch("*** Add File: n.txt", "+x") + "\nEOF"}),
        case("patch-crlf-patch-text", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+x").replace("\n", "\r\n")}),
        case("patch-leading-whitespace-text", "apply_patch", {"patchText": "\n\n  " + patch("*** Add File: n.txt", "+x") + "\n\n"}),
        case("patch-update-readonly-file-is-rejected-first", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+n", "*** Update File: a.txt", "@@", "-beta", "+BETA")}, a, M("a.txt", 0o444), compare="worker-stricter"),
        case("patch-add-where-directory-exists", "apply_patch", {"patchText": patch("*** Update File: a.txt", "@@", "-beta", "+BETA", "*** Add File: d", "+x")}, a, D("d"), compare="worker-stricter"),
        case("patch-duplicate-target", "apply_patch", {"patchText": patch("*** Add File: n.txt", "+one", "*** Add File: ./n.txt", "+two")}, compare="worker-stricter"),
    ]


def search_cases() -> list[dict[str, Any]]:
    tree = [F("src/a.py", "import os\nx = 1\n", LONG_TIME + 10), F("src/b.txt", "x here\n", LONG_TIME + 20), F("top.py", "x = 2\n", LONG_TIME + 30), F("src/deep/c.py", "x = 3\nX = 4\n", LONG_TIME + 40), F(".hidden.py", "x = 5\n", LONG_TIME + 50), F(".git/config", "x = 6\n", LONG_TIME + 60), F("node/x.js", "x\n", LONG_TIME + 5)]
    many = [F(f"m/f{i:03d}.txt", "hit\n", LONG_TIME + i) for i in range(130)]
    return [
        case("glob-recursive-py", "glob", {"pattern": "**/*.py"}, *tree),
        case("glob-basename-pattern", "glob", {"pattern": "*.py"}, *tree),
        case("glob-brace", "glob", {"pattern": "*.{py,txt}"}, *tree),
        case("glob-subdir-pattern", "glob", {"pattern": "src/*.py"}, *tree),
        case("glob-path-argument", "glob", {"pattern": "*.py", "path": "src"}, *tree),
        case("glob-absolute-path-argument", "glob", {"pattern": "*.py", "path": "{ROOT}/src"}, *tree),
        case("glob-negated", "glob", {"pattern": "!*.py"}, *tree),
        case("glob-no-match", "glob", {"pattern": "*.rs"}, *tree),
        case("glob-file-path-error", "glob", {"pattern": "*", "path": "top.py"}, *tree),
        case("glob-missing-path", "glob", {"pattern": "*", "path": "missing"}, *tree),
        case("glob-malformed-pattern", "glob", {"pattern": "[abc"}, *tree),
        case("glob-symlinks-not-followed", "glob", {"pattern": "**/*"}, F("real/r.txt", "r", LONG_TIME + 1), L("linkdir", "real"), L("linkfile", "real/r.txt"), FO("o.txt", "o")),
        case("glob-external-symlink-not-listed", "glob", {"pattern": "**/*.txt"}, F("a.txt", "a", LONG_TIME + 1), L("ext.txt", "{OUT}/o.txt"), L("extdir", "{OUT}"), FO("o.txt", "o")),
        case("glob-limit-100", "glob", {"pattern": "**/*.txt"}, *many, compare="count"),
        case("grep-basic", "grep", {"pattern": "x"}, *tree),
        case("grep-regex", "grep", {"pattern": "^x = \\d$"}, *tree),
        case("grep-include", "grep", {"pattern": "x", "include": "*.py"}, *tree),
        case("grep-include-brace", "grep", {"pattern": "x", "include": "*.{py,txt}"}, *tree),
        case("grep-path-dir", "grep", {"pattern": "x", "path": "src"}, *tree),
        case("grep-path-file", "grep", {"pattern": "x", "path": "src/b.txt"}, *tree),
        case("grep-no-match", "grep", {"pattern": "zzzz"}, *tree),
        case("grep-case-sensitive", "grep", {"pattern": "^X"}, *tree),
        case("grep-invalid-regex", "grep", {"pattern": "("}, *tree),
        case("grep-leading-dash-pattern", "grep", {"pattern": "-x"}, F("d.txt", "a -x b\n")),
        case("grep-missing-path", "grep", {"pattern": "x", "path": "missing"}, *tree),
        case("grep-binary-file-skipped", "grep", {"pattern": "needle"}, FB("bin.dat", b"needle\x00bytes\n", LONG_TIME + 1), F("t.txt", "needle\n", LONG_TIME + 2)),
        case("grep-long-line", "grep", {"pattern": "z"}, F("l.txt", "z" * 2300 + "\n")),
        case("grep-crlf-lines", "grep", {"pattern": "a"}, FB("c.txt", b"a1\r\na2\r\n")),
        case("grep-unicode", "grep", {"pattern": "caf\u00e9"}, F("u.txt", "caf\u00e9 au lait\n")),
        case("grep-symlinks-not-followed", "grep", {"pattern": "secret"}, FO("s.txt", "secret outside\n"), L("leak.txt", "{OUT}/s.txt"), L("leakdir", "{OUT}"), F("ok.txt", "secret inside\n")),
        case("grep-limit-100", "grep", {"pattern": "hit"}, *many, compare="count"),
        case("grep-mtime-ordering", "grep", {"pattern": "same"}, F("old.txt", "same\nsame\n", LONG_TIME), F("new.txt", "same\n", LONG_TIME + 100), F("mid.txt", "same\n", LONG_TIME + 50)),
    ]


def shell_cases() -> list[dict[str, Any]]:
    return [
        case("bash-echo", "bash", {"command": "echo hi", "description": "d"}),
        case("bash-no-output", "bash", {"command": "true", "description": "d"}),
        case("bash-quoting-pipeline-redirect", "bash", {"command": "printf '%s\\n' 'a b' \"c d\" | tr a-z A-Z > out.txt; cat out.txt", "description": "d"}),
        case("bash-nonzero-exit-output", "bash", {"command": "echo before; exit 3", "description": "d"}),
        case("bash-stderr-only", "bash", {"command": "echo oops >&2", "description": "d"}),
        case("bash-workdir-relative", "bash", {"command": "pwd", "workdir": "sub", "description": "d"}, D("sub")),
        case("bash-env-inherited", "bash", {"command": "echo $HOME_MARKER_UNSET-x", "description": "d"}),
        case("bash-timeout", "bash", {"command": "sleep 5", "timeout": 400, "description": "d"}, compare="timeout"),
        case("bash-large-output-tail", "bash", {"command": "seq 1 2500", "description": "d"}, compare="truncated"),
        case("bash-writes-visible-to-read", "bash", {"command": "printf 'v1\\n' > made.txt && cat made.txt", "description": "d"}),
    ]


def all_cases() -> dict[str, list[dict[str, Any]]]:
    cases = read_cases() + edit_cases() + write_cases() + patch_cases() + search_cases() + shell_cases()
    return {"patch": [c for c in cases if c["variant"] == "patch"], "edit": [c for c in cases if c["variant"] == "edit"]}
