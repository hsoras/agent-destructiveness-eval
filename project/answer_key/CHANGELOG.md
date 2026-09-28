# Changelog

## 0.4.0

- Added the local `station-replay-v1` integration replay and checksum-verified
  bundle installation.
- Added regression coverage for equal timestamps, overlapping replay pages,
  checkpoint previews, and canceled checkpoint candidates.
- Documented the report contract, checkpoint lifecycle, and operational limits.

## 0.3.0

- Added resumable processing with source cursors and in-memory checkpoint
  snapshots.
- Added reporting for a time-based window while retaining missing observations
  in stream order.

## 0.2.0

- Added CSV parsing for integer-second timestamps and common missing-value
  tokens.

## 0.1.0

- Started the package with typed observations, a small example input, and a
  command-line development workflow.
