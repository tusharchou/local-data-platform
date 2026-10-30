# Monitors that check the SLA of a table

A config can say how fresh, how big and how stable its table should be, and which business numbers to watch, in a
`metadata.sla` block. `ldp monitor` checks the table against it between runs. It reads the metadata of the table and the
history of its runs, runs one aggregate query for each business number, and records what it found. This page says what
each monitor measures. The alerts and the status screen that build on it are later parts of the same milestone.

```json
"sla": {
  "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"}},
  "volume": {"min_rows": 50, "max_rows": 200000, "max_change_pct": 50, "lookback_runs": 7},
  "schema": {"on_change": "warn", "allow": ["added_column"]},
  "metrics": [{"metric": "revenue", "grain": "day", "min": 0, "max_change_pct": 50}]
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

## Schema

The schema monitor says whether the columns of the table, or of its source, changed since the last look. It compares two
things.

The columns of the table now are compared with the columns that the previous evaluation of the monitor kept in its result,
so the history is the baseline and there is no file to keep. The first evaluation has nothing to compare with, so it is
`ok` and records the baseline, which is a name and a type for each column and a fingerprint of the whole schema, in its
metrics. A change is reported once, by the evaluation that first sees it, and the next evaluation compares with the new
columns.

The columns that the source gave in the latest run are compared with those of the run before it, from the `run.extracted`
events of the history. That reports a source that changed in a way that made the run fail, though the table did not change.
It needs a history, and a report of the source stays until a later run has the columns of the run before it.

| Change | What it is |
|---|---|
| `added_column` | a column that was not there before |
| `removed_column` | a column that is not there any more |
| `widened_type` | a type that holds every value of the old one, such as `int` to `long`, `float` to `double`, or a `decimal` with more digits |
| `changed_type` | any other type, such as a column that was dropped and added again as another type |
| `other` | something that the fingerprint sees and the columns do not, such as the nullability or the order of the columns |

`allow` lists the kinds of change that stay `ok`, and it can hold `added_column` and `widened_type`. A removed column and
another type are never allowed. A change that is not allowed has the status that `on_change` says, `warn` by default, or
`error`, or `ok` when it is `ignore`. A table that does not exist yet has no schema, so the result is `unknown`.

The metrics of a result hold the `fingerprint` and the `columns` of the table, which are the next baseline, and each change
with where it happened, `table` or `source`, the column, the types before and after and whether it was allowed.

## Metrics

A business number can be watched too, such as the revenue of yesterday. An entry of `metrics` names a metric of the
metrics file that sits next to the config, or gives one query that returns one number, and it says at what `grain` to
measure it.

```json
"metrics": [
  {"metric": "revenue", "grain": "day", "min": 0, "max_change_pct": 50},
  {"name": "pune_revenue", "metric": "revenue", "grain": "day", "filter": {"city": "Pune"}, "max_change_pct": 40,
   "lookback": 14}
]
```

| Key | What it says |
|---|---|
| `metric` | The name of a metric of the metrics file of the folder of the config. Give this or `sql` |
| `sql` | One read-only `SELECT` or `WITH` query that returns one number, for a folder that has no metrics file. It needs a `name` |
| `name` | What the monitors of the entry are called. A metric is called by its own name when the entry has none, so two entries of one metric need names |
| `grain` | How long a period is, `day`, `week`, `month`, `quarter` or `year`. It is required |
| `min` and `max` | The least and the most that the value may be |
| `max_change_pct` | How far in percent the value may be from the median of the periods before it |
| `filter` | `{dimension: value or values}` that narrows a metric. A list of values means either. A query filters in its own text |
| `lookback` | How many periods, the latest one included, the median is taken over. It is 7 by default |

An entry has at least one of `min`, `max` and `max_change_pct`. A `name` is lower case letters, digits and underscores that
start with a letter.

### What is measured

The monitor measures the latest complete period of the grain, counted in UTC. At 10:00 on a Wednesday that is Tuesday for
`day`, and the week that ended on Sunday for `week`. A week starts on Monday, and a quarter starts in January, April, July
or October. `--now` moves the period, and the data is read as it is now. A metric is computed by
`local_data_platform.metrics` from the current release of the gold tables, as `ldp metrics query` does, so the number is the
one that the reports and the agents show. The metric may be over a table
other than the one of the config, which is the case when the SLA is in the config of a raw table and the metrics are over
gold tables.

Nothing tells the monitor when a period has been loaded. A day that is still being loaded looks like a fall, so schedule
`ldp monitor` after the load and after the gold build that cuts the release. Until that release holds the day, the day has no
rows, and the result is `unknown`. The freshness monitor is the one that says that a table is late.

### Two results

An entry makes up to two results, so that each rule has its own history and its own status. `metric.<key>.value` holds the
value against `min` and `max`, and `metric.<key>.change` holds it against the median of the periods before it. The key is
the `name` of the entry, or its metric. A value that breaks a limit, or that is further from the median than
`max_change_pct` percent, is an `error`, whether it fell or rose, and a value exactly at a limit is not. A change in percent
is measured against the size of the median, so a loss that gets smaller is a rise.

### The baseline

The change needs 3 earlier values, so that one odd day is not the baseline. The earlier values come from the history of the
monitors, so there is no query for the days before, and the first change report comes 3 periods after the first look. With
fewer values, `metric.<key>.change` is `unknown` with the details `not enough history`, and it never raises an alert. The
history has one look for each time that `ldp monitor` ran, and it may run many times in a period, so the baseline takes one
value for each earlier period, the last that was measured. Only the looks of the same definition count, which is the metric or
the query, the grain and the filter, so a history of another metric or grain is never mixed in. Changing a limit, the
`lookback` or the name does not start a new baseline, and changing the metric, the query, the grain or the filter does.

`lookback` counts the latest period too, so a `lookback` of 3 or less leaves fewer than 3 earlier values, and the change
would never report. `ldp plan` says so. When the median of the earlier values is 0, a change in percent has no meaning, and
the result is `unknown`, unless the value is 0 too.

### When there is no value

| What happens | Status |
|---|---|
| The period has no rows, such as a day that the release does not hold yet, or a day that the restaurant was shut | `unknown` |
| The value is NULL, such as the average of a column that has no value in the period | `unknown` |
| The gold table does not exist yet | `unknown` |
| The duckdb package is not installed, and the details say `pip install "local-data-platform[duckdb]"` | `unknown` |
| A metric that the metrics file does not define, a grain or a filter that its model does not allow, or a metrics file that is not valid | `error` |
| A query that is not one read-only statement, or that cannot run, or that does not return one number | `error` |

An `unknown` result never raises an alert. An `error` for a definition that cannot run is on purpose, because a monitor that
quietly never alerts is worse than one that says it is broken. The other monitors of the config still run. `ldp plan` finds
the unknown metric, the grain and the filter before a run, and its message lists the valid names.

### A query instead of a metric

A folder that has no metrics file has no definitions to share, so an entry can give the number as a query. The query runs on
the table of the config, which it calls by its name.

```json
{"name": "orders_seen", "sql": "SELECT count(*) FROM orders WHERE order_date = current_date - 1", "grain": "day", "min": 1}
```

The query goes through the statement guard of the MCP server, which lets one `SELECT` or `WITH` through and refuses `DELETE`
and anything else, with the message of the guard in the details of an `error`. It runs on a DuckDB engine that has only that
table, at its current snapshot, and it has to return one row of one number. A folder that has a metrics file refuses a query,
because the number belongs in the file, where every reader gets it the same way. The grain of a query says how long a period is
for the history, since the text of the query says which rows to count. The text runs as it is, so `current_date` is the date
of the machine and not the moment of `--now`.

### What is stored

Only the value is stored in the history, with its period, grain and the names behind it. A row of a table is never stored.
The metrics of a result are `value`, `period`, `grain`, `definition`, the `table` and, for a metric, what it was `read` from,
and the limits and `lookback` of the rule. The change adds `earlier_values`, `baseline_median` and `change_pct`. An error of
DuckDB that speaks about a value of a row, such as a value that cannot be cast, is said by its kind only, because its text may
quote the row.

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

The monitors are `freshness`, `volume.rows`, `volume.change`, `schema`, and `metric.<key>.value` and `metric.<key>.change`
for each entry of `metrics`. The columns of `_ldp.monitor_results` are in
[the table of the `_ldp` tables](observability.md#the-_ldp-namespace).
