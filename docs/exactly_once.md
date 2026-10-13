# Exactly-once writes

An Iceberg target can be written in two ways:

- **Direct mode** is a plain `Iceberg.put(df, mode)`, as the [hardening contract](design/v0_1_1.md)
  specified it, with three fixes. Several processes on one laptop can append, overwrite and
  upsert safely. On a shared catalog that other machines also write, only appends are safe.
- **Staged mode** is `Iceberg.put(df, mode, commit=CommitContext(...))`. It is for several writers,
  retries and re-runs: each idempotency key has at most one effect on `main`.

This is contract C3 of the [v0.1.1 platform contract](design/v0_1_1_platform.md), and the
protocol from §7 of the [SaaS architecture](design/saas_architecture.md). The code is in
`format/iceberg/__init__.py` and `format/iceberg/commit.py`.

## Direct mode

Appending the same batch twice gives duplicates, and overwriting twice doesn't, exactly as in the
hardening contract. What changed:

- **One commit per write.** When a batch brings new columns, the schema union and the data write
  run in one `table.transaction()`, so a reader never sees the new schema without the data. (An
  `overwrite` still adds two snapshots, a delete and an append, in that one commit.)
- **Counts from metadata.** `WriteResult.rows_before` and `rows_after` come from the snapshots'
  `total-records` summaries, not from a scan.
- **A lock on local catalogs.** On a `local` catalog, `overwrite` and `upsert` hold an exclusive
  file lock, `<warehouse>/.ldp/locks/<namespace>.<table>.lock` (`fcntl` on POSIX, `msvcrt` on
  Windows), for their read-modify-write. Two processes on one machine can't interleave.
- **Appends retry.** `append` takes no lock, because concurrent appends only add files. They still
  race to commit, and the catalog refuses the commit of the loser. That append reloads the table,
  waits a random backoff and runs again on the new head. It stops after
  `CommitPolicy.max_publish_attempts` tries (6 by default) or at `deadline_s`, and then `put`
  raises a `CommitConflict` that names the table and the number of tries. `WriteResult.attempts`
  says how many tries a write took. pyiceberg 0.12 also retries inside its own commit, and
  pyiceberg 0.11 doesn't, which is why LDP does. A refused try leaves its data files in storage
  without a snapshot, and `ldp maintain` lists them as orphans.
- **An unknown outcome is never retried.** If the catalog says it can't tell whether a commit
  landed (`CommitStateUnknownException`), the first try may have succeeded. A direct write has no
  idempotency key to check, so `put` raises instead of risking a duplicate. Use staged mode when you
  need to recover from that.

The lock only covers processes that share the warehouse folder. On a `sql`, `rest` or `glue`
catalog that other machines also write, direct `overwrite` and `upsert` are single-writer modes:
use staged mode for them. Direct `append` is safe for several writers on any catalog.

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
   `CommitSearchExhausted` instead of risking a second effect. This is the search of the publish loop, and
   its two bounds are right for a rival that publishes the same key at the same moment. A caller that
   must not read its source twice asks `find_published` first, which has no bound but the history the table
   still holds, see [Long backfills and late re-runs](#long-backfills-and-late-re-runs).
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
`deadline_s=300.0` and `max_search_depth=500`. `Iceberg.put`, `Pipeline.run` and `run_config` all take `policy=`. Running out raises `CommitConflict` with
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

### From the command line

`ldp run` takes the same window and retry identity, so cron, Airflow or a shell script can run and retry a window
without loading its rows twice:

```bash
ldp run rides.json --window 2026-08-01/2026-08-02                 # published
ldp run rides.json --window 2026-08-01/2026-08-02                 # skipped_duplicate, and the rows are unchanged
ldp run rides.json --window 2026-08-01/2026-08-02 --run-id RUN --attempt 2   # a retry of run RUN
ldp run rides.json --idempotency-key sales-2026-08-01             # a key from the orchestrator, used as given
ldp run rides.json --window 2026-08-01/2026-08-02 --json          # PipelineResult.to_dict() on stdout
ldp run rides.json --window yesterday                             # the local day before today; also last-week, last-month
```

A staged run prints its `window`, `idempotency_key`, `run_id`, `attempt`, `status` and `snapshot`. The key is the one
`ldp plan rides.json --window 2026-08-01/2026-08-02` prints, and `--idempotency-key auto` says to use it. `--run-id`
and `--attempt` belong to a staged run, so they need a window or a key, and `--attempt` needs `--run-id`. A malformed
window, or one of these combinations that does not make sense, exits with 1 and one line. A quality check that blocks
the run exits with 3, and `ldp run` exits with 0 for a skipped duplicate, so a scheduler can retry exit 1, never
retry exit 3, and treat a re-run of a finished window as done. The exit-code table is in the
[README](https://github.com/tusharchou/local-data-platform#use-it-from-the-command-line).

### What a window reads

A window is the key of a run, and it also picks what the run reads, when the config says how: a
`{window_start:FORMAT}` placeholder in `metadata.source.path` names the file of the window, and `metadata.window.column`
keeps only the rows of the window. The rules are in the
[contract](design/v0_1_1.md#windows-and-schedules-013). Without either, a window only scopes the key and the run loads the
whole source, so three daily windows over one file that holds three days of rows write 9 rows. With
`"window": {"size": "day", "column": "ordered_at"}` they write 3, and with the write mode `overwrite` a run replaces only
the rows of its window.

`yesterday`, `last-week` (the last full Monday-to-Monday week) and `last-month` are counted in the time zone of
`metadata.window`, and `ldp run` prints the window each one stands for. A published window is skipped when it runs again,
so to replace a day that changed at the source, run it with a key of your own:

```bash
ldp run orders.json --window 2026-08-02/2026-08-03 --idempotency-key orders-2026-08-02-v2
```

### Long backfills and late re-runs

A staged run looks for its idempotency key twice, and the two searches answer different questions.

**Before it reads its source,** the run asks the table whether the key is on `main` anywhere in the history that the
table still holds, with `find_published`. There is no time limit and no limit on the number of snapshots. If the key is
there, the run skips. Its result is `skipped_duplicate` with the snapshot that carries the key, `rows_read` is 0, and the
source is never opened. So a window that ran 8 days ago, or 600 commits ago, is skipped as surely as one that ran a
minute ago, for as long as its snapshot is in the table. `ldp maintain --expire` keeps every keyed snapshot for the
idempotency horizon, 7 days by default, and may expire it after that.

**Inside the publish loop,** `find_commit` looks again before each publish, back to `search_since_ms` and over at most
`max_search_depth` snapshots. It catches a rival that publishes the same key between the first search and the publish,
and a rival can only have committed recently. The default bounds are 7 days and 500 snapshots, and a long backfill
exceeds them. Every snapshot of the batch is newer than 7 days, so the walk never reaches the time bound, and the
501st daily window of a batch walked 500 snapshots without reaching the start of the table and refused to publish
rather than risk a second effect. `etl.batch_search` gives a batch bounds that fit it:

```python
from local_data_platform.etl import batch_search, run_config

windows = [f"2026-08-{day:02d}/2026-08-{day + 1:02d}" for day in range(1, 29)]
since_ms, policy = batch_search(len(windows))
for window in windows:
    run_config("orders.json", window=window, policy=policy, search_since_ms=since_ms)
```

`since_ms` is the start of the batch less 10 minutes, for clock skew between machines. `policy` is the default policy
with a `max_search_depth` that also holds every snapshot the batch can add, 2 for each window (an `overwrite` or an
`upsert` adds a delete and an append) and one for the first write of a new table. The key of a window that ran before the
batch is found by the first search, so the second search only has to cover the batch itself. A second run of the batch
skips every window. A single run that is not part of a batch needs none of this, unless its table gets more than 500
snapshots in 7 days. Then pass `policy=CommitPolicy(max_search_depth=...)` or `search_since_ms=`.

**A key that is gone.** The first search is no proof that a window never ran. If the snapshot of its first publish expired,
its key is gone and the window would load again. When the key is not there and the window is older than every window that
the table still holds a key for, the run logs a warning and carries on. The warning names `ldp backfill --replace`, which
replaces a window instead of adding to it. Until that command exists, redo the window under a key of your own, with a
window column and the write mode `overwrite`, as in [What a window reads](#what-a-window-reads).

## What it guarantees

- **At most one effect on `main` per idempotency key**, as long as the key's snapshot is still in the
  table. A run looks for its key in the whole retained history before it reads its source, and the
  publish loop looks again over the recent history. With retries, that is effectively once.
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
- `tests/test_cli.py` covers `ldp run --window`: the printed window, key, status and snapshot, a second run that
  skips, the key that `ldp plan` prints, a retry with a higher attempt that fences the staged branch an earlier
  attempt left, `--json`, and the exit status 3. Two processes that start at the same moment with the same key, on
  the `local` catalog and on a `sql` catalog on SQLite, write the batch once.
- `tests/test_windows.py` covers what a window reads: the window shortcuts with a fixed clock, across month ends and clock
  changes, the source path placeholders, the rows of a window and the replacement of one day, in each time zone, the
  freshness check at the end of the window, and the three-days probe, where three daily windows over one file write 3 rows
  and not 9.
- `tests/test_commit.py` and `tests/test_etl.py` cover the key in the whole retained history. A window that ran 8 days
  ago is skipped and its table does not change, where it used to publish again and go from 4 rows to 6. A skipped run does
  not read its source and emits three events. A batch of more windows than the search depth fails closed with the default
  bounds, publishes every window with `batch_search`, and publishes none the second time. The `policy` of `run_config`
  reaches the publish loop, and a window older than every held key warns.
- `tests/test_upsert_race.py` runs N processes upserting the same keys into one table, three ways.
  Raw pyiceberg `Table.upsert` duplicates keys: pyiceberg's commit retry rebases each loser's
  insert onto the new head, so the race is real. Direct mode on a `local` catalog is serialised by
  the file lock. Staged mode, with no lock, loses no rows, duplicates no keys and records exactly
  one effect per key. A barrier makes every round race, so the result doesn't depend on timing.
  `LDP_RACE_PROCS` (default 4) and `LDP_RACE_ROUNDS` (default 5) size it.

Both run offline in the normal test suite. They use SQLite catalogs; other catalogs are not yet
covered by a race test.
