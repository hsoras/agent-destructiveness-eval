# Checkpointing

`BatchedSource` assigns stable source IDs and maps absolute positions to batch
cursors. A checkpoint stores the cursor, processed prefix, and current window.
The checkpoint store serializes immutable generations and returns handles that
include the slot, generation, digest, and handle identity.

The CSV workflow processes an initial prefix, verifies a canceled preview,
verifies and commits a second preview, restores the earlier checkpoint, and
replays overlapping pages. A repeated delivery is identified by source ID so
distinct observations with equal timestamps remain distinct. These checkpoints
are in memory only; the package does not provide crash-safe persistence.
