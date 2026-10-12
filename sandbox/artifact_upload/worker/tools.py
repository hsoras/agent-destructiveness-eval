"""Python 3.11 guest ports of the pinned OpenCode 1.14.41 file tools.

Semantics follow packages/opencode/src/tool/{read,edit,write,glob,grep,
apply_patch}.ts and src/patch/index.ts at commit 8ba2a917.  Failures raise
ToolError carrying the native message text.  Optional hooks (LSP, formatters,
permission prompts, bus events) are disabled and declared in DISABLED_FEATURES.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable

# ---- privilege split: the worker may run as root; everything the agent drives runs as the agent uid/gid.
AGENT_UID = int(os.environ.get("ARTIFACT_AGENT_UID", "1000"))
AGENT_GID = int(os.environ.get("ARTIFACT_AGENT_GID", str(AGENT_UID)))


def privileged() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def spawn_kwargs() -> dict:
    """subprocess.Popen/run arguments that drop an agent-driven child to the agent identity (empty when not root)."""
    return {"user": AGENT_UID, "group": AGENT_GID, "extra_groups": []} if privileged() else {}


@contextlib.contextmanager
def as_agent():
    """Perform in-process file access with the agent's filesystem identity (per thread; no-op when not root).

    Changing the fsuid from 0 clears the filesystem override capabilities, so the worker cannot read
    root-only paths on the agent's behalf.
    """
    if not privileged():
        yield
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.setfsgid(AGENT_GID); libc.setfsuid(AGENT_UID)
    try:
        yield
    finally:
        libc.setfsuid(0); libc.setfsgid(0)


DISABLED_FEATURES = ("lsp", "automatic_formatters", "persistent_terminals", "command_expansion", "delegation")
DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_LINE_SUFFIX = f"... (line truncated to {MAX_LINE_LENGTH} chars)"
MAX_BYTES = 50 * 1024
MAX_LINES = 2000
SAMPLE_BYTES = 4096
BOM = "\ufeff"
BINARY_EXTENSIONS = frozenset(".zip .tar .gz .exe .dll .so .class .jar .war .7z .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp .bin .dat .obj .o .a .lib .wasm .pyc .pyo".split())
JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
WS_RE = re.compile("[" + re.escape(JS_WS) + "]+")


class ToolError(Exception):
    def __init__(self, message: str, code: str = "tool_failed"):
        super().__init__(message)
        self.code = code


def fs_error(exc: OSError, method: str, path: str) -> ToolError:
    reason = {errno.ENOENT: "NotFound", errno.EACCES: "PermissionDenied", errno.EPERM: "PermissionDenied", errno.EEXIST: "AlreadyExists",
              errno.EISDIR: "BadResource", errno.ENOTDIR: "BadResource", errno.ENOTEMPTY: "BadResource", errno.EINVAL: "BadResource", errno.EBADF: "BadResource"}.get(exc.errno, "Unknown")
    code = {"NotFound": "not_found", "PermissionDenied": "permission_denied"}.get(reason, "filesystem_error")
    return ToolError(f"{reason}: FileSystem.{method} ({path})", code)


def js_trim(s: str) -> str:
    return s.strip(JS_WS)


def js_trim_end(s: str) -> str:
    return s.rstrip(JS_WS)


def utf16_len(s: str) -> int:
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


def utf16_prefix(s: str, n: int) -> str:
    out, used = [], 0
    for c in s:
        w = 2 if ord(c) > 0xFFFF else 1
        if used + w > n:
            break
        out.append(c); used += w
    return "".join(out)


def decode_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def bom_split(text: str) -> tuple[bool, str]:
    return (True, text[1:]) if text[:1] == BOM else (False, text)


def bom_join(text: str, bom: bool) -> str:
    stripped = bom_split(text)[1]
    return BOM + stripped if bom else stripped


class Ctx:
    """Resolved per-call environment (cwd, boundary checks, spool dir)."""

    def __init__(self, cwd: str, check: Callable[[str], None], output_dir: str, rg: str | None):
        self.cwd, self.check, self.output_dir, self.rg = cwd, check, output_dir, rg

    def resolve(self, p: str) -> str:
        full = os.path.normpath(p if os.path.isabs(p) else os.path.join(self.cwd, p))
        self.check(full)
        return full

    def worktree(self) -> str:
        # Observed against the pinned executable (see the git-worktree fixture):
        # summaries are relative to "/" even inside a committed git repository.
        return "/"

    def spool(self, text: str) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        path = os.path.join(self.output_dir, "tool_" + uuid.uuid4().hex)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


# ---------------------------------------------------------------- helpers

_PUNCT = "_-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"


def locale_key(name: str):
    def primary(c: str):
        if c.isspace(): return (0, 0)
        if c in _PUNCT: return (1, _PUNCT.index(c))
        if c.isdigit(): return (2, ord(c))
        return (3, ord(c.lower()) if c.isascii() else ord(c) + 0x10000)
    return ([primary(c) for c in name], [c.isupper() for c in name])


def read_file_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise fs_error(e, "readFile", path) from e


def bom_read_file(path: str) -> tuple[bool, str]:
    return bom_split(decode_utf8(read_file_bytes(path)))


def write_with_dirs(path: str, text: str) -> None:
    data = text.encode("utf-8", errors="replace")
    try:
        with open(path, "wb") as f:
            f.write(data)
    except FileNotFoundError:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as e:
            raise fs_error(e, "makeDirectory", os.path.dirname(path)) from e
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            raise fs_error(e, "writeFile", path) from e
    except OSError as e:
        raise fs_error(e, "writeFile", path) from e


def truncate_output(ctx: Ctx, text: str) -> str:
    lines = text.split("\n")
    total = len(text.encode("utf-8"))
    if len(lines) <= MAX_LINES and total <= MAX_BYTES:
        return text
    out, size, hit = [], 0, False
    for i, line in enumerate(lines):
        if i >= MAX_LINES: break
        add = len(line.encode("utf-8")) + (1 if i > 0 else 0)
        if size + add > MAX_BYTES:
            hit = True; break
        out.append(line); size += add
    removed = total - size if hit else len(lines) - len(out)
    unit = "bytes" if hit else "lines"
    file = ctx.spool(text)
    hint = f"The tool call succeeded but the output was truncated. Full output saved to: {file}\nUse Grep to search the full content or Read with offset/limit to view specific sections."
    return "\n".join(out) + f"\n\n...{removed} {unit} truncated...\n\n{hint}"


# ------------------------------------------------------------------- read

def _is_binary(path: str, sample: bytes) -> bool:
    if os.path.splitext(path)[1].lower() in BINARY_EXTENSIONS: return True
    if not sample: return False
    nonprint = 0
    for b in sample:
        if b == 0: return True
        if b < 9 or 13 < b < 32: nonprint += 1
    return nonprint / len(sample) > 0.3


def _sniff_attachment(sample: bytes) -> bool:
    return sample.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"%PDF-")) or (sample[:4] == b"RIFF" and sample[8:12] == b"WEBP")


_LINE_SPLIT = re.compile(r"\r\n|\n|\r")


def split_lines(text: str) -> list[str]:
    if text == "": return []
    parts = _LINE_SPLIT.split(text)
    if parts and parts[-1] == "": parts.pop()
    return parts


def read(ctx: Ctx, a: dict) -> str:
    filepath = ctx.resolve(a["filePath"])
    try:
        st = os.stat(filepath)
    except FileNotFoundError:
        base = os.path.basename(filepath); d = os.path.dirname(filepath)
        try:
            items = [os.path.join(d, i) for i in os.listdir(d) if i.lower() in base.lower() or base.lower() in i.lower()][:3]
        except OSError:
            items = []
        if items: raise ToolError(f"File not found: {filepath}\n\nDid you mean one of these?\n" + "\n".join(items), "not_found")
        raise ToolError(f"File not found: {filepath}", "not_found")
    except OSError as e:
        raise fs_error(e, "stat", filepath) from e
    import stat as statmod
    if statmod.S_ISDIR(st.st_mode):
        try:
            entries = os.listdir(filepath)
        except OSError as e:
            raise fs_error(e, "readDirectory", filepath) from e
        items = []
        for name in entries:
            full = os.path.join(filepath, name)
            items.append(name + "/" if os.path.isdir(full) else name)
        items.sort(key=locale_key)
        limit = a.get("limit", DEFAULT_READ_LIMIT)
        offset = a.get("offset") or 1
        start = offset - 1
        sliced = items[start:start + limit]
        truncated = start + len(sliced) < len(items)
        tail = f"\n(Showing {len(sliced)} of {len(items)} entries. Use 'offset' parameter to read beyond entry {offset + len(sliced)})" if truncated else f"\n({len(items)} entries)"
        return "\n".join([f"<path>{filepath}</path>", "<type>directory</type>", "<entries>", "\n".join(sliced), tail, "</entries>"])
    try:
        with open(filepath, "rb") as f:
            sample = f.read(min(SAMPLE_BYTES, st.st_size)) if st.st_size else b""
    except OSError as e:
        raise fs_error(e, "open", filepath) from e
    if _sniff_attachment(sample):
        raise ToolError("Image and PDF attachments are mediated outside the guest worker", "unsupported_attachment")
    if _is_binary(filepath, sample):
        raise ToolError(f"Cannot read binary file: {filepath}", "binary_file")
    limit = a.get("limit", DEFAULT_READ_LIMIT)
    offset = a.get("offset") or 1
    start = offset - 1
    raw: list[str] = []; size_total = 0; count = 0; cut = False; more = False
    for text in split_lines(decode_utf8(read_file_bytes(filepath))):
        count += 1
        if count <= start: continue
        if len(raw) >= limit:
            more = True; continue
        line = utf16_prefix(text, MAX_LINE_LENGTH) + MAX_LINE_SUFFIX if utf16_len(text) > MAX_LINE_LENGTH else text
        size = len(line.encode("utf-8")) + (1 if raw else 0)
        if size_total + size > MAX_BYTES:
            cut = True; more = True; break
        raw.append(line); size_total += size
    if count < offset and not (count == 0 and offset == 1):
        raise ToolError(f"Offset {offset} is out of range for this file ({count} lines)", "read_failed")
    out = "\n".join([f"<path>{filepath}</path>", "<type>file</type>", "<content>\n"])
    out += "\n".join(f"{i + offset}: {line}" for i, line in enumerate(raw))
    last = offset + len(raw) - 1
    nxt = last + 1
    if cut: out += f"\n\n(Output capped at {MAX_BYTES // 1024} KB. Showing lines {offset}-{last}. Use offset={nxt} to continue.)"
    elif more: out += f"\n\n(Showing lines {offset}-{last} of {count}. Use offset={nxt} to continue.)"
    else: out += f"\n\n(End of file - total {count} lines)"
    return out + "\n</content>"


# ------------------------------------------------------------- edit/write

def _normalize_le(t: str) -> str: return t.replace("\r\n", "\n")
def _to_le(t: str, ending: str) -> str: return t if ending == "\n" else t.replace("\n", "\r\n")


def _levenshtein(a: str, b: str) -> int:
    if a == "" or b == "": return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1))
        prev = cur
    return prev[len(b)]


def _line_span(lines: list[str], start: int, end: int) -> tuple[int, int]:
    s = sum(len(lines[k]) + 1 for k in range(start))
    e = s + sum(len(lines[k]) for k in range(start, end + 1)) + (end - start)
    return s, e


def simple_replacer(content, find):
    yield find


def line_trimmed_replacer(content, find):
    ol = content.split("\n"); sl = find.split("\n")
    if sl and sl[-1] == "": sl.pop()
    for i in range(0, len(ol) - len(sl) + 1):
        if all(js_trim(ol[i + j]) == js_trim(sl[j]) for j in range(len(sl))):
            s, e = _line_span(ol, i, i + len(sl) - 1)
            yield content[s:e]


def block_anchor_replacer(content, find):
    ol = content.split("\n"); sl = find.split("\n")
    if len(sl) < 3: return
    if sl[-1] == "": sl.pop()
    first, last = js_trim(sl[0]), js_trim(sl[-1]); size = len(sl)
    cands = []
    for i in range(len(ol)):
        if js_trim(ol[i]) != first: continue
        for j in range(i + 2, len(ol)):
            if js_trim(ol[j]) == last:
                cands.append((i, j)); break
    if not cands: return
    def span(c):
        s, e = _line_span(ol, c[0], c[1]); return content[s:e]
    if len(cands) == 1:
        sline, eline = cands[0]; actual = eline - sline + 1; sim = 0.0
        check = min(size - 2, actual - 2)
        if check > 0:
            j = 1
            while j < size - 1 and j < actual - 1:
                o, s_ = js_trim(ol[sline + j]), js_trim(sl[j]); mx = max(utf16_len(o), utf16_len(s_))
                if mx != 0:
                    sim += (1 - _levenshtein(o, s_) / mx) / check
                    if sim >= 0.0: break
                j += 1
        else:
            sim = 1.0
        if sim >= 0.0: yield span(cands[0])
        return
    best, best_sim = None, -1.0
    for c in cands:
        sline, eline = c; actual = eline - sline + 1; sim = 0.0
        check = min(size - 2, actual - 2)
        if check > 0:
            j = 1
            while j < size - 1 and j < actual - 1:
                o, s_ = js_trim(ol[sline + j]), js_trim(sl[j]); mx = max(utf16_len(o), utf16_len(s_))
                if mx != 0: sim += 1 - _levenshtein(o, s_) / mx
                j += 1
            sim /= check
        else:
            sim = 1.0
        if sim > best_sim: best_sim, best = sim, c
    if best_sim >= 0.3 and best: yield span(best)


def whitespace_normalized_replacer(content, find):
    norm = lambda t: js_trim(WS_RE.sub(" ", t))
    nf = norm(find); lines = content.split("\n")
    for line in lines:
        if norm(line) == nf:
            yield line
        elif nf in norm(line):
            words = re.split(WS_RE, js_trim(find))
            if words:
                pattern = r"\s+".join(re.escape(w) for w in words)
                try:
                    m = re.search(pattern.replace(r"\s+", "[" + re.escape(JS_WS) + "]+"), line)
                except re.error:
                    m = None
                if m: yield m.group(0)
    fl = find.split("\n")
    if len(fl) > 1:
        for i in range(0, len(lines) - len(fl) + 1):
            block = lines[i:i + len(fl)]
            if norm("\n".join(block)) == nf: yield "\n".join(block)


def indentation_flexible_replacer(content, find):
    def strip(t):
        lines = t.split("\n"); ne = [l for l in lines if js_trim(l)]
        if not ne: return t
        mi = min(len(l) - len(l.lstrip(JS_WS)) for l in ne)
        return "\n".join(l if not js_trim(l) else l[mi:] for l in lines)
    nf = strip(find); cl = content.split("\n"); fl = find.split("\n")
    for i in range(0, len(cl) - len(fl) + 1):
        block = "\n".join(cl[i:i + len(fl)])
        if strip(block) == nf: yield block


_ESC = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "`": "`", "\\": "\\", "\n": "\n", "$": "$"}


def _unescape(s: str) -> str:
    return re.sub(r"\\(n|t|r|'|\"|`|\\|\n|\$)", lambda m: _ESC[m.group(1)], s)


def escape_normalized_replacer(content, find):
    uf = _unescape(find)
    if uf in content: yield uf
    lines = content.split("\n"); fl = uf.split("\n")
    for i in range(0, len(lines) - len(fl) + 1):
        block = "\n".join(lines[i:i + len(fl)])
        if _unescape(block) == uf: yield block


def multi_occurrence_replacer(content, find):
    start = 0
    while True:
        idx = content.find(find, start)
        if idx == -1: break
        yield find
        start = idx + len(find)


def trimmed_boundary_replacer(content, find):
    tf = js_trim(find)
    if tf == find: return
    if tf in content: yield tf
    lines = content.split("\n"); fl = find.split("\n")
    for i in range(0, len(lines) - len(fl) + 1):
        block = "\n".join(lines[i:i + len(fl)])
        if js_trim(block) == tf: yield block


def context_aware_replacer(content, find):
    fl = find.split("\n")
    if len(fl) < 3: return
    if fl[-1] == "": fl.pop()
    cl = content.split("\n"); first, last = js_trim(fl[0]), js_trim(fl[-1])
    for i in range(len(cl)):
        if js_trim(cl[i]) != first: continue
        for j in range(i + 2, len(cl)):
            if js_trim(cl[j]) == last:
                block_lines = cl[i:j + 1]
                if len(block_lines) == len(fl):
                    match = total = 0
                    for k in range(1, len(block_lines) - 1):
                        b, f = js_trim(block_lines[k]), js_trim(fl[k])
                        if len(b) > 0 or len(f) > 0:
                            total += 1
                            if b == f: match += 1
                    if total == 0 or match / total >= 0.5:
                        yield "\n".join(block_lines)
                        break
                break


REPLACERS = (simple_replacer, line_trimmed_replacer, block_anchor_replacer, whitespace_normalized_replacer, indentation_flexible_replacer,
             escape_normalized_replacer, trimmed_boundary_replacer, context_aware_replacer, multi_occurrence_replacer)


def _js_replace_all(content: str, search: str, repl: str) -> str:
    """String.prototype.replaceAll with a string pattern, including $ patterns."""
    out, pos = [], 0
    while True:
        idx = content.find(search, pos)
        if idx == -1: break
        out.append(content[pos:idx])
        r, i = [], 0
        while i < len(repl):
            c = repl[i]
            if c == "$" and i + 1 < len(repl):
                n = repl[i + 1]
                if n == "$": r.append("$"); i += 2; continue
                if n == "&": r.append(search); i += 2; continue
                if n == "`": r.append(content[:idx]); i += 2; continue
                if n == "'": r.append(content[idx + len(search):]); i += 2; continue
            r.append(c); i += 1
        out.append("".join(r))
        pos = idx + len(search)
        if search == "": break
    out.append(content[pos:])
    return "".join(out)


def replace_text(content: str, old: str, new: str, replace_all: bool = False) -> str:
    if old == new: raise ToolError("No changes to apply: oldString and newString are identical.", "edit_failed")
    not_found = True
    for replacer in REPLACERS:
        for search in replacer(content, old):
            index = content.find(search)
            if index == -1: continue
            not_found = False
            if replace_all: return _js_replace_all(content, search, new)
            if index != content.rfind(search): continue
            return content[:index] + new + content[index + len(search):]
    if not_found:
        raise ToolError("Could not find oldString in the file. It must match exactly, including whitespace, indentation, and line endings.", "edit_failed")
    raise ToolError("Found multiple matches for oldString. Provide more surrounding context to make the match unique.", "edit_failed")


def edit(ctx: Ctx, a: dict) -> str:
    if not a["filePath"]: raise ToolError("filePath is required", "edit_failed")
    old, new = a["oldString"], a["newString"]
    if old == new: raise ToolError("No changes to apply: oldString and newString are identical.", "edit_failed")
    path = ctx.resolve(a["filePath"])
    if old == "":
        existed = _exists(path)
        bom, text = bom_read_file(path) if existed else (False, "")
        nbom, ntext = bom_split(new)
        write_with_dirs(path, bom_join(ntext, bom or nbom))
        return "Edit applied successfully."
    try:
        st = os.stat(path)
    except OSError:
        raise ToolError(f"File {path} not found", "edit_failed")
    import stat as statmod
    if statmod.S_ISDIR(st.st_mode): raise ToolError(f"Path is a directory, not a file: {path}", "edit_failed")
    bom, text = bom_read_file(path)
    ending = "\r\n" if "\r\n" in text else "\n"
    o = _to_le(_normalize_le(old), ending); n = _to_le(_normalize_le(new), ending)
    nbom, ntext = bom_split(replace_text(text, o, n, a.get("replaceAll", False)))
    write_with_dirs(path, bom_join(ntext, bom or nbom))
    return "Edit applied successfully."


def _exists(path: str) -> bool:
    try:
        os.stat(path); return True
    except OSError:
        return False


def write(ctx: Ctx, a: dict) -> str:
    path = ctx.resolve(a["filePath"])
    exists = _exists(path)
    bom, _ = bom_read_file(path) if exists else (False, "")
    nbom, ntext = bom_split(a["content"])
    write_with_dirs(path, bom_join(ntext, bom or nbom))
    return "Wrote file successfully."


# -------------------------------------------------------------- search

def rg_env() -> dict:
    env = {k: v for k, v in os.environ.items() if v is not None}
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


def _need_rg(ctx: Ctx) -> str:
    if not ctx.rg: raise ToolError("ripgrep (rg) is required in the guest for glob and grep", "ripgrep_missing")
    return ctx.rg


def _clean(p: str) -> str:
    return os.path.normpath(re.sub(r"^\.[\\/]", "", p))


def _mtime_ms(path: str) -> float:
    try: return os.stat(path).st_mtime * 1000
    except OSError: return 0


def glob(ctx: Ctx, a: dict) -> str:
    search = a.get("path") or ctx.cwd
    search = ctx.resolve(search)
    if os.path.isfile(search): raise ToolError(f"glob path must be a directory: {search}", "find_failed")
    if not os.path.isdir(search): raise ToolError(f"No such file or directory: '{search}'", "find_failed")
    rg = _need_rg(ctx)
    p = subprocess.run([rg, "--no-config", "--files", "--glob=!.git/*", "--hidden", f"--glob={a['pattern']}", "."], cwd=search, env=rg_env(), stdin=subprocess.DEVNULL, capture_output=True, **spawn_kwargs())
    if p.returncode not in (0, 1):
        raise ToolError(p.stderr.decode("utf-8", "replace").strip() or f"ripgrep failed with code {p.returncode}", "find_failed")
    files = []
    for line in p.stdout.decode("utf-8", "replace").split("\n"):
        if not line: continue
        full = os.path.normpath(os.path.join(search, _clean(line)))
        try:
            ctx.check(full)
        except ToolError:
            continue
        files.append((full, _mtime_ms(full)))
        if len(files) >= 101: break
    limit = 100; truncated = len(files) > limit
    if truncated: files = files[:limit]
    files.sort(key=lambda f: -f[1])
    out = []
    if not files: out.append("No files found")
    else:
        out.extend(f[0] for f in files)
        if truncated:
            out.append("")
            out.append(f"(Results are truncated: showing first {limit} results. Consider using a more specific path or pattern.)")
    return "\n".join(out)


def _real(p: str) -> str:
    try: return os.path.realpath(p)
    except OSError: return p


def grep(ctx: Ctx, a: dict) -> str:
    if not a["pattern"]: raise ToolError("pattern is required", "grep_failed")
    given = a.get("path") or "."
    search = _real(os.path.normpath(given if os.path.isabs(given) else os.path.join(ctx.cwd, given)))
    ctx.check(search)
    isdir = os.path.isdir(search)
    cwd = search if isdir else os.path.dirname(search)
    file = None if isdir else [os.path.relpath(search, cwd)]
    rg = _need_rg(ctx)
    cmd = [rg, "--no-config", "--json", "--hidden", "--glob=!.git/*", "--no-messages"]
    if a.get("include"): cmd.append(f"--glob={a['include']}")
    cmd += ["--", a["pattern"], *(file or ["."])]
    if not os.path.isdir(cwd): return "No files found"
    p = subprocess.run(cmd, cwd=cwd, env=rg_env(), stdin=subprocess.DEVNULL, capture_output=True, **spawn_kwargs())
    if p.returncode not in (0, 1, 2):
        raise ToolError(p.stderr.decode("utf-8", "replace").strip() or f"ripgrep failed with code {p.returncode}", "grep_failed")
    items, partial = [], p.returncode == 2
    if p.returncode != 1:
        for line in p.stdout.decode("utf-8", "replace").split("\n"):
            if not line: continue
            try: obj = json.loads(line)
            except ValueError as e: raise ToolError("invalid ripgrep output", "grep_failed") from e
            if obj.get("type") != "match": continue
            d = obj["data"]
            if "text" not in d.get("path", {}) or "text" not in d.get("lines", {}):
                raise ToolError("invalid ripgrep output", "grep_failed")
            items.append((d["path"]["text"], d["line_number"], d["lines"]["text"]))
    if not items: return "No files found"
    rows = []
    for ptxt, num, text in items:
        cleaned = _clean(ptxt)
        full = _real(cleaned if os.path.isabs(cleaned) else os.path.join(cwd, cleaned))
        try: ctx.check(full)
        except ToolError: continue
        rows.append((full, num, text))
    times = {}
    for full in dict.fromkeys(r[0] for r in rows):
        if os.path.isfile(full) or (os.path.exists(full) and not os.path.isdir(full)): times[full] = _mtime_ms(full)
    matches = [(r, times[r[0]]) for r in rows if r[0] in times]
    matches.sort(key=lambda m: -m[1])
    limit = 100; truncated = len(matches) > limit
    final = matches[:limit] if truncated else matches
    if not final: return "No files found"
    total = len(matches)
    out = [f"Found {total} matches" + (f" (showing first {limit})" if truncated else "")]
    current = ""
    for (full, num, text), _ in final:
        if current != full:
            if current != "": out.append("")
            current = full
            out.append(f"{full}:")
        t = utf16_prefix(text, MAX_LINE_LENGTH) + "..." if utf16_len(text) > MAX_LINE_LENGTH else text
        out.append(f"  Line {num}: {t}")
    if truncated:
        out.append("")
        out.append(f"(Results truncated: showing {limit} of {total} matches ({total - limit} hidden). Consider using a more specific path or pattern.)")
    if partial:
        out.append("")
        out.append("(Some paths were inaccessible and skipped)")
    return "\n".join(out)


# --------------------------------------------------------------- patch

def _strip_heredoc(s: str) -> str:
    m = re.match(r"^(?:cat\s+)?<<['\"]?(\w+)['\"]?\s*\n([\s\S]*?)\n\1\s*$", s)
    return m.group(2) if m else s


def parse_patch(text: str) -> list[dict]:
    lines = _strip_heredoc(js_trim(text)).split("\n")
    begin = next((i for i, l in enumerate(lines) if js_trim(l) == "*** Begin Patch"), -1)
    end = next((i for i, l in enumerate(lines) if js_trim(l) == "*** End Patch"), -1)
    if begin == -1 or end == -1 or begin >= end: raise ToolError("Error: Invalid patch format: missing Begin/End markers", "patch_failed")
    hunks, i = [], begin + 1

    def header(idx):
        line = lines[idx]
        for tag in ("*** Add File:", "*** Delete File:"):
            if line.startswith(tag):
                p = js_trim(line[len(tag):]); return (p, None, idx + 1) if p else None
        if line.startswith("*** Update File:"):
            p = js_trim(line[len("*** Update File:"):]); move = None; nxt = idx + 1
            if nxt < len(lines) and lines[nxt].startswith("*** Move to:"):
                move = js_trim(lines[nxt][len("*** Move to:"):]); nxt += 1
            return (p, move, nxt) if p else None
        return None

    while i < end:
        h = header(i)
        if not h:
            i += 1; continue
        path, move, nxt = h
        if lines[i].startswith("*** Add File:"):
            content, j = "", nxt
            while j < len(lines) and not lines[j].startswith("***"):
                if lines[j].startswith("+"): content += lines[j][1:] + "\n"
                j += 1
            if content.endswith("\n"): content = content[:-1]
            hunks.append({"type": "add", "path": path, "contents": content}); i = j
        elif lines[i].startswith("*** Delete File:"):
            hunks.append({"type": "delete", "path": path}); i = nxt
        else:
            chunks, j = [], nxt
            while j < len(lines) and not lines[j].startswith("***"):
                if lines[j].startswith("@@"):
                    ctxline = js_trim(lines[j][2:]); j += 1
                    old, new, eof = [], [], False
                    while j < len(lines) and not lines[j].startswith("@@") and not lines[j].startswith("***"):
                        cl = lines[j]
                        if cl == "*** End of File":
                            eof = True; j += 1; break
                        if cl.startswith(" "): old.append(cl[1:]); new.append(cl[1:])
                        elif cl.startswith("-"): old.append(cl[1:])
                        elif cl.startswith("+"): new.append(cl[1:])
                        j += 1
                    chunks.append({"old": old, "new": new, "ctx": ctxline or None, "eof": eof})
                else:
                    j += 1
            hunks.append({"type": "update", "path": path, "move": move, "chunks": chunks}); i = j
    return hunks


def _norm_unicode(s: str) -> str:
    s = re.sub("[\u2018\u2019\u201a\u201b]", "'", s)
    s = re.sub("[\u201c\u201d\u201e\u201f]", '"', s)
    s = re.sub("[\u2010\u2011\u2012\u2013\u2014\u2015]", "-", s)
    return s.replace("\u2026", "...").replace("\u00a0", " ")


def _try_match(lines, pattern, start, cmp, eof):
    if eof:
        fe = len(lines) - len(pattern)
        if fe >= start and all(cmp(lines[fe + j], pattern[j]) for j in range(len(pattern))): return fe
    for i in range(start, len(lines) - len(pattern) + 1):
        if all(cmp(lines[i + j], pattern[j]) for j in range(len(pattern))): return i
    return -1


def _seek(lines, pattern, start, eof=False):
    if not pattern: return -1
    for cmp in (lambda a, b: a == b, lambda a, b: js_trim_end(a) == js_trim_end(b), lambda a, b: js_trim(a) == js_trim(b),
                lambda a, b: _norm_unicode(js_trim(a)) == _norm_unicode(js_trim(b))):
        r = _try_match(lines, pattern, start, cmp, eof)
        if r != -1: return r
    return -1


def derive_new_contents(path: str, chunks: list[dict]) -> tuple[str, bool]:
    try:
        with open(path, "rb") as f: raw = decode_utf8(f.read())
    except OSError as e:
        raise ToolError(f"Error: Failed to read file {path}: {e}", "patch_failed") from e
    obom, otext = bom_split(raw)
    lines = otext.split("\n")
    if lines and lines[-1] == "": lines.pop()
    reps, idx = [], 0
    for ch in chunks:
        if ch["ctx"]:
            ci = _seek(lines, [ch["ctx"]], idx)
            if ci == -1: raise ToolError(f"Error: Failed to find context '{ch['ctx']}' in {path}", "patch_failed")
            idx = ci + 1
        if not ch["old"]:
            ins = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
            reps.append((ins, 0, ch["new"])); continue
        pattern, new_slice = ch["old"], ch["new"]
        found = _seek(lines, pattern, idx, ch["eof"])
        if found == -1 and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "": new_slice = new_slice[:-1]
            found = _seek(lines, pattern, idx, ch["eof"])
        if found == -1:
            raise ToolError(f"Error: Failed to find expected lines in {path}:\n" + "\n".join(ch["old"]), "patch_failed")
        reps.append((found, len(pattern), new_slice)); idx = found + len(pattern)
    reps.sort(key=lambda r: r[0])
    result = list(lines)
    for start, oldlen, seg in reversed(reps):
        result[start:start + oldlen] = seg
    if not result or result[-1] != "": result.append("")
    nbom, ntext = bom_split("\n".join(result))
    return ntext, obom or nbom


class Journal:
    """Pre-mutation snapshots so any late failure restores the exact prior state."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, bytes | None, int | None]] = []
        self.made_dirs: list[str] = []

    def remember(self, path: str) -> None:
        try:
            st = os.lstat(path)
            if os.path.islink(path):
                self.entries.append((path, "symlink", os.fsencode(os.readlink(path)), st.st_mode & 0o7777))
                target = os.path.realpath(path)
                if os.path.exists(target): self.remember(target)
            else:
                with open(path, "rb") as f: self.entries.append((path, "file", f.read(), st.st_mode & 0o7777))
        except FileNotFoundError:
            self.entries.append((path, "missing", None, None))

    def makedirs(self, directory: str) -> None:
        missing, cur = [], directory
        while cur and not os.path.exists(cur):
            missing.append(cur); cur = os.path.dirname(cur)
        os.makedirs(directory, exist_ok=True)
        self.made_dirs.extend(missing)

    def rollback(self) -> None:
        for path, kind, data, mode in reversed(self.entries):
            try:
                if kind == "missing":
                    if os.path.lexists(path): os.unlink(path)
                elif kind == "symlink":
                    if os.path.lexists(path): os.unlink(path)
                    os.symlink(os.fsdecode(data), path)
                else:
                    with open(path, "wb") as f: f.write(data)
                    os.chmod(path, mode)
            except OSError:
                pass
        for d in self.made_dirs:
            try: os.rmdir(d)
            except OSError: pass


def _nearest_existing(path: str) -> str:
    cur = path
    while not os.path.lexists(cur):
        cur = os.path.dirname(cur)
    return cur


def _preflight_write(path: str) -> None:
    if os.path.isdir(path): raise ToolError(f"apply_patch verification failed: BadResource: FileSystem.writeFile ({path})", "patch_failed")
    if os.path.lexists(path):
        if not os.access(path, os.W_OK): raise ToolError(f"apply_patch verification failed: PermissionDenied: FileSystem.writeFile ({path})", "permission_denied")
        return
    anc = _nearest_existing(os.path.dirname(path))
    if not os.path.isdir(anc) or not os.access(anc, os.W_OK | os.X_OK):
        raise ToolError(f"apply_patch verification failed: PermissionDenied: FileSystem.writeFile ({path})", "permission_denied")


def _target_identity(path: str) -> str:
    """Canonicalize existing aliases without forbidding ordinary guest symlinks."""
    return os.path.realpath(os.path.abspath(path))


def _same_target(left: str, right: str) -> bool:
    if _target_identity(left) == _target_identity(right): return True
    try: return os.path.samefile(left, right)
    except (FileNotFoundError, NotADirectoryError, OSError): return False


def _overlap(left: str, right: str) -> bool:
    a, b = _target_identity(left), _target_identity(right)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


def apply_patch(ctx: Ctx, a: dict) -> str:
    text = a["patchText"]
    if not text: raise ToolError("patchText is required", "patch_failed")
    try:
        hunks = parse_patch(text)
    except ToolError as e:
        raise ToolError(f"apply_patch verification failed: {e}", "patch_failed") from e
    if not hunks:
        if js_trim(text.replace("\r\n", "\n").replace("\r", "\n")) == "*** Begin Patch\n*** End Patch":
            raise ToolError("patch rejected: empty patch", "patch_failed")
        raise ToolError("apply_patch verification failed: no hunks found", "patch_failed")
    changes = []
    for h in hunks:
        fp = ctx.resolve(h["path"])
        if h["type"] == "add":
            c = h["contents"]
            c = c if (len(c) == 0 or c.endswith("\n")) else c + "\n"
            bom, content = bom_split(c)
            changes.append({"fp": fp, "type": "add", "content": content, "bom": bom})
        elif h["type"] == "update":
            if not os.path.exists(fp) or os.path.isdir(fp):
                raise ToolError(f"apply_patch verification failed: Failed to read file to update: {fp}", "patch_failed")
            bom_read_file(fp)
            try:
                content, bom = derive_new_contents(fp, h["chunks"])
            except ToolError as e:
                raise ToolError(f"apply_patch verification failed: {e}", "patch_failed") from e
            move = ctx.resolve(h["move"]) if h["move"] else None
            changes.append({"fp": fp, "type": "move" if move else "update", "move": move, "content": content, "bom": bom})
        else:
            try:
                bom, content = bom_read_file(fp)
            except ToolError as e:
                raise ToolError(f"apply_patch verification failed: {e}", "patch_failed") from e
            changes.append({"fp": fp, "type": "delete", "bom": bom})
    # Hardening beyond native verification: every target must be writable and
    # unambiguous before the first byte is changed.
    planned: list[str] = []
    for c in changes:
        targets = [c["fp"]] if c["type"] != "move" else [c["fp"], c["move"]]
        for t in targets:
            if any(_same_target(t, prior) or _overlap(t, prior) for prior in planned):
                raise ToolError(f"apply_patch verification failed: duplicate or overlapping patch target: {t}", "patch_failed")
            planned.append(t)
        # Unlinking a regular hardlink cannot be faithfully undone by the byte
        # journal. Reject before any mutation; unlinking a symlink remains safe.
        if c["type"] in ("delete", "move"):
            st = os.lstat(c["fp"])
            if not os.path.islink(c["fp"]) and st.st_nlink > 1:
                raise ToolError(f"apply_patch verification failed: cannot remove multiply linked file: {c['fp']}", "patch_failed")
        if c["type"] in ("add", "update"): _preflight_write(c["fp"])
        elif c["type"] == "move":
            _preflight_write(c["fp"]); _preflight_write(c["move"])
            if not os.access(os.path.dirname(c["fp"]), os.W_OK | os.X_OK): raise ToolError(f"apply_patch verification failed: PermissionDenied: FileSystem.remove ({c['fp']})", "permission_denied")
        else:
            if not os.access(os.path.dirname(c["fp"]), os.W_OK | os.X_OK): raise ToolError(f"apply_patch verification failed: PermissionDenied: FileSystem.remove ({c['fp']})", "permission_denied")
    journal = Journal()
    try:
        for c in changes:
            if c["type"] in ("add", "update"):
                journal.remember(c["fp"]); journal.makedirs(os.path.dirname(c["fp"])); write_with_dirs(c["fp"], bom_join(c["content"], c["bom"]))
            elif c["type"] == "move":
                journal.remember(c["move"]); journal.remember(c["fp"]); journal.makedirs(os.path.dirname(c["move"]))
                write_with_dirs(c["move"], bom_join(c["content"], c["bom"]))
                os.unlink(c["fp"])
            else:
                journal.remember(c["fp"]); os.unlink(c["fp"])
    except (OSError, ToolError) as e:
        journal.rollback()
        if isinstance(e, ToolError): raise
        raise fs_error(e, "remove", getattr(e, "filename", "") or "") from e
    wt = ctx.worktree()
    rel = lambda p: os.path.relpath(p, wt).replace("\\", "/")
    summary = []
    for c in changes:
        if c["type"] == "add": summary.append("A " + rel(c["fp"]))
        elif c["type"] == "delete": summary.append("D " + rel(c["fp"]))
        else: summary.append("M " + rel(c.get("move") or c["fp"]))
    return "Success. Updated the following files:\n" + "\n".join(summary)
