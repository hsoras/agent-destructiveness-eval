"""Data-only presentation rules for the script/live proxy (``artifact-presentation-rules.v1``).

A ``RuleSet`` is immutable (copy-on-write: ``with_changes`` returns a new set with
``version+1`` and ``parent_sha256`` = the old sha). Rules are JSON data only; no
LLM-written code is ever executed. Edit kinds:

* ``literal``     replace the FIRST occurrence of ``find``
* ``replace_all`` replace EVERY occurrence of ``find`` (plain text)
* ``regex``       ``re.sub`` with MULTILINE; guarded by size caps, a static
                  nested-quantifier rejection and a per-render time budget

``check_rendering`` is the hard safety guard applied on every render: protected
facts (numbers/sizes, hashes, owner/object ids, URLs, HTTP status lines) must be
identical, as a multiset, in raw and rendered text. Any failure -> caller falls
back to the raw output.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
from pathlib import Path
import re
import threading

SCHEMA = "artifact-presentation-rules.v1"
V1_VERSION = "artifact-presentation.v1"
CONDITIONS = frozenset({"eval-looking", "natural-looking"})
KINDS = frozenset({"literal", "regex", "replace_all"})
ORIGINS = frozenset({"user", "llm"})
MAX_RULES, MAX_EDITS, MAX_COMMANDS = 64, 16, 16
MAX_FIND, MAX_REPLACE, MAX_NOTE = 300, 500, 500
MAX_TEXT = 256 * 1024
REGEX_SECONDS = 0.5
RULE_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


class RuleError(ValueError):
    """Invalid rule data or a failed/unsafe render (caller fails open)."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------- regex safety
_QUANTIFIED_GROUP = re.compile(r"\(([^()]*)\)\s*(?:[+*]|\{\d*,\d*\})")


def check_regex(pattern: str) -> re.Pattern:
    if not isinstance(pattern, str) or not 0 < len(pattern) <= MAX_FIND:
        raise RuleError("regex size")
    for inner in _QUANTIFIED_GROUP.findall(pattern):
        if re.search(r"[+*|]|\{\d*,\d*\}", inner):
            raise RuleError("nested quantifier or alternation under a quantifier")
    try:
        return re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise RuleError("bad regex: " + str(exc)[:80])


def _timed(fn, seconds=REGEX_SECONDS):
    box = {}
    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported to caller
            box["e"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise RuleError("regex-timeout")
    if "e" in box:
        raise RuleError("regex failed: " + type(box["e"]).__name__)
    return box["v"]


def apply_edit(text: str, edit: dict) -> str:
    if len(text) > MAX_TEXT:
        raise RuleError("text too large")
    kind, find, repl = edit["kind"], edit["find"], edit["replace"]
    if kind == "literal":
        return text.replace(find, repl, 1)
    if kind == "replace_all":
        return text.replace(find, repl)
    pattern = check_regex(find)
    try:
        return _timed(lambda: pattern.sub(repl, text))
    except re.error as exc:
        raise RuleError("bad replacement: " + str(exc)[:80])


def apply_edits(text: str, edits) -> str:
    for edit in edits:
        text = apply_edit(text, edit)
    return text


# ----------------------------------------------------------- protected facts
_PROTECTED = re.compile(
    r"https?://[^\s\"'<>]+"
    r"|HTTP/\d(?:\.\d)?\s+\d{3}[^\n]*"
    r"|\b(?:team|owner|obj|object|artifact|upload)[-_][\w-]+"
    r"|\b[0-9a-fA-F]{12,}\b"
    r"|(?<![\w.-])\d+(?:[.,]\d+)*(?:[KMGTP]i?B?|B)?(?!\w)")


def protected_facts(text: str, extra=()) -> Counter:
    found = Counter(m.group() for m in _PROTECTED.finditer(text))
    for token in extra:
        if token:
            found["literal:" + token] += text.count(token)
    return found


def check_rendering(raw: str, rendered: str, protected_tokens=()) -> tuple[bool, str]:
    """Protected facts of ``raw`` must be unchanged in ``rendered`` (none lost, none added)."""
    if not isinstance(rendered, str):
        return False, "not-text"
    if len(rendered) > 4 * len(raw) + 4096:
        return False, "size-growth"
    before, after = protected_facts(raw, protected_tokens), protected_facts(rendered, protected_tokens)
    if before != after:
        return False, "protected-facts-changed"
    return True, "ok"


# ------------------------------------------------------------- validation
def _text(value, limit, what, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or (not value and not allow_empty):
        raise RuleError("invalid " + what)
    return value


OPERATIONS = {"shell": ("stdout", "stderr"), "read": ("text", "error"), "grep": ("results", "error"),
              "find": ("results", "error"), "glob": ("results", "error")}
REFS = frozenset({"direct", "output_ref", "any"})
FACT = re.compile(r"[A-Za-z0-9_.:/-]{1,96}")
MAX_REQUIRES = 8
_SCALAR = (str, int, float, bool, type(None))


def _requirement(req) -> dict:
    ops = {"equals", "not_equals", "in", "exists"}
    if (not isinstance(req, dict) or "fact" not in req or not isinstance(req["fact"], str)
            or not FACT.fullmatch(req["fact"]) or len(set(req) - {"fact"}) != 1 or not (set(req) - {"fact"}) <= ops):
        raise RuleError("invalid requires entry")
    (op,) = set(req) - {"fact"}
    value = req[op]
    if op == "in":
        if not isinstance(value, list) or not 0 < len(value) <= 16 or any(not isinstance(v, _SCALAR) for v in value):
            raise RuleError("invalid requires entry")
    elif op == "exists":
        if type(value) is not bool:
            raise RuleError("invalid requires entry")
    elif not isinstance(value, _SCALAR) or (isinstance(value, str) and len(value) > MAX_FIND):
        raise RuleError("invalid requires entry")
    return {"fact": req["fact"], op: deepcopy(value)}


def _match(match) -> dict:
    """Legacy shell matches stay byte-identical; read/grep, surface, ref scope and state preconditions are additive."""
    if not isinstance(match, dict) or match.get("operation") not in OPERATIONS:
        raise RuleError("match needs operation=shell|read|grep|find|glob")
    operation = "find" if match["operation"] == "glob" else match["operation"]
    keys = ("commands", "command_regex") if operation == "shell" else ("paths", "path_regex")
    if set(match) - {"operation", "surface", "refs", "requires", *keys} or sum(k in match for k in keys) != 1:
        raise RuleError("match needs operation=shell and exactly one of commands/command_regex"
                        if operation == "shell" else "match needs exactly one of paths/path_regex")
    if operation == "shell" and "refs" in match:
        raise RuleError("refs applies to read/grep matches")
    out = {"operation": operation}
    key = next(k for k in keys if k in match)
    if key in ("commands", "paths"):
        items = match[key]
        if not isinstance(items, list) or not 0 < len(items) <= MAX_COMMANDS:
            raise RuleError("invalid " + key)
        out[key] = [_text(c, MAX_FIND, key[:-1]) for c in items]
    else:
        check_regex(match[key])
        out[key] = match[key]
    if "surface" in match:
        if match["surface"] not in OPERATIONS[operation]:
            raise RuleError("invalid surface")
        out["surface"] = match["surface"]
    if "refs" in match:
        if match["refs"] not in REFS:
            raise RuleError("invalid refs")
        out["refs"] = match["refs"]
    if "requires" in match:
        req = match["requires"]
        if not isinstance(req, list) or len(req) > MAX_REQUIRES:
            raise RuleError("invalid requires")
        out["requires"] = [_requirement(r) for r in req]
    return out


def holds(requirement: dict, state) -> bool:
    """One state precondition against a flat fact mapping; a missing fact only satisfies ``exists: false``."""
    state = state or {}
    fact = requirement["fact"]
    if "exists" in requirement:
        return (fact in state) == requirement["exists"]
    if fact not in state:
        return False
    value = state[fact]
    if "equals" in requirement:
        return value == requirement["equals"]
    if "not_equals" in requirement:
        return value != requirement["not_equals"]
    return value in requirement["in"]


def surface_of(match: dict) -> str:
    return match.get("surface") or OPERATIONS[match["operation"]][0]


def call_key(call: dict) -> str | None:
    """The string a rule's commands/paths/regex matches: command, read file path, or search path."""
    args = call.get("arguments") or {}
    op = call.get("operation")
    value = (args.get("command") if op == "shell" else args.get("filePath") if op == "read"
             else args.get("path") or "." if op in {"grep", "find", "glob"} else None)
    return value if isinstance(value, str) else None


_ERROR_WORDS = re.compile(r"(?i)no such file|permission denied|not a directory|is a directory|not found|denied|forbidden|"
                          r"unauthori[sz]ed|conflict|already exists|exists|quota|exceed\w*|invalid|fatal|error|failed|fail|"
                          r"cannot|refused|timed out|timeout|traceback|exception|warning")


def check_error_meaning(raw: str, rendered: str) -> tuple[bool, str]:
    """Error/stderr renderings keep their error vocabulary (as a multiset) and never become empty."""
    if bool(raw.strip()) != bool(rendered.strip()):
        return False, "error-emptiness-changed"
    if Counter(m.group().lower() for m in _ERROR_WORDS.finditer(raw)) != Counter(m.group().lower() for m in _ERROR_WORDS.finditer(rendered)):
        return False, "error-meaning-changed"
    return True, "ok"


def check_surface(raw: str, rendered: str, surface: str, protected_tokens=()) -> tuple[bool, str]:
    """Protected-fact guard for every delivered surface; error surfaces also keep their error meaning."""
    ok, why = check_rendering(raw, rendered, protected_tokens)
    if ok and surface in ("stderr", "error"):
        return check_error_meaning(raw, rendered)
    return ok, why


def _rule(rule) -> dict:
    if not isinstance(rule, dict) or not {"rule_id", "match", "edits"} <= set(rule):
        raise RuleError("rule needs rule_id, match, edits")
    extra = set(rule) - {"rule_id", "origin", "condition", "match", "edits", "note", "provenance"}
    if extra:
        raise RuleError("unknown rule fields")
    rid = rule["rule_id"]
    if not isinstance(rid, str) or not RULE_ID.fullmatch(rid):
        raise RuleError("invalid rule_id")
    origin = rule.get("origin", "user")
    if origin not in ORIGINS:
        raise RuleError("invalid origin")
    condition = rule.get("condition", "any")
    if condition != "any" and condition not in CONDITIONS:
        raise RuleError("invalid condition")
    m = _match(rule["match"])
    edits = rule["edits"]
    if not isinstance(edits, list) or not 0 < len(edits) <= MAX_EDITS:
        raise RuleError("invalid edits")
    out = []
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {"kind", "find", "replace"} or edit["kind"] not in KINDS):
            raise RuleError("invalid edit")
        _text(edit["find"], MAX_FIND, "find")
        _text(edit["replace"], MAX_REPLACE, "replace", allow_empty=True)
        if edit["kind"] == "regex":
            check_regex(edit["find"])
        out.append(dict(edit))
    provenance = rule.get("provenance", "")
    if isinstance(provenance, str):
        _text(provenance, MAX_NOTE, "provenance", allow_empty=True)
    elif not isinstance(provenance, dict) or len(_canon(provenance)) > 2000:
        raise RuleError("invalid provenance")
    return {"rule_id": rid, "origin": origin, "condition": condition, "match": m, "edits": out,
            "note": _text(rule.get("note", ""), MAX_NOTE, "note", allow_empty=True), "provenance": deepcopy(provenance)}


def _normalize(data) -> dict:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise RuleError("wrong schema")
    if set(data) - {"schema", "version", "parent_sha256", "rules", "label_map"}:
        raise RuleError("unknown fields")
    version, parent = data.get("version", 1), data.get("parent_sha256")
    if type(version) is not int or version < 1:
        raise RuleError("invalid version")
    if parent is not None and not (isinstance(parent, str) and re.fullmatch(r"[0-9a-f]{64}", parent)):
        raise RuleError("invalid parent_sha256")
    rules = data.get("rules", [])
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise RuleError("invalid rules")
    rules = [_rule(r) for r in rules]
    if len({r["rule_id"] for r in rules}) != len(rules):
        raise RuleError("duplicate rule_id")
    labels = data.get("label_map", {})
    if (not isinstance(labels, dict) or len(labels) > 64 or
            any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 200 or len(v) > 200 for k, v in labels.items())):
        raise RuleError("invalid label_map")
    return {"schema": SCHEMA, "version": version, "parent_sha256": parent, "rules": rules, "label_map": dict(labels)}


@dataclass(frozen=True)
class RuleSet:
    canon: str  # canonical JSON of the normalized data; the only state

    @classmethod
    def from_dict(cls, data) -> "RuleSet":
        return cls(_canon(_normalize(deepcopy(data))))

    @classmethod
    def empty(cls) -> "RuleSet":
        return cls.from_dict({"schema": SCHEMA, "version": 1, "parent_sha256": None, "rules": [], "label_map": {}})

    @classmethod
    def from_v1(cls, v1: dict, condition: str = "natural-looking") -> "RuleSet":
        """Convert an ``artifact-presentation.v1`` bundle (source-label aliases only)."""
        if v1.get("version") != V1_VERSION or condition not in CONDITIONS:
            raise RuleError("not a v1 bundle / unknown condition")
        shapes = {  # per parser: (regex for the source field, replacement template)
            "df-posix.v1": (r"^{src}(?=\s)", "{dst}"),
            "proc-mounts.v1": (r"^{src}(?=\s+/\S*\s+overlay\s)", "{dst}"),
            "proc-mountinfo.v1": (r"(- overlay ){src}(?=\s)", r"\g<1>{dst}"),
        }
        rules, labels = [], {}
        if condition == "natural-looking":  # eval-looking presents raw output
            for name, entry in v1["rules"].items():
                find, repl = shapes[name]
                edits = [{"kind": "regex", "find": find.format(src=re.escape(s)),
                          "replace": repl.format(dst=d.replace("\\", "\\\\"))}
                         for s, d in entry["source_aliases"].items()]
                labels.update(entry["source_aliases"])
                rules.append({"rule_id": name, "origin": "user", "condition": condition,
                              "match": {"operation": "shell", "commands": list(entry["commands"])},
                              "edits": edits, "note": "converted from presentation-v1.json",
                              "provenance": {"v1_rule": name, "v1_version": v1["version"]}})
        return cls.from_dict({"schema": SCHEMA, "version": 1, "parent_sha256": None, "rules": rules, "label_map": labels})

    @classmethod
    def loads(cls, text: str, condition: str = "natural-looking") -> "RuleSet":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise RuleError("invalid JSON") from exc
        if isinstance(data, dict) and data.get("version") == V1_VERSION:
            return cls.from_v1(data, condition)
        return cls.from_dict(data)

    @classmethod
    def load(cls, path, condition: str = "natural-looking") -> "RuleSet":
        return cls.loads(Path(path).read_text(), condition)

    # ---- views
    @cached_property
    def _obj(self) -> dict:
        return json.loads(self.canon)

    @property
    def data(self) -> dict:
        return deepcopy(self._obj)

    @property
    def rules(self) -> list:
        return deepcopy(self._obj["rules"])

    @property
    def version(self) -> int:
        return self._obj["version"]

    @property
    def parent_sha256(self):
        return self._obj["parent_sha256"]

    @property
    def label_map(self) -> dict:
        return dict(self._obj["label_map"])

    @cached_property
    def sha256(self) -> str:
        return _sha(self.canon.encode())

    def dumps(self) -> str:
        return json.dumps(self._obj, indent=2, sort_keys=True) + "\n"

    def dump(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps())
        return path

    def get(self, rule_id: str):
        return next((deepcopy(r) for r in self._obj["rules"] if r["rule_id"] == rule_id), None)

    # ---- copy on write
    def with_changes(self, changes) -> "RuleSet":
        """changes: [{"action": "add"|"replace"|"remove", "rule_id"?, "rule"?}]; returns a NEW set."""
        rules = deepcopy(self._obj["rules"])
        for change in changes:
            action = change.get("action")
            if action == "remove":
                kept = [r for r in rules if r["rule_id"] != change.get("rule_id")]
                if len(kept) == len(rules):
                    raise RuleError("remove: unknown rule_id")
                rules = kept
                continue
            rule = deepcopy(change.get("rule"))
            if action not in ("add", "replace") or not isinstance(rule, dict):
                raise RuleError("invalid change")
            index = next((i for i, r in enumerate(rules) if r["rule_id"] == rule.get("rule_id")), None)
            if action == "add" and index is not None:
                raise RuleError("add: rule_id exists")
            if action == "replace":
                if index is None:
                    raise RuleError("replace: unknown rule_id")
                rules[index] = rule
            else:
                rules.append(rule)
        data = self.data
        data.update(rules=rules, version=self.version + 1, parent_sha256=self.sha256)
        return RuleSet.from_dict(data)

    # ---- rendering
    @staticmethod
    def _hit(match: dict, key: str) -> bool:
        if "commands" in match or "paths" in match:
            return key in match.get("commands", match.get("paths"))
        return check_regex(match.get("command_regex", match.get("path_regex"))).fullmatch(key) is not None

    def select(self, operation: str, key: str, surface: str, condition: str | None = None, state=None, via_ref: bool = False) -> list:
        """Rules for one operation/surface whose condition, target and state preconditions all hold."""
        out = []
        for rule in self._obj["rules"]:
            m = rule["match"]
            if m["operation"] != operation or surface_of(m) != surface:
                continue
            if condition and rule["condition"] not in ("any", condition):
                continue
            refs = m.get("refs", "direct")
            if operation != "shell" and ((refs == "direct" and via_ref) or (refs == "output_ref" and not via_ref)):
                continue
            if not self._hit(m, key) or any(not holds(r, state) for r in m.get("requires", ())):
                continue
            out.append(rule)
        return out

    def matching(self, command: str, condition: str | None = None) -> list:
        # Legacy view: stateless shell-stdout rules (a rule with state preconditions needs render_surface).
        return [r for r in self.select("shell", command, "stdout", condition, None) if not r["match"].get("requires")]

    def render(self, command: str, stdout: str, condition: str | None = None) -> tuple[str, list]:
        """Apply matching rules in order. Returns (text, [rule_id that changed the text]).
        Raises RuleError on any failure; callers must fall back to raw."""
        return self._apply(self.matching(command, condition), stdout)

    def render_surface(self, call: dict, surface: str, text: str, condition: str | None = None, state=None,
                       via_ref: bool = False) -> tuple[str, list]:
        """Surface-aware render of one delivered text (shell stdout/stderr, read text/error, search results/error)."""
        key = call_key(call)
        if key is None or surface not in OPERATIONS.get(call.get("operation"), ()):
            return text, []
        operation = "find" if call["operation"] == "glob" else call["operation"]
        return self._apply(self.select(operation, key, surface, condition, state, via_ref), text)

    @staticmethod
    def _apply(rules, text):
        applied = []
        for rule in rules:
            new = apply_edits(text, rule["edits"])
            if new != text:
                applied.append(rule["rule_id"])
            text = new
        return text, applied

    def inactive(self, state) -> list:
        """Rule IDs whose state preconditions no longer hold (callers log them as invalidated)."""
        return [r["rule_id"] for r in self._obj["rules"] if any(not holds(q, state) for q in r["match"].get("requires", ()))]


def default_ruleset(condition: str = "natural-looking") -> RuleSet:
    """Default rules == the frozen presentation behaviour (converted from presentation-v1.json)."""
    from .presentation import DATA
    return RuleSet.from_v1(json.loads(DATA.read_text()), condition)
