# Exactly-once writes

An Iceberg target can be written in two ways:

- **Direct mode** is a plain `Iceberg.put(df, mode)`. It is what 0.1.1 did, with three fixes. It is
  safe for one writer per table, and for several processes on one laptop.
- **Staged mode** is `Iceberg.put(df, mode, commit=CommitContext(...))`. It is for several writers,
  retries and re-runs: each idempotency key has at most one effect on `main`.

This is contract C3 of the [v0.2.0 design](design/v0_2_0.md), and the protocol from §7 of the
[SaaS architecture](design/saas_architecture.md). The code is in `format/iceberg/__init__.py` and
`format/iceberg/commit.py`.

## Direct mode

Appending the same batch twice gives duplicates, and overwriting twice doesn't, exactly as in
0.1.1. What changed:

- **One commit per write.** When a batch brings new columns, the schema union and the data write
  run in one `table.transaction()`, so a reader never sees the new schema without the data. (An
  `overwrite` still adds two snapshots, a delete and an append, in that one commit.)
- **Counts from metadata.** `WriteResult.rows_before` and `rows_after` come from the snapshots'
  `total-records` summaries, not from a scan.
- **A lock on local catalogs.** On a `local` catalog, `overwrite` and `upsert` hold an exclusive
  file lock, `<warehouse>/.ldp/locks/<namespace>.<table>.lock` (`fcntl` on POSIX, `msvcrt` on
  Windows), for their read-modify-write. Two processes on one machine can't interleave. `append`
  takes no lock: concurrent appends don't conflict, because each one only adds its own files.

The lock only covers processes that share the warehouse folder. On a `sql`, `rest` or `glue`
catalog that other machines also write, direct mode is a single-writer mode: use staged mode.

## Staged mode

Pass a `CommitContext` and `put` runs `commit.write_once`:

```python
import pyarrow as pa

from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import CommitContext

fares = Iceberg("fares", {"identifier": "nyc", "warehouse_path": "warehouse"},
                write_mode="upsert", join_cols=["ride_id"])
batch = pa.table({"ride_id": [1, 2, 3], "fare": [12.5, 8.0, 30.25]})

context = CommitContext.create("fares/2024-01-01")   # a new uuid7 run id; searches 7 days back
first = fares.put(batch, commit=context)
again = fares.put(batch, commit=context)             # the same key: nothing is written
print(first.attempts, again.skipped_duplicate, again.snapshot_id == first.snapshot_id)   # 1 True True
print(first.branch)                                  # ldp_r<run id>_a1_0, the branch that was published
summary = fares.snapshots()[-1]["summary"]
print(summary["ldp.idempotency-key"], summary["ldp.attempt"])   # fares/2024-01-01 1
```

`CommitContext(run_id, attempt, idempotency_key, spec_hash, search_since_ms, logical_window=None,
source_positions={})` is the full form; `CommitContext.create(key, ...)` fills in a new run id and a
search horizon of 7 days. `put` also takes `overwrite_filter=` (replace only the rows of one window,
such as `"day = '2024-01-01'"`) and `policy=`.

A write goes through these steps:

1. **Fence.** Branches of superseded attempts (`fence_branches`, and every earlier attempt of the
   same run) are removed in one commit, and a fence marker is recorded for each, so a zombie
   attempt can never publish afterwards.
2. **Base.** A new table gets one empty `ldp.init` snapshot, so there is always a `main` to branch
   from.
3. **Find.** `find_commit` walks `main`'s history back to `search_since_ms`, looking for the key. If
   it is there, the write already happened: the result has `skipped_duplicate=True` and nothing is
   written. If the walk hits `max_search_depth`, or a parent that was expired, before it reaches
   the horizon or the first snapshot, it can't prove the key is absent, so it raises
   `CommitSearchExhausted` instead of risking a second effect.
4. **Stage.** The write goes to a private branch, `ldp_r<run id>_a<attempt>_<n>`, created at the base
   snapshot, with the schema union in the same transaction as the data.
5. **Audit.** `write_once(..., audit=)` can check the staged snapshot. A failed audit raises
   `DataQualityError` and keeps the branch for inspection; `main` is untouched.
6. **Publish.** One `Catalog.commit_table` call fast-forwards `main` to the staged snapshot. It
   asserts both `main == base` and `branch == staged`, so it fails if anyone moved `main` or fenced
   the branch in the meantime. It goes straight to the catalog because pyiceberg's
   `Transaction._stage` keeps only the first requirement of each type, and its commit retry would
   rebase the publish blindly.

Every snapshot a publish adds carries `ldp.idempotency-key`, `ldp.run-id` and `ldp.attempt`, plus
`ldp.spec-hash`, `ldp.logical-window` and `ldp.source.<name>` when they are set.

**When another writer wins.** If `main` moved first, the attempt resolves the conflict by mode and
tries again with full-jitter backoff:

| Mode | On a lost race |
|---|---|
| `append` | Re-stages on the new `main`, reusing the data files it already wrote |
| `overwrite` | Re-applied on the new `main`; a window overwrite replaces only its window |
| `upsert` | Recomputed against the new `main`'s rows, never rebased blindly |

`CommitPolicy` sets the limits: `max_publish_attempts=6`, `base_delay_s=0.2`, `max_delay_s=8.0`,
`deadline_s=300.0` and `max_search_depth=500`. Running out raises `CommitConflict` with
`retriable=True`; the caller may retry as `attempt + 1`. A fenced attempt raises `CommitConflict`
with `retriable=False` and must stop.

**Retries and zombies.** A retry of a run uses the same `run_id` with `attempt + 1`. Its first
commit fences every older attempt of that run, so if the old attempt wakes up later, its publish
fails, and re-running it finds the key the retry published and writes nothing.

## Pipelines and configs

`Pipeline.run(commit=)` passes the context to an Iceberg target, and the run's `status` says what
happened:

```python
from local_data_platform import Config
from local_data_platform.format.iceberg.commit import CommitContext
from local_data_platform.pipeline.registry import create_pipeline

pipeline = create_pipeline(Config.from_json("rides.json"))
first = pipeline.run(commit=CommitContext.create("rides/2024-01-02"))
again = pipeline.run(commit=CommitContext.create("rides/2024-01-02"))
print(first.status, again.status, again.published_snapshot_id == first.published_snapshot_id)
# published skipped_duplicate True
```

`run_config(path, window="START/END")` derives the key from the config with
`spec.idempotency_key(config, window)`: a hash of the pipeline, the target table and the window,
and deliberately not of the spec, so changing a spec and re-running a window still finds the
earlier commit. See [Run events and the `_ldp` namespace](observability.md#exactly-once-runs-and-idempotency-keys).

## What it guarantees

- **At most one effect on `main` per idempotency key**, as long as the key's snapshot is within the
  search horizon. With retries, that is effectively once.
- **No lost updates between writers.** Every publish asserts the base it was computed from.
- **Not end-to-end exactly once.** Rows a producer sent twice upstream are written twice, unless
  `upsert` on a key removes them.
- **Expiry must keep the keys.** `maintenance.expire_snapshots` never expires a snapshot that
  carries an idempotency key newer than the table's idempotency horizon: the
  `ldp.idempotency.horizon-days` table property, 7 days by default.

Staged mode needs branch writes, which arrived in pyiceberg 0.10. This package needs 0.11 or newer, so
staged and direct mode both work on every supported pyiceberg.

## Tests

- `tests/test_commit.py` covers each step: the `ldp.*` properties, re-running a key, a publish that
  asserts both refs, `find_commit` failing closed, conflict resolution for each mode, lost publish
  responses, fencing of zombie and superseded attempts, and a failed audit.
- `tests/test_upsert_race.py` runs N processes upserting the same keys into one table, three ways.
  Raw pyiceberg `Table.upsert` duplicates keys: pyiceberg's commit retry rebases each loser's
  insert onto the new head, so the race is real. Direct mode on a `local` catalog is serialised by
  the file lock. Staged mode, with no lock, loses no rows, duplicates no keys and records exactly
  one effect per key. A barrier makes every round race, so the result doesn't depend on timing.
  `LDP_RACE_PROCS` (default 4) and `LDP_RACE_ROUNDS` (default 5) size it.

Both run offline in the normal test suite. They use SQLite catalogs; other catalogs are not yet
covered by a race test.
