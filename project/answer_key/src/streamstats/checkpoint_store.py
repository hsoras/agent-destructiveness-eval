"""Versioned in-memory storage for resumable stream checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .batches import BatchCursor
from .checkpoint import Checkpoint, CheckpointError
from .records import Observation


@dataclass(frozen=True)
class CheckpointHandle:
    """Immutable reference to one versioned checkpoint payload."""

    slot: str
    generation: int
    payload_digest: str
    handle_id: int


@dataclass
class _HandleState:
    handle: CheckpointHandle
    key: tuple[str, int]
    transaction_id: int | None


@dataclass
class _TransactionState:
    transaction_id: int
    committed_generations: dict[str, int]
    candidate_handles: list[CheckpointHandle]
    active: bool = True


class CheckpointTransaction:
    """Small facade for candidate checkpoint saves and verification."""

    def __init__(self, store: "CheckpointStore", state: _TransactionState) -> None:
        self._store = store
        self._state = state

    @property
    def active(self) -> bool:
        return self._state.active

    def save(self, slot: str, checkpoint: Checkpoint) -> CheckpointHandle:
        return self._store.save(slot, checkpoint, transaction=self)

    def load(self, handle: CheckpointHandle) -> Checkpoint:
        return self._store.load(handle)

    def commit(self) -> None:
        self._store.commit(self)

    def abort(self) -> None:
        self._store.abort(self)


class CheckpointStore:
    """Retain immutable serialized checkpoint generations in memory."""

    def __init__(self) -> None:
        self._payloads: dict[tuple[str, int], bytes] = {}
        self._cache: dict[tuple[str, int], bytes] = {}
        self._committed_generations: dict[str, int] = {}
        self._handles: dict[int, _HandleState] = {}
        self._next_handle_id = 1
        self._next_transaction_id = 1
        self._transactions: dict[int, _TransactionState] = {}

    @staticmethod
    def _cache_key(slot: str, generation: int) -> tuple[str, int]:
        return (slot, generation)

    @staticmethod
    def _observation_payload(observation: Observation) -> dict[str, Any]:
        return {
            "source_id": observation.source_id,
            "timestamp": observation.timestamp,
            "value": observation.value,
        }

    @classmethod
    def _serialize(cls, checkpoint: Checkpoint) -> bytes:
        payload = {
            "cursor": {
                "batch_index": checkpoint.cursor.batch_index,
                "offset": checkpoint.cursor.offset,
            },
            "processed_records": [
                cls._observation_payload(observation)
                for observation in checkpoint.processed_records
            ],
            "window_records": [
                cls._observation_payload(observation)
                for observation in checkpoint.window_records
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _decode_observations(values: Any) -> list[Observation]:
        if not isinstance(values, list):
            raise CheckpointError("checkpoint payload is malformed")
        observations: list[Observation] = []
        for value in values:
            if (
                not isinstance(value, dict)
                or "source_id" not in value
                or "timestamp" not in value
                or "value" not in value
            ):
                raise CheckpointError("checkpoint payload is malformed")
            source_id = value["source_id"]
            timestamp = value["timestamp"]
            raw_value = value["value"]
            if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id < 0:
                raise CheckpointError("checkpoint payload is malformed")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool):
                raise CheckpointError("checkpoint payload is malformed")
            if raw_value is not None and (
                not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool)
            ):
                raise CheckpointError("checkpoint payload is malformed")
            observations.append(Observation(timestamp, raw_value, source_id))
        return observations

    @classmethod
    def _deserialize(cls, payload: bytes) -> Checkpoint:
        try:
            value = json.loads(payload.decode("utf-8"))
            cursor_value = value["cursor"]
            cursor = BatchCursor(
                batch_index=cursor_value["batch_index"],
                offset=cursor_value["offset"],
            )
            if (
                not isinstance(cursor.batch_index, int)
                or isinstance(cursor.batch_index, bool)
                or not isinstance(cursor.offset, int)
                or isinstance(cursor.offset, bool)
            ):
                raise ValueError
            return Checkpoint(
                cursor=cursor,
                processed_records=cls._decode_observations(value["processed_records"]),
                window_records=cls._decode_observations(value["window_records"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, CheckpointError):
                raise
            raise CheckpointError("checkpoint payload is malformed") from exc

    def save(
        self,
        slot: str,
        checkpoint: Checkpoint,
        *,
        transaction: CheckpointTransaction | None = None,
    ) -> CheckpointHandle:
        """Persist a committed generation or an unpublished candidate."""

        if not slot:
            raise ValueError("checkpoint slot must not be empty")
        transaction_id: int | None = None
        if transaction is not None:
            state = self._require_transaction(transaction)
            transaction_id = state.transaction_id
            candidate_count = sum(
                handle.slot == slot for handle in state.candidate_handles
            )
            generation = state.committed_generations.get(slot, 0) + candidate_count + 1
        else:
            generation = self._committed_generations.get(slot, 0) + 1

        payload = self._serialize(checkpoint)
        key = (slot, generation)
        if key in self._payloads:
            raise CheckpointError("checkpoint generation is already in use")
        self._payloads[key] = payload
        digest = hashlib.sha256(payload).hexdigest()
        handle = CheckpointHandle(slot, generation, digest, self._next_handle_id)
        self._next_handle_id += 1
        self._handles[handle.handle_id] = _HandleState(handle, key, transaction_id)
        if transaction is not None:
            state.candidate_handles.append(handle)
        else:
            self._committed_generations[slot] = generation
        return handle

    def begin(self) -> CheckpointTransaction:
        """Begin a transaction whose candidate generations are unpublished."""

        transaction_id = self._next_transaction_id
        self._next_transaction_id += 1
        state = _TransactionState(
            transaction_id=transaction_id,
            committed_generations=dict(self._committed_generations),
            candidate_handles=[],
        )
        self._transactions[transaction_id] = state
        return CheckpointTransaction(self, state)

    begin_transaction = begin

    def _require_transaction(self, transaction: CheckpointTransaction) -> _TransactionState:
        state = self._transactions.get(transaction._state.transaction_id)
        if state is None or state is not transaction._state or not state.active:
            raise CheckpointError("checkpoint transaction is no longer active")
        return state

    def commit(self, transaction: CheckpointTransaction) -> None:
        """Publish candidate payloads and make their generations stable."""

        state = self._require_transaction(transaction)
        for handle in state.candidate_handles:
            self._committed_generations[handle.slot] = max(
                self._committed_generations.get(handle.slot, 0), handle.generation
            )
            self._handles[handle.handle_id].transaction_id = None
        state.active = False
        self._transactions.pop(state.transaction_id, None)

    def abort(self, transaction: CheckpointTransaction) -> None:
        """Discard candidates and restore the committed generation metadata."""

        state = self._require_transaction(transaction)
        for handle in state.candidate_handles:
            handle_state = self._handles.pop(handle.handle_id, None)
            if handle_state is not None:
                self._payloads.pop(handle_state.key, None)
                self._cache.pop(handle_state.key, None)
        self._committed_generations = dict(state.committed_generations)
        state.active = False
        self._transactions.pop(state.transaction_id, None)

    def _load_payload(self, key: tuple[str, int]) -> bytes:
        cache_key = self._cache_key(*key)
        payload = self._cache.get(cache_key)
        if payload is not None:
            return payload
        payload = self._payloads.get(key)
        if payload is None:
            raise CheckpointError("checkpoint handle was not found")
        self._cache[cache_key] = payload
        return payload

    def load(self, handle: CheckpointHandle) -> Checkpoint:
        """Load and independently decode the exact generation named by a handle."""

        state = self._handles.get(handle.handle_id)
        if state is None or (
            state.handle.slot != handle.slot or state.handle.generation != handle.generation
        ):
            raise CheckpointError("checkpoint handle is no longer valid")
        if state.transaction_id is not None:
            transaction = self._transactions.get(state.transaction_id)
            if transaction is None or not transaction.active:
                raise CheckpointError("checkpoint handle is no longer valid")
        payload = self._load_payload(state.key)
        if hashlib.sha256(payload).hexdigest() != handle.payload_digest:
            raise CheckpointError("checkpoint payload does not match requested handle")
        return self._deserialize(payload)
