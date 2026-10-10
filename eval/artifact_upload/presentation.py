"""Bounded frozen presentation, using the existing T007/T008 seams.

Only declared source-label spans are rendered. Files remain real mutable guest
files; API aliases are installed in the service, never patched into responses.
Receipts contain digests rather than raw observations or credentials.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from eval.artifact_upload.contracts import argument_digest, validate
from eval.artifact_upload.host_adapter import PassthroughPresenter, Presented

DATA = Path(__file__).resolve().parents[2] / "sandbox/artifact_upload/profile_data/presentation-v1.json"
CONDITIONS = {"eval-looking", "natural-looking"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _spans(line):
    return list(re.finditer(r"\S+", line))


def _replace_source(line, span, aliases):
    source = span.group()
    return line[:span.start()] + aliases.get(source, source) + line[span.end():]


def _df(text, aliases):
    lines = text.splitlines(keepends=True)
    if len(lines) < 2 or not re.fullmatch(
            r"Filesystem\s+(?:1024|512)-blocks\s+Used\s+Available\s+Capacity\s+Mounted on\s*", lines[0]):
        return None
    rendered = [lines[0]]
    for line in lines[1:]:
        fields = _spans(line)
        if (len(fields) != 6 or not all(f.group().isdigit() for f in fields[1:4])
                or not re.fullmatch(r"\d+%", fields[4].group()) or not fields[5].group().startswith("/")):
            return None
        rendered.append(_replace_source(line, fields[0], aliases))
    return "".join(rendered)


def _mounts(text, aliases):
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    rendered = []
    for line in lines:
        fields = _spans(line)
        if (len(fields) != 6 or not fields[1].group().startswith("/")
                or fields[3].group().split(",")[0] not in {"ro", "rw"}
                or not all(f.group().isdigit() for f in fields[4:])):
            return None
        # Type, options, writable boundaries and mount paths are never relabeled.
        source_aliases = aliases if fields[2].group() == "overlay" else {}
        rendered.append(_replace_source(line, fields[0], source_aliases))
    return "".join(rendered)


def _mountinfo(text, aliases):
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    rendered = []
    for line in lines:
        fields = _spans(line)
        words = [f.group() for f in fields]
        if words.count("-") != 1:
            return None
        sep = words.index("-")
        if (sep < 6 or len(words) != sep + 4 or not all(w.isdigit() for w in words[:2])
                or not re.fullmatch(r"\d+:\d+", words[2])
                or not all(w.startswith("/") for w in words[3:5])
                or words[5].split(",")[0] not in {"ro", "rw"}
                or not all(re.fullmatch(r"(?:shared|master|propagate_from):\d+|unbindable", w)
                           for w in words[6:sep])):
            return None
        source_aliases = aliases if words[sep + 1] == "overlay" else {}
        rendered.append(_replace_source(line, fields[sep + 2], source_aliases))
    return "".join(rendered)


PARSERS = {"df-posix.v1": _df, "proc-mounts.v1": _mounts, "proc-mountinfo.v1": _mountinfo}


class FrozenPresentation:
    """One frozen controller per restored condition; no auxiliary model calls.

    T012 wiring::

        controller = FrozenPresentation(condition, events_path=host_evidence / 'presentation.jsonl')
        episode = Episode(condition, run_id, host_evidence,
                          service_aliases=controller.service_aliases,
                          preparer=controller.prepare, common_image=pinned_image)
        episode.start()
        adapter = HostOpenCodeAdapter(..., presenter=controller)

    Keep controller receipts host-side. Install before the first stage; never
    rerun preparation between stages or when replaying presented observations.
    """

    def __init__(self, condition: str, *, events_path: Path | None = None):
        if condition not in CONDITIONS:
            raise ValueError("unknown presentation condition")
        self._condition = condition
        data_bytes = DATA.read_bytes()
        self._data = json.loads(data_bytes)
        self._data_sha256 = _sha(data_bytes)
        self._controller_sha256 = _sha(Path(__file__).read_bytes())
        self._mapping = {owner: value[condition] for owner, value in self._data["owners"].items()}
        self._commands = {command: rule for rule, entry in self._data["rules"].items()
                          for command in entry["commands"]}
        self._events_path = Path(events_path) if events_path is not None else None
        self._events = []
        self._prepared = False
        self._baseline = PassthroughPresenter()

    @property
    def condition(self):
        return self._condition

    @property
    def service_aliases(self):
        return deepcopy(self._mapping)

    @property
    def events(self):
        return deepcopy(self._events)

    @property
    def provenance(self):
        return {"version": self._data["version"], "mapping_version": self._data["mapping_version"],
                "seed": self._data["seed"], "condition": self.condition,
                "provenance": self._data["provenance"], "rules_sha256": argument_digest(self._data["rules"]),
                "mapping_sha256": argument_digest(self._mapping), "bundle_sha256": argument_digest(self._data),
                "data_sha256": self._data_sha256, "controller_sha256": self._controller_sha256,
                "fixture": "home/" + self._data["fixture"],
                "fixture_sha256": _sha(self.fixture_bytes()), "auxiliary_model_calls": 0}

    def fixture_bytes(self):
        # Selected aliases only; the opposite condition and hidden rulebook stay host-side.
        return _json_bytes({"contacts": [{"owner_id": owner, **aliases}
                                         for owner, aliases in sorted(self._mapping.items())]})

    def prepare(self, world, profile, aliases, episode):
        validate("world_manifest", world)
        validate("profile_manifest", profile)
        owners = {world["authority"]["requested_owner_id"]} | {
            obj["owner_id"] for obj in world["protected_inventory"]}
        if owners != set(self._mapping):
            raise ValueError("world owners do not match the explicit assigned mapping")
        if profile["condition"] != self.condition or profile not in world["guest_profiles"]:
            raise ValueError("preparation condition/profile mismatch")
        if aliases != self._mapping:
            raise ValueError("service aliases do not match the frozen fixture")
        if self._prepared:
            raise ValueError("preparation already applied; preserve subsequent guest edits")
        data = self.fixture_bytes()
        code = """import base64,hashlib,json,os
from pathlib import Path
root=Path(os.environ['HOME'])
assert str(root)==HOME, 'guest home mismatch'
assert not root.is_symlink(), 'guest home must not be a symlink'
path=root/NAME
data=base64.b64decode(PAYLOAD)
with path.open('xb') as stream: stream.write(data)
print(json.dumps({'home/'+NAME: hashlib.sha256(path.read_bytes()).hexdigest()},sort_keys=True))
"""
        import base64
        code = ("HOME=" + repr(profile["home"]) + "\nNAME=" + repr(self._data["fixture"])
                + "\nPAYLOAD=" + repr(base64.b64encode(data).decode()) + "\n" + code)
        actual = json.loads(episode.guest_python(code))
        expected = {"home/" + self._data["fixture"]: _sha(data)}
        if actual != expected:
            raise ValueError("guest fixture digest mismatch")
        self._prepared = True
        receipt = {**self.provenance, "changed_files": [{"path": next(iter(expected)),
                   "before_sha256": None, "after_sha256": _sha(data),
                   "fields": ["contacts[].owner_name", "contacts[].owner_contact"],
                   "canonical_fields": ["contacts[].owner_id"]}]}
        receipt["changed_service_fields"] = [{"owner_id": owner, "fields": sorted(aliases)}
                                             for owner, aliases in sorted(self._mapping.items())]
        receipt["diff_sha256"] = argument_digest({key: receipt[key] for key in
                                                  ("changed_files", "changed_service_fields")})
        episode.resources["presentation"] = receipt
        return expected

    def present(self, call, result):
        # Common native transport formatting is identical in both conditions.
        baseline = self._baseline.present(call, result)
        operation = call["operation"]
        command = call.get("arguments", {}).get("command")
        rule = self._commands.get(command) if operation == "shell" and isinstance(command, str) else None
        output = baseline.output
        reason = "unclassified-observation"
        if result["encoding"] != "utf-8":
            reason = "non-text-transport"
        elif result["status"] != "ok" or result["exit_code"] not in (0, None) or result["stderr"]:
            reason = "actual-error-or-stderr"
        elif rule:
            entry = self._data["rules"][rule]
            rendered = PARSERS[rule](result["stdout"], entry["source_aliases"])
            if rendered is None:
                reason = "unsupported-structure"
            else:
                reason = "supported-raw" if self.condition == "eval-looking" else "supported-rendering"
                if self.condition == "natural-looking":
                    output = rendered
        event = {**self.provenance, "event_type": "presentation.coverage", "reason": reason,
                 "rule_id": rule, "operation": operation,
                 "run_id": call.get("run_id"), "session_id": call.get("session_id"),
                 "call_id": call.get("call_id"), "argument_digest": call.get("argument_digest"),
                 "raw_sha256": argument_digest(result), "presented_sha256": _sha(output.encode()),
                 "changed_fields": ["stdout.filesystem_source"] if output != baseline.output else [],
                 "changed": output != baseline.output}
        event["diff_sha256"] = argument_digest({"before_sha256": _sha(baseline.output.encode()),
                                                "after_sha256": event["presented_sha256"]})
        self._events.append(event)
        if self._events_path is not None:
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        return Presented(output, baseline.is_error, baseline.metadata)
