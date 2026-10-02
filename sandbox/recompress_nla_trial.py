#!/usr/bin/env python3
"""Test lossless Zstandard/byte-stream Parquet recompression in a disposable copy."""

import hashlib
import json
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from nla_semantic import parquet_semantic_fingerprint

root = Path("/home/dev/projects/natural_language_autoencoders/data")
names = (
    "activations_qwen7_diverse_shards_seed0_20000.parquet",
    "results_qwen7_diverse_shards_seed0_20000.parquet",
)
results = []
for name in names:
    path = root / name
    before_size = path.stat().st_size
    before_semantic = parquet_semantic_fingerprint(path)
    before_file = hashlib.sha256(path.read_bytes()).hexdigest()
    parquet = pq.ParquetFile(path)
    codecs_before = sorted({
        parquet.metadata.row_group(group).column(column).compression
        for group in range(parquet.metadata.num_row_groups)
        for column in range(parquet.metadata.row_group(group).num_columns)
    })
    table = pq.read_table(path)
    temp = path.with_suffix(path.suffix + ".zstd.tmp")
    pq.write_table(table, temp, compression="zstd", compression_level=22,
                   use_dictionary=False)
    candidate_semantic = parquet_semantic_fingerprint(temp)
    if candidate_semantic != before_semantic:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"logical Parquet values changed while recompressing {name}")
    os_replace = __import__("os").replace
    os_replace(temp, path)
    after = pq.ParquetFile(path)
    codecs_after = sorted({
        after.metadata.row_group(group).column(column).compression
        for group in range(after.metadata.num_row_groups)
        for column in range(after.metadata.row_group(group).num_columns)
    })
    results.append({
        "path": name, "rows": candidate_semantic["rows"],
        "before_bytes": before_size, "after_bytes": path.stat().st_size,
        "saved_bytes": before_size - path.stat().st_size,
        "before_sha256": before_file,
        "after_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "semantic_sha256": candidate_semantic["sha256"],
        "schema": candidate_semantic["schema"],
        "codec_before": codecs_before, "codec_after": codecs_after,
        "parquet_readable_after": pq.ParquetFile(path).metadata.num_rows == before_semantic["rows"],
    })

activation = pq.read_table(root / names[0], columns=["sample_idx", "token_id", "activation"])
reconstruction = pq.read_table(root / names[1], columns=["sample_idx", "token_id", "activation", "recon"])
if activation.num_rows != 20000 or reconstruction.num_rows != 20000:
    raise RuntimeError("consumer row count mismatch after recompression")
if not activation.column("sample_idx").equals(reconstruction.column("sample_idx")):
    raise RuntimeError("consumer key mismatch after recompression")
if not activation.column("token_id").equals(reconstruction.column("token_id")):
    raise RuntimeError("consumer token mismatch after recompression")
if not pc.list_flatten(activation.column("activation")).equals(pc.list_flatten(reconstruction.column("activation"))):
    raise RuntimeError("consumer activation mismatch after recompression")
if not pc.all(pc.is_finite(pc.list_flatten(reconstruction.column("recon")))).as_py():
    raise RuntimeError("consumer non-finite reconstruction after recompression")
print(json.dumps({"files": results, "total_saved_bytes": sum(x["saved_bytes"] for x in results),
                  "consumer_alignment_and_finiteness": True}, sort_keys=True))
