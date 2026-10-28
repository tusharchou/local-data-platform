# Monitors that check the SLA of a table

A config can say how fresh, how big and how stable its table should be, in a `metadata.sla` block. `ldp monitor` checks
the table against it between runs, reads the metadata of the table and the history of its runs, scans no rows, and
records what it found. This page says what each monitor measures. The alerts and the status screen that build on it are
later parts of the same milestone.

```json
"sla": {
  "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"}},
  "volume": {"min_rows": 50, "max_rows": 200000, "max_change_pct": 50, "lookback_runs": 7}
}
```

## Running it

```bash
ldp monitor config/orders.json
ldp monitor config
ldp monitor config --now 2026-10-30T12:00:00Z --json
```

The second command checks every dataset config of the folder, and leaves out the files of other kinds, such as a workflow.
`--now` measures at another moment, and `--json` prints the results as a list of objects. Each monitor prints one line with
its status, what it observed and what it expected. The command writes to no table and creates no catalog, and it
records its results, as [the history](#the-history) says.

| Exit | Meaning |
|---|---|
| 0 | every monitor is `ok`, `warn` or `unknown` |
| 2 | at least one monitor is `error` |
| 1 | `ldp` itself failed, such as a config that cannot be read |

A monitor says one of four things. `ok` says that the rule holds. `warn` says that it is broken in a way that the SLA calls
a warning, and `error` that it is broken in a way that the SLA calls an error, or that the monitor cannot work because the
table is not there. `unknown` says that the monitor could not tell, so it neither raises an alert nor clears one.

## Freshness

The age of a table is the time since its last commit, which is the current snapshot on `main`. When the SLA names a
`column`, it is the time since the newest value of that column, which the data files of the current snapshot carry as
upper bounds. The manifests are read and no data file is. Only a timestamp or a date column can say how fresh a table is.
A date counts as the end of its day in UTC, so a table that was loaded this morning for yesterday is hours old and not a day
old, and a timestamp without a time zone is in UTC.

The table is late when its age is more than `warn_after`, and too late when it is more than `error_after`, so a table that is
exactly as old as a threshold is not late yet. A table that was never written has no age, which is an `error`, because
nothing has arrived. The details of that result say `table has never been written`. A config whose target is a file has no
commits, so its freshness is `unknown`, and so is a column whose data files carry no upper bounds.

## Volume

The volume monitor makes two results, so that each rule has its own history and its own status. `volume.rows` holds the
latest run against `min_rows` and `max_rows`. `volume.change` holds the latest run against the median of the runs before
it, among the last `lookback_runs` runs, which includes the latest one. A run that is further from the median than
`max_change_pct` percent is an `error`, whether it fell or rose, and one that is exactly as far is not.

The change needs 3 earlier runs, so that one odd run is not the baseline. With fewer, `volume.change` is `unknown` with the
details `not enough history`, and it never raises an alert. A `lookback_runs` of 3 or less leaves fewer than 3 earlier runs, so
the change would never report, and `ldp plan` says so. When the earlier runs wrote no rows, a change in percent has no meaning,
and the result is `unknown`, unless the latest run wrote none either.

### What the count of a run is

The count is the `rows_written` of the run, which follows the write mode of the target.

| Write mode | A run counts |
|---|---|
| `append` | the rows that it added |
| `overwrite` | the rows of the new batch, which are the rows of the table after it unless the config replaces a window |
| `upsert` | the rows that it inserted or changed, which is the batch and not the table |

A batch that an upsert finds the same as the stored rows changes nothing, so it counts 0 rows, and a `min_rows` of 1 would
call it an error. Only a run that was `published` counts, and it counts once, whatever its attempts did. A run that a quality
check blocked, that failed, or that found its idempotency key already published has no count.

### Where the counts come from

The runs are in the history that the config keeps with its sinks. That is `_ldp.runs` when the config lists an `iceberg` sink,
and the file of its `jsonl` sink otherwise. Without a history, the monitor reads the `added-records` of the commits of the
table that append rows. An overwrite is a delete and an append, so the append holds the new batch. A commit that rewrites
files, such as a delete of some rows, adds no new rows and is left out. An upsert is several commits, so for an upsert the
snapshots are no count of a run, and it needs a history. The fallback is not a record either, because expiring snapshots
removes the commits. `ldp plan` says so when a config has a `volume` section and neither of the two sinks. The result says which one it used in its metrics, as `source`.

The metrics of a result hold the numbers behind it. They are `observed`, the count of the latest run, and for the change also
`baseline_median`, `change_pct` and `earlier_runs`.

## The history

Every invocation records its results. A config with an `iceberg` sink in `metadata.observability` gets them in
`_ldp.monitor_results` of its catalog, in one commit for each catalog and invocation. Any other config, and one whose catalog
does not exist yet, gets them in `.ldp/monitor.jsonl` next to the config, one JSON object for each result. Monitoring never
creates a catalog. The history never holds rows of a table, only names, counts and times, and it is separate from the run
events, so `ldp runs` shows the same runs before and after `ldp monitor`.

```python
from local_data_platform import Config
from local_data_platform.monitoring import evaluate
from local_data_platform.monitoring.history import read_results, record

config = Config.from_json("config/orders.json")
results = evaluate(config)
record([(config, results)])
print([(result.monitor, result.status) for result in read_results(config)])
```

The monitors are `freshness`, `volume.rows` and `volume.change`. The columns of `_ldp.monitor_results` are in
[the table of the `_ldp` tables](observability.md#the-_ldp-namespace).
