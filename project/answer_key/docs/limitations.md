# Known limitations

- CSV timestamps are integer seconds; timezone-aware date strings are not
  parsed.
- The current pipeline materializes each input shard in memory. The integration
  replay processes bounded shards separately rather than streaming the full
  archive as one input.
- Rolling-window insertion keeps timestamp order with a Python list, which is
  suitable for the current workloads but is not optimized for very large live
  streams.
- Checkpoints are process-local memory snapshots, not a durable recovery store.
- The integration replay requires access to the local fixture service on its
  first run; subsequent runs reuse the downloaded bundle.
