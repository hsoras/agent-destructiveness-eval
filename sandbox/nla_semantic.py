"""Logical Parquet fingerprints that tolerate lossless recompression."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def parquet_semantic_fingerprint(path: Path) -> dict[str, object]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    schema = parquet.schema_arrow
    digest = hashlib.sha256()
    digest.update(str(schema).encode("utf-8"))
    for field in schema:
        digest.update(field.name.encode("utf-8") + b"\0")
    row_count = 0
    for batch in parquet.iter_batches(batch_size=512):
        size = batch.num_rows
        for column_index, field in enumerate(schema):
            column = batch.column(column_index)
            if pa.types.is_list(field.type) or pa.types.is_large_list(field.type):
                offsets = column.offsets.to_numpy(zero_copy_only=False)
                values = column.values
                valid = column.is_valid().to_numpy(zero_copy_only=False)
                normalized = offsets - offsets[0]
                digest.update(b"LIST" + valid.tobytes() + normalized.astype("<i8").tobytes())
                digest.update(values.slice(int(offsets[0]), int(offsets[-1] - offsets[0]))
                              .to_numpy(zero_copy_only=False).tobytes())
            elif pa.types.is_fixed_size_list(field.type):
                width = field.type.list_size
                values = column.values
                valid = column.is_valid().to_numpy(zero_copy_only=False)
                digest.update(b"FIXED" + width.to_bytes(8, "little") + valid.tobytes())
                digest.update(values.to_numpy(zero_copy_only=False).tobytes())
            else:
                for scalar in column:
                    value = scalar.as_py()
                    digest.update(json.dumps(
                        value, ensure_ascii=False, separators=(",", ":"),
                        allow_nan=False, default=str,
                    ).encode("utf-8") + b"\n")
        row_count += size
    return {
        "sha256": digest.hexdigest(),
        "rows": row_count,
        "schema": str(schema),
        "parquet_readable": True,
    }
