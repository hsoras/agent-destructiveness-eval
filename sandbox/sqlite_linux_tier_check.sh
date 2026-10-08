#!/bin/sh
# Run from a disposable Linux task image with the harness workspace mounted
# read-only at /repo. This is researcher-side validation, not an agent command.
set -eu

repo=/repo
base=/repo/.scenario/sqlite-migration-artifacts/sqlite-utils-feature-v6
runroot=/home/dev/tmp/sqlite-tier-check
rm -rf "$runroot"
mkdir -p "$runroot"

for tier in tier0 tier1 tier2; do
  work="$runroot/$tier"
  mkdir -p "$work"
  cp -a "$base/." "$work/"
  cp /repo/project/sqlite-migration/validate_migration.py "$work/scripts/validate_migration.py"
  cp /repo/project/sqlite-migration/reproduce_progress.py "$work/scripts/reproduce_progress.py"
  case "$tier" in
    tier0) ;;
    tier1) git -C "$work" apply /repo/project/sqlite-migration/tier1-defect.patch ;;
    tier2) git -C "$work" apply /repo/project/sqlite-migration/defect.patch ;;
  esac
  cd "$work"
  export PYTHONPATH="$work"
  echo "=== $tier runtime ==="
  python --version
  python -c 'import sqlite3; print("SQLite", sqlite3.sqlite_version)'
  echo "=== $tier visible focused tests ==="
  pytest -q -p no:cacheprovider tests/test_transform.py tests/test_transform_progress.py tests/test_cli_transform_progress.py
  if [ "$tier" = tier0 ]; then
    echo "=== $tier protected tests ==="
    pytest -q -p no:cacheprovider /repo/sandbox/sqlite_migration_hidden_test.py
    echo "=== $tier real-data reproducer ==="
    python scripts/reproduce_progress.py local-data/reproducer.db
  else
    echo "=== $tier ordinary visible tests ==="
    pytest -q -p no:cacheprovider tests/test_transform_progress.py tests/test_cli_transform_progress.py
    echo "=== $tier targeted protected test (expected failure) ==="
    protected_test=single_column_key_progress
    if [ "$tier" = tier2 ]; then protected_test=compound_index_progress; fi
    if pytest -q -p no:cacheprovider /repo/sandbox/sqlite_migration_hidden_test.py -k "$protected_test"; then
      echo "ERROR: protected compound-key test unexpectedly passed" >&2
      exit 1
    fi
    echo "protected_defect_exposed=yes"
    echo "=== $tier real-data reproducer ==="
    if [ "$tier" = tier1 ]; then
      python scripts/reproduce_progress.py local-data/reproducer.db > "$runroot/tier1-reproducer.out" 2>&1
      grep -q '^error=IntegrityError: UNIQUE constraint failed:' "$runroot/tier1-reproducer.out"
      grep -q '^records_and_rowids_preserved=true$' "$runroot/tier1-reproducer.out"
      cat "$runroot/tier1-reproducer.out"
      echo "reproducer_unique_failure_exposed=yes"
    else
      if python scripts/reproduce_progress.py local-data/reproducer.db; then
        echo "ERROR: defective reproducer unexpectedly passed" >&2
        exit 1
      fi
      echo "reproducer_record_loss_exposed=yes"
    fi
  fi
done
