"""Development-only data proposals and explicit, future-batch promotion.

No operation executor or installation API exists here. MeasuredPresenter wraps
only the existing native deterministic presenters. AuxiliaryClient uses the
existing ProviderGuard with tools disabled; development receipts use RunRecorder
'diagnostics', separately from target requests and budgets.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import http.client
import json
import math
import re
import socket
import threading
import time
import uuid

from .contracts import argument_digest
from .host_adapter import PassthroughPresenter, ProviderGuard
from .presentation import CONDITIONS, DATA, PARSERS, FrozenPresentation


DIGEST = re.compile(r"[0-9a-f]{64}")
TEST_CASES = frozenset({"ordinary", "alternate-format", "malformed", "mixed-output", "protected-service"})
ROLES = ("proposer", "reviewer", "routing", "presentation")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digests(value):
    return (isinstance(value, dict) and 0 < len(value) <= 32 and
            all(isinstance(k, str) and 0 < len(k) <= 64 and isinstance(v, str) and DIGEST.fullmatch(v)
                for k, v in value.items()))


def _object(raw: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


class ProtocolViolation(RuntimeError):
    pass


class AuxiliaryTripwire:
    """Optional ProviderGuard audit seam; any measured auxiliary attempt stops."""
    def __init__(self, mode, recorder):
        if mode not in {"passthrough", "frozen"}:
            raise ValueError("tripwire requires a measured mode")
        self.mode, self.recorder = mode, recorder
        self._attempts = dict.fromkeys(ROLES, 0)

    def reject(self, role):
        if role not in ROLES:
            raise ValueError("unknown auxiliary role")
        self._attempts[role] += 1
        self.recorder.append("diagnostics", {"kind": "auxiliary_protocol_violation", "mode": self.mode,
                                             "role": role, "forwarded": False})
        raise ProtocolViolation("auxiliary request during measurement")

    def audit(self, _body):
        # Supply this audit only to an AUXILIARY guard, never the target guard.
        self.reject("routing")

    def receipt(self):
        return {"mode": self.mode, "auxiliary_model_calls": 0, "attempts": dict(self._attempts),
                "protocol_valid": not any(self._attempts.values())}

    def assert_zero(self):
        receipt = self.receipt()
        self.recorder.append("diagnostics", {"kind": "auxiliary_measurement", **receipt})
        if not receipt["protocol_valid"]:
            raise ProtocolViolation("measured auxiliary tripwire fired")
        return receipt


class MeasuredPresenter:
    """T012: wrap the same controller used to prepare the Episode, then record
    ``presenter.tripwire.assert_zero()`` at completion (including failure paths).
    This wrapper accepts no auxiliary client, discovery controller or callback.
    """
    def __init__(self, mode, presenter, recorder):
        expected = {"passthrough": PassthroughPresenter, "frozen": FrozenPresentation}.get(mode)
        if expected is None or type(presenter) is not expected:
            raise ValueError("measured mode requires its native deterministic presenter")
        self._presenter = presenter
        self.tripwire = AuxiliaryTripwire(mode, recorder)

    def present(self, call, result):
        try:
            return self._presenter.present(call, result)
        finally:
            self.tripwire.assert_zero()


@dataclass(frozen=True)
class Budget:
    cost_usd: float  # explicit combined ceiling; there is no implicit paid budget
    max_call_cost_usd: float
    pairs: int = 5
    seconds: float = 10.0
    output_tokens: int = 2048

    def __post_init__(self):
        if (type(self.pairs) is not int or not 1 <= self.pairs <= 5 or
                type(self.output_tokens) is not int or not 1 <= self.output_tokens <= 2048 or
                not all(type(x) in (int, float) and math.isfinite(x) and x > 0
                        for x in (self.cost_usd, self.max_call_cost_usd, self.seconds)) or self.seconds > 10):
            raise ValueError("invalid bounded development budget")


@dataclass(frozen=True)
class Reply:
    output: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class AuxiliaryClient:
    """Explicitly configured development transport; never created by measured
    routes. Fixed per-token upper prices must cover the configured provider.
    No tools, streaming, automatic retries, or target context are sent.
    Local acceptance substitutes mocked replies instead of calling providers.
    """
    def __init__(self, upstream, model, *, input_price, output_price, api_key=None):
        if not model or any(not math.isfinite(x) or x < 0 for x in (input_price, output_price)):
            raise ValueError("model and nonnegative per-token prices required")
        self.upstream, self.model, self.api_key = upstream, model, api_key
        self.input_price, self.output_price = input_price, output_price

    def complete(self, payload, *, seconds, max_tokens, max_cost_usd):
        prompt = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        # Conservative UTF-8 byte bound on prompt tokens, plus envelope allowance.
        if (len(prompt.encode()) + 1024) * self.input_price + max_tokens * self.output_price > max_cost_usd:
            raise ValueError("request cannot fit reserved cost")
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "stream": False}
        raw = json.dumps(body).encode()
        def audit(actual):
            return [] if actual == body else [{"reason": "unexpected auxiliary request"}]
        guard = ProviderGuard(self.upstream, audit, max_request_bytes=len(raw), upstream_timeout=seconds).start()
        connection = http.client.HTTPConnection("127.0.0.1", guard._server.server_port, timeout=seconds)
        started = time.monotonic()
        timer = None
        try:
            connection.connect()
            transport_socket = connection.sock
            def expire():
                try:
                    transport_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("auxiliary deadline")
            # Absolute deadline also stops trickling headers/body, unlike a
            # socket inactivity timeout. It never dispatches a replacement call.
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            connection.request("POST", "/v1/chat/completions", body=raw, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("auxiliary provider failure")
            chunks, size = [], 0
            while True:
                remaining = seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("auxiliary deadline")
                # HTTP/1.0 response retains its socket through the response file.
                response.fp.raw._sock.settimeout(remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 128 * 1024:
                    raise ValueError("auxiliary response too large")
                chunks.append(chunk)
            data = _object(b"".join(chunks).decode())
            usage = data["usage"]
            inp, out = usage["prompt_tokens"], usage["completion_tokens"]
            return Reply(data["choices"][0]["message"]["content"], inp, out,
                         inp * self.input_price + out * self.output_price)
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()
            guard.close()


@dataclass(frozen=True)
class Evidence:
    command: str
    raw: str
    condition: str
    base_digests: tuple[tuple[str, str], ...]
    prior: tuple[str, ...] = ()
    status: str = "ok"
    exit_code: int = 0
    stderr: str = ""

    def packet(self):
        rules = json.loads(DATA.read_bytes())["rules"]
        rule = next((key for key, entry in rules.items() if self.command in entry["commands"]), None)
        if (len(dict(self.base_digests)) != len(self.base_digests) or
                self.status != "ok" or type(self.exit_code) is not int or self.exit_code != 0 or self.stderr != "" or
                self.condition not in CONDITIONS or not _digests(dict(self.base_digests)) or rule is None or
                not isinstance(self.raw, str) or len(self.raw.encode()) > 32768 or
                len(self.prior) > 5 or any(not isinstance(x, str) or len(x.encode()) > 32768 for x in self.prior)):
            raise ValueError("unscoped infrastructure evidence")
        # The existing parsers exclude ordinary coding/service/mixed outputs.
        if any(PARSERS[rule](x, {}) is None for x in (self.raw, *self.prior)):
            raise ValueError("unverifiable infrastructure span")
        choices = rules[rule]["source_aliases"]
        if self.condition == "eval-looking":
            choices = {key: key for key in choices}
        return {"command": self.command, "raw": self.raw, "raw_sha256": sha(self.raw.encode()),
                "status": self.status, "exit_code": self.exit_code, "stderr": self.stderr,
                "condition": self.condition, "base_digests": dict(self.base_digests), "rule": rule,
                "prior_observations": list(self.prior), "allowed_fields": {"source_aliases": choices},
                "protected_invariants": ["all bytes outside declared source-label spans", "no new authority or stakes"]}


@dataclass(frozen=True)
class Decision:
    raw: str
    rendered: str
    accepted: bool
    transaction_id: str
    proposal_digest: str | None = None
    review_digest: str | None = None


@dataclass(frozen=True)
class ImplementationReview:
    transaction_id: str
    artifacts: tuple[tuple[str, str], ...]
    tests_digest: str
    review_digest: str


@dataclass(frozen=True)
class Promotion:
    batch_id: str
    developer: str
    base_digests: tuple[tuple[str, str], ...]
    artifacts: tuple[tuple[str, str], ...]
    tests_digest: str
    review_digest: str
    proposal_digest: str


class DevelopmentDiscovery:
    def __init__(self, *, mode, proposer, reviewer, recorder, budget, base_digests, source_batch_id):
        if mode != "develop":
            raise ValueError("auxiliary discovery is development-only")
        if proposer is reviewer or not _digests(base_digests) or not source_batch_id:
            raise ValueError("independent clients and frozen base required")
        if (proposer.upstream, proposer.model) == (reviewer.upstream, reviewer.model):
            raise ValueError("reviewer must use a distinct configured model identity")
        self.proposer, self.reviewer, self.recorder, self.budget = proposer, reviewer, recorder, budget
        self._base = deepcopy(base_digests)
        self.source_batch_id = source_batch_id
        self._calls, self._reserved, self._actual, self._halted = 0, 0.0, 0.0, False
        self._unaccounted = 0
        self._decisions, self._reviews, self._promoted = {}, {}, set()

    def usage(self):
        return {"account": "development_auxiliary", "calls": self._calls, "reserved_cost_usd": self._reserved,
                "reported_cost_usd": self._actual, "unaccounted_calls": self._unaccounted, "halted": self._halted}

    def _call(self, role, payload, transaction):
        cap = self.budget.max_call_cost_usd
        if self._halted or self._calls >= 2 * self.budget.pairs or self._reserved + cap > self.budget.cost_usd + 1e-12:
            raise ValueError("development budget exhausted")
        self._calls += 1
        self._reserved += cap  # failed/unknown calls retain the full reservation
        started, reply, status = time.monotonic(), None, "failed"
        try:
            client = self.proposer if role == "proposer" else self.reviewer
            reply = client.complete(deepcopy(payload), seconds=self.budget.seconds,
                                    max_tokens=self.budget.output_tokens, max_cost_usd=cap)
            if (isinstance(reply, Reply) and type(reply.cost_usd) in (int, float) and
                    math.isfinite(reply.cost_usd) and reply.cost_usd >= 0):
                self._actual += reply.cost_usd  # record overruns too; never present them as zero
            else:
                self._unaccounted += 1
            if (not isinstance(reply, Reply) or type(reply.input_tokens) is not int or reply.input_tokens < 0 or
                    type(reply.output_tokens) is not int or not 0 <= reply.output_tokens <= self.budget.output_tokens or
                    type(reply.cost_usd) not in (float, int) or not math.isfinite(reply.cost_usd) or not 0 <= reply.cost_usd <= cap or
                    not isinstance(reply.output, str) or len(reply.output.encode()) > 32768 or
                    time.monotonic() - started > self.budget.seconds):
                self._halted = True
                raise ValueError("auxiliary response exceeds bounds or lacks usage")
            status = "completed"
            return _object(reply.output)
        except Exception:
            # An unknown charge or transport failure cannot trigger retries.
            if reply is None:
                self._halted = True
                self._unaccounted += 1
            raise
        finally:
            receipt = None
            if isinstance(reply, Reply):
                receipt = {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
                           for key, value in reply.__dict__.items()}
            self.recorder.append("diagnostics", {"kind": "development_auxiliary_call", "transaction_id": transaction,
                "role": role, "status": status, "seconds": time.monotonic() - started,
                "request_digest": argument_digest(payload), "reply": receipt,
                **self.usage()})

    def _proposal(self, value, packet):
        if (not isinstance(value, dict) or set(value) != {"raw_sha256", "base_digests", "condition", "rule", "changes"} or
                any(value[key] != packet[key] for key in ("raw_sha256", "base_digests", "condition", "rule")) or
                not isinstance(value["changes"], dict) or set(value["changes"]) != {"source_aliases"}):
            raise ValueError("malformed or stale proposal")
        aliases = value["changes"]["source_aliases"]
        if (not isinstance(aliases, dict) or not aliases or
                any(k not in packet["allowed_fields"]["source_aliases"] or
                    packet["allowed_fields"]["source_aliases"][k] != v for k, v in aliases.items())):
            raise ValueError("protected or undeclared field change")
        rendered = PARSERS[packet["rule"]](packet["raw"], aliases)
        if rendered is None:
            raise ValueError("unverifiable rendering")
        return rendered

    @staticmethod
    def _verdict(value, subject):
        if (not isinstance(value, dict) or set(value) != {"subject_digest", "verdict", "checks"} or
                value["subject_digest"] != subject or value["verdict"] != "approve" or
                value["checks"] != {key: True for key in ("identity", "cross_query", "span", "condition", "protected_facts", "no_new_authority")} or
                any(type(x) is not bool for x in value["checks"].values())):
            raise ValueError("review rejected or malformed")

    def discover(self, evidence):
        transaction = uuid.uuid4().hex
        packet, proposal, review = None, None, None
        try:
            packet = evidence.packet()
            if packet["base_digests"] != self._base:
                raise ValueError("stale evidence")
            proposal = self._call("proposer", {"phase": "proposal", "original_evidence": packet}, transaction)
            rendered = self._proposal(proposal, packet)  # always before reviewer dispatch
            review = self._call("reviewer", {"phase": "policy_review", "original_evidence": packet,
                                            "proposal": proposal}, transaction)
            self._verdict(review, argument_digest(proposal))
            decision = Decision(evidence.raw, rendered, True, transaction, argument_digest(proposal), argument_digest(review))
            self._decisions[transaction] = (decision, deepcopy(packet), deepcopy(proposal))
        except Exception as exc:
            # No executor exists, so fallback cannot replay or mutate an operation.
            decision = Decision(evidence.raw, evidence.raw, False, transaction)
            reason = type(exc).__name__  # never log possibly secret exception messages
        else:
            reason = "reviewed-development-preview"
        self.recorder.append("diagnostics", {"kind": "development_candidate", "transaction_id": transaction,
            "original_evidence": packet, "proposal": proposal, "review": review,
            "accepted": decision.accepted, "reason": reason})
        return decision

    def review_implementation(self, decision, *, artifacts, tests):
        saved, packet, proposal = self._decisions[decision.transaction_id]
        if saved != decision or not decision.accepted:
            raise ValueError("unknown reviewed candidate")
        self._proposal(proposal, packet)
        # Developer-owned source/files, not proposer JSON. Test receipts bind exact
        # artifact bytes and successful ordinary/alternate/malformed/mixed/service tests.
        digests = self._artifacts(artifacts)
        if (set(tests) != TEST_CASES or any(set(t) != {"artifacts", "exit_code", "log"} or
                t["artifacts"] != digests or type(t["exit_code"]) is not int or t["exit_code"] != 0 or
                not isinstance(t["log"], bytes) or not 0 < len(t["log"]) <= 32768 for t in tests.values())):
            raise ValueError("missing successful artifact-bound test evidence")
        test_receipt = {key: {"artifacts": t["artifacts"], "exit_code": 0, "log_sha256": sha(t["log"])}
                        for key, t in tests.items()}
        subject = {"artifacts": digests, "tests": test_receipt, "proposal_digest": decision.proposal_digest}
        # Independent reviewer sees actual implemented bytes, original evidence and tests.
        payload = {"phase": "implementation_review", "original_evidence": packet, "proposal": proposal,
                   "implementation": {k: v.decode("utf-8") for k, v in artifacts.items()}, "subject": subject,
                   "test_logs": {k: t["log"].decode("utf-8") for k, t in tests.items()}}
        verdict = self._call("reviewer", payload, decision.transaction_id)
        self._verdict(verdict, argument_digest(subject))
        result = ImplementationReview(decision.transaction_id, tuple(sorted(digests.items())), argument_digest(test_receipt), argument_digest(verdict))
        self._reviews[decision.transaction_id] = result
        self.recorder.append("diagnostics", {"kind": "development_implementation_review", "subject": subject,
                                             "verdict": verdict, "transaction_id": decision.transaction_id})
        return result

    @staticmethod
    def _artifacts(artifacts):
        if (not isinstance(artifacts, dict) or not artifacts or len(artifacts) > 10 or
                any(not isinstance(k, str) or not k or not isinstance(v, bytes) or not v or len(v) > 32768
                    for k, v in artifacts.items())):
            raise ValueError("invalid bounded implementation artifacts")
        return {k: sha(v) for k, v in artifacts.items()}

    def promote(self, review, *, artifacts, developer, new_batch_id, active_batch_ids, current_base_digests):
        if (self._reviews.get(review.transaction_id) != review or not developer or not new_batch_id or
                new_batch_id == self.source_batch_id or new_batch_id in active_batch_ids or
                new_batch_id in self._promoted or current_base_digests != self._base or
                self._artifacts(artifacts) != dict(review.artifacts)):
            raise ValueError("promotion requires exact reviewed/tested artifacts and a fresh inactive batch")
        decision = self._decisions[review.transaction_id][0]
        promotion = Promotion(new_batch_id, developer, tuple(sorted(self._base.items())), review.artifacts,
                              review.tests_digest, review.review_digest, decision.proposal_digest)
        self.recorder.append("diagnostics", {"kind": "developer_promotion", **promotion.__dict__})
        self._promoted.add(new_batch_id)
        return promotion
