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
- The integration bundle is served by the local fixture service. No separate
  backup service is configured in the development environment; the adjacent
  station telemetry checkout is separate project data, not a copy of this
  fixture.
