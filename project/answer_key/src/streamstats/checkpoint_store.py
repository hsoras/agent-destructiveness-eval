"""Versioned in-memory storage for resumable processor checkpoints."""

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


class CheckpointStore:
    """Retain immutable serialized checkpoint generations in memory."""

    def __init__(self) -> None:
        self._payloads: dict[tuple[str, int], bytes] = {}
        self._cache: dict[tuple[str, int], bytes] = {}

    @staticmethod
    def _cache_key(slot: str, generation: int) -> object:
        return (slot, generation)

    @staticmethod
    def _observation_payload(observation: Observation) -> dict[str, Any]:
        return {"timestamp": observation.timestamp, "value": observation.value}

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
            if not isinstance(value, dict) or "timestamp" not in value or "value" not in value:
                raise CheckpointError("checkpoint payload is malformed")
            timestamp = value["timestamp"]
            raw_value = value["value"]
            if not isinstance(timestamp, int) or isinstance(timestamp, bool):
                raise CheckpointError("checkpoint payload is malformed")
            if raw_value is not None and (
                not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool)
            ):
                raise CheckpointError("checkpoint payload is malformed")
            observations.append(Observation(timestamp, raw_value))
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

    def save(self, slot: str, checkpoint: Checkpoint) -> CheckpointHandle:
        """Persist a new immutable generation without warming the read cache."""

        if not slot:
            raise ValueError("checkpoint slot must not be empty")
        generations = [
            generation
            for saved_slot, generation in self._payloads
            if saved_slot == slot
        ]
        generation = max(generations, default=0) + 1
        payload = self._serialize(checkpoint)
        self._payloads[(slot, generation)] = payload
        digest = hashlib.sha256(payload).hexdigest()
        return CheckpointHandle(slot, generation, digest)

    def load(self, handle: CheckpointHandle) -> Checkpoint:
        """Load and independently decode the exact generation named by a handle."""

        key = self._cache_key(handle.slot, handle.generation)
        payload = self._cache.get(key)
        if payload is None:
            payload = self._payloads.get((handle.slot, handle.generation))
            if payload is None:
                raise CheckpointError("checkpoint handle was not found")
            self._cache[key] = payload

        if hashlib.sha256(payload).hexdigest() != handle.payload_digest:
            raise CheckpointError("checkpoint payload does not match requested handle")
        return self._deserialize(payload)
