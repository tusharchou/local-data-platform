# Monitor the lakehouse with an SLA, alerts and one status screen on your laptop

**You will learn** how to say how fresh, how big and how stable a table should be, and which business number must stay above a floor. You will see a monitor catch a table that is late, a batch that is short, a column that appeared and a day of revenue that is far too low. You will see an alert that says it once and says it again when it is over, one screen that shows every pipeline, and a cron or launchd job that looks every 15 minutes while an outside service watches the watcher. You will also see what a monitor on a laptop cannot do.

**It takes** about 45 minutes. The commands run in about 20 seconds, and the rest is reading.

**You need** Python 3.12 or newer, git, and a clone of this repo, because the data comes from the restaurant example in `examples/restaurant`. The chapter uses the `dbt` and `excel` extras, as [the warehousing guide](warehousing.md) does, because Part 5 reads a revenue from a gold table. Nothing is downloaded after the install, and there is no server and no account. Slack and Healthchecks.io are optional, and no command of the chapter calls them. The test suite runs every command on this page, except the install, the two that change your computer's schedule and the one that deletes from the history, in `tests/test_docs_monitoring.py`, so the output you see here is what the code prints.

## The story

The restaurant chain's system exports the orders of the day every night, as a CSV file. Someone loads the file into `raw.orders`, and the reports read it the next morning. Nobody opens the table every morning, so a table can be wrong for days before a person notices. Five things go wrong sooner or later, and the chapter has a part for each of them.

1. A night's file never arrives, and the table goes stale.
2. A file arrives cut short, with 3 orders where a normal night has 8.
3. The export gains a column that nobody announced.
4. A night has the right number of orders, and every amount is a tenth of what it should be.
5. The monitor itself stops, because the laptop that runs it was asleep.

The chapter has eight parts, and they are what a small data team does about it.

1. Say what a good table looks like, in an SLA.
2. See a table that is late.
3. See a short batch.
4. See a new column.
5. See a day of low revenue.
6. Hear about each problem once, and hear when it is over.
7. Look at every pipeline on one screen.
8. Let cron or launchd look every 15 minutes, and let an outside service notice when the looking stops.

## Install

The restaurant example lives in the repo, so you work from a clone. This is the install of the warehousing guide.

<!-- docs-runner: skip it installs the package, and this run already has it -->
```bash
git clone https://github.com/tusharchou/local-data-platform.git
cd local-data-platform
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dbt,excel]"
ldp --version
```

## Make the data

Everything this guide creates goes into `ldp_demo/monitoring`, and git ignores `ldp_demo`, so you can delete the folder to start again. The generator uses a fixed seed, so you get the same files as this page shows. The option `--monitoring` writes the four small tables and the five nights of the story.

```bash
mkdir -p ldp_demo/monitoring
cd ldp_demo/monitoring
python ../../examples/restaurant/generate.py --out exports --monitoring
```

<!-- docs-runner: output -->
```text
Wrote 14 files to exports: the four small tables and 5 days of orders for the monitoring story, 8 orders on a normal day
```

A night is an orders file and an order lines file. Each normal night repeats the same 8 orders of the first week of August on a new date, so the numbers stay easy to follow.

| File | Rows | What it is |
|---|---|---|
| `restaurants.xlsx`, `customers.csv`, `menu_items.csv` and `reviews.csv` | 6, 150, 36 and 94 | The four small tables that the models of Part 5 read |
| `monitor_orders_2026-08-03.csv` | 8 | A normal night. Its orders add up to 6330 |
| `monitor_orders_2026-08-06.csv` | 3 | A night cut short, with the first 3 orders, which add up to 1950 |
| `monitor_orders_2026-08-07.csv` | 8 | A normal night, and the export has gained a column called `discount` |
| `monitor_orders_2026-08-08.csv` | 8 | The right orders, with every amount a tenth of what it should be, so they add up to 633 |
| `monitor_orders_2026-08-09.csv` | 8 | A normal night again, with the `discount` column |
| `monitor_order_lines_2026-08-03.csv` and the four dates after it | 19, 8, 19, 19 and 19 | The lines of the same nights, which Part 5 needs |

There is no file for 4 and 5 August, because the system sent nothing on those nights.

## Part 1. Say what a good table looks like

An SLA says in a few lines how fresh, how big and how stable a table should be. It sits in the `metadata.sla` block of the config of the table, next to its `source` and its `target`, and it changes nothing about what a run writes. `ldp monitor` checks the table against it between runs. It reads the metadata of the table and the history of its runs, runs one aggregate query for each business number, and records what it found.

Make the folders and take the configs of the example. The orders config is the one to watch.

```bash
mkdir landing
cp -R ../../examples/restaurant/config config
```

The block below adds three things to the config of the orders. The `sla` says what a good table looks like. The `iceberg` sink keeps the history that the monitors read, which lives in the `_ldp` tables of the catalog, and the `jsonl` sink is the one the config already had. The channel is a file that gets one line of JSON for each alert, so the chapter needs no Slack. The paths are relative to the folder of the config.

```python
import json
from pathlib import Path

path = Path("config/orders.json")
spec = json.loads(path.read_text())
spec["metadata"]["sla"] = {
    "freshness": {"warn_after": {"count": 26, "period": "hour"}, "error_after": {"count": 48, "period": "hour"},
                  "column": "order_ts"},
    "volume": {"min_rows": 5},
    "schema": {"on_change": "warn"},
}
spec["metadata"]["observability"] = {
    "sinks": ["iceberg", {"type": "jsonl", "path": "../.ldp/events.jsonl"}],
    "alerts": [{"type": "file", "path": "../.ldp/alerts.jsonl",
                "on": ["monitor.error", "monitor.warn", "monitor.resolved"]}],
}
path.write_text(json.dumps(spec, indent=2) + "\n")
```

The three rules say what the restaurant wants.

| Rule | What it says here |
|---|---|
| `freshness` | The table is late when its newest `order_ts` is more than 26 hours old, and too late when it is more than 48 hours old. The system exports every night, so the newest order of a healthy table is a night old, which is about half a day in the morning. 26 hours says that last night's file has not come, and 48 hours says that two nights have gone by without one |
| `volume.min_rows` | A load must write at least 5 rows. A normal night has 8 |
| `schema.on_change` | A change in the columns is a warning. The other choices are `error` and `ignore` |

The channel lists three events, because a channel that lists none gets the defaults, and the defaults leave out the warnings. A warning is not sent unless the channel asks for `monitor.warn`.

`ldp plan` checks the SLA before anything runs, and it says what it understood.

```bash
ldp plan config/orders.json
```

<!-- docs-runner: output -->
```text
...
sinks       iceberg, jsonl
sla         freshness warn after 26 hour and error after 48 hour on column order_ts, volume min 5 rows, schema warn
...
```

The restaurant needs only a few of the keys. This block shows every key that an SLA has.

```json
"sla": {
  "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"},
                "column": "order_ts"},
  "volume": {"min_rows": 50, "max_rows": 200000, "max_change_pct": 50, "lookback_runs": 7},
  "schema": {"on_change": "warn", "allow": ["added_column"]},
  "metrics": [{"metric": "revenue", "grain": "day", "min": 0, "max_change_pct": 50}]
}
```

| Section | What it says |
|---|---|
| `freshness` | `warn_after` and `error_after` are each a `count` of `minute`, `hour` or `day` periods, where a day is 24 hours. A section has one or both, and `warn_after` is the shorter. `column` names the timestamp or date column whose newest value says how fresh the data is, and without it the table is as old as its last commit |
| `volume` | `min_rows` and `max_rows` bound the rows that the latest run wrote. `max_change_pct` is how far in percent the latest run may be from the median of the runs before it, among the last `lookback_runs` runs, which is 7 by default. A section has at least one of the three rules |
| `schema` | `on_change` is `ignore`, `warn` or `error`, and `warn` is the default. `allow` lists the changes that stay fine, which can be `added_column` and `widened_type` |
| `metrics` | A list of business numbers to watch. Part 5 adds one |

An SLA has at least one of the four sections.

## Part 2. A table that is late

The night of 3 August arrives, and the system sends nothing on the nights of 4 and 5 August. Load the file the way a nightly job would.

```bash
cp exports/monitor_orders_2026-08-03.csv landing/orders.csv
ldp run config/orders.json
```

<!-- docs-runner: output -->
```text
orders: read 8 rows, wrote 8 rows in ...s (quality: 7 of 7 checks passed)
...
```

It is ten o'clock on the night of 5 August, which is about 50 hours after the newest order. `ldp monitor` looks at the table. `--now` gives it that time, and without `--now` the monitor uses the clock. The `|| echo` prints the exit status, so that the page can show it.

```bash
ldp monitor config --now 2026-08-05T22:00:00Z 2>&1 || echo "exit status $?"
```

<!-- docs-runner: output -->
```text
Monitors at 2026-08-05T22:00:00Z
pipeline  table       monitor      status  observed   expected                                                       details
--------  ----------  -----------  ------  ---------  -------------------------------------------------------------  ----------------------------------------------------------------------------------------------
orders    raw.orders  freshness    error   50.4 hour  warn after 26 hour and error after 48 hour on column order_ts  the newest order_ts is 2026-08-03T19:34:26Z, 50.4 hour ago
orders    raw.orders  volume.rows  ok      8 rows     min 5 rows                                                     the latest run wrote 8 rows, inside the limits
orders    raw.orders  schema       ok      baseline   warn                                                           baseline recorded for the 7 columns of the table, so there is nothing to compare them with yet
(3 rows)
2 ok, 1 error
6 configs have no metadata.sla, so there is nothing to check in customers.json, menu_items.json, order_lines.json, restaurants.json, restaurants_to_csv.json, reviews.json
Results recorded in _ldp.monitor_results
Alerts sent to file ../.ldp/alerts.jsonl (1)
exit status 2
```

The freshness monitor says `error`, because the newest order is 50.4 hours old and the limit is 48. The volume monitor is happy with 8 rows. The schema monitor has nothing to compare the columns with yet, so it is `ok` and keeps the 7 columns it saw as the baseline for the next look.

Exit status 2 says that a monitor is in error. It is a finding and not a failure of `ldp`, so a scheduler must not retry it, and [the table of exit statuses](#exit-statuses) says what a scheduler does with each one.

The alert is a line of JSON in the file that the channel names. It says the pipeline, the table and the owner, which is the `who` of the config, what the monitor observed and what it expected, and the command to look closer.

```bash
cat .ldp/alerts.jsonl
```

<!-- docs-runner: output -->
```text
{"at": "2026-08-05T22:00:00Z", "command": "ldp status config/orders.json", "details": "the newest order_ts is 2026-08-03T19:34:26Z, 50.4 hour ago", "evaluated_at": "2026-08-05T22:00:00Z", "event": "monitor.error", "expected": "warn after 26 hour and error after 48 hour on column order_ts", "kind": "freshness", "monitor": "freshness", "observed": "50.4 hour", "owner": "restaurant owner", "pipeline": "orders", "schema": "ldp.alert", "status": "error", "table": "raw.orders", "version": 1}
```

### Reading a look

`ldp monitor` takes a config or a folder. A folder checks every dataset config in it and leaves out the files of other kinds, such as a workflow. A config that has no `metadata.sla` is not looked at, and the last lines of the output say which ones. `--now` measures at another moment, and `--json` prints the results as a list of objects. Each monitor prints one line with its status, what it observed and what it expected. The command writes to no table and creates no catalog, and it records its results, as [the history](#the-history) says.

A monitor says one of four things. `ok` says that the rule holds. `warn` says that it is broken in a way that the SLA calls a warning, and `error` that it is broken in a way that the SLA calls an error, or that the monitor cannot work because the table is not there. `unknown` says that the monitor could not tell, so it neither raises an alert nor clears one.

The exit status of `ldp monitor` is 0 when every monitor is `ok`, `warn` or `unknown`, 2 when at least one monitor is `error`, and 1 when `ldp` itself failed, such as a config that cannot be read.

### What the age of a table is

The age of a table is the time since its last commit, which is the current snapshot on `main`. When the SLA names a `column`, it is the time since the newest value of that column, which the data files of the current snapshot carry as upper bounds. The manifests are read and no data file is. Only a timestamp or a date column can say how fresh a table is. A date counts as the end of its day in UTC, so a table that was loaded this morning for yesterday is hours old and not a day old, and a timestamp without a time zone is in UTC. This chapter names `order_ts`, because the restaurant cares about how old its orders are and not about when a file was loaded.

The table is late when its age is more than `warn_after`, and too late when it is more than `error_after`, so a table that is exactly as old as a threshold is not late yet. A table that was never written has no age, which is an `error`, because nothing has arrived. The details of that result say `table has never been written`. A config whose target is a file has no commits, so its freshness is `unknown`, and so is a column whose data files carry no upper bounds.

## Part 3. A short batch

The night of 6 August arrives, and it is cut short. The file has 3 orders where a normal night has 8. Every quality check of the load passes, because 3 rows break no rule of the data, so the size is the only thing that tells. The look of the next morning finds the table fresh again, because the newest order is 13.2 hours old, so the freshness alert is resolved. The load is under the 5 rows that `min_rows` asks for.

```bash
cp exports/monitor_orders_2026-08-06.csv landing/orders.csv
ldp run config/orders.json
ldp monitor config --now 2026-08-07T06:00:00Z 2>&1 || echo "exit status $?"
```

<!-- docs-runner: output -->
```text
orders: read 3 rows, wrote 3 rows in ...s (quality: 7 of 7 checks passed)
...
Monitors at 2026-08-07T06:00:00Z
pipeline  table       monitor      status  observed   expected                                                       details
--------  ----------  -----------  ------  ---------  -------------------------------------------------------------  -------------------------------------------------------------------------
orders    raw.orders  freshness    ok      13.2 hour  warn after 26 hour and error after 48 hour on column order_ts  the newest order_ts is 2026-08-06T16:50:42Z, 13.2 hour ago
orders    raw.orders  volume.rows  error   3 rows     min 5 rows                                                     the latest run wrote 3 rows, fewer than the 5 rows that min_rows asks for
orders    raw.orders  schema       ok      no change  warn                                                           the 7 columns of the table are as the last evaluation found them
(3 rows)
2 ok, 1 error
6 configs have no metadata.sla, so there is nothing to check in customers.json, menu_items.json, order_lines.json, restaurants.json, restaurants_to_csv.json, reviews.json
Results recorded in _ldp.monitor_results
Alerts sent to file ../.ldp/alerts.jsonl (2)
exit status 2
```

Two alerts went out. The freshness monitor was alerting and is not any more, so the channel got `monitor.resolved` for it. The volume monitor went to `error`, so the channel got `monitor.error` for it. The table is still the same table, and the exit status is still 2, because one monitor is in error.

### The two results of the volume monitor

The volume monitor makes up to two results, so that each rule has its own history and its own status. `volume.rows` holds the latest run against `min_rows` and `max_rows`, and it exists when the SLA has one of them. `volume.change` holds the latest run against the median of the runs before it, among the last `lookback_runs` runs, which includes the latest one, and it exists when the SLA has `max_change_pct`. A run that is further from the median than `max_change_pct` percent is an `error`, whether it fell or rose, and one that is exactly as far is not. The SLA of this chapter has only `min_rows`, so it makes only `volume.rows`.

The change needs 3 earlier runs, so that one odd run is not the baseline. With fewer, `volume.change` is `unknown` with the details `not enough history`, and it never raises an alert. A `lookback_runs` of 3 or less leaves fewer than 3 earlier runs, so the change would never report, and `ldp plan` says so. When the earlier runs wrote no rows, a change in percent has no meaning, and the result is `unknown`, unless the latest run wrote none either.

### What the count of a run is

The count is the `rows_written` of the run, which follows the write mode of the target. The orders config appends, so a run counts the rows that it added.

| Write mode | A run counts |
|---|---|
| `append` | the rows that it added |
| `overwrite` | the rows of the new batch, which are the rows of the table after it unless the config replaces a window |
| `upsert` | the rows that it inserted or changed, which is the batch and not the table |

A batch that an upsert finds the same as the stored rows changes nothing, so it counts 0 rows, and a `min_rows` of 1 would call it an error. Only a run that was `published` counts, and it counts once, whatever its attempts did. A run that a quality check blocked, that failed, or that found its idempotency key already published has no count.

### Where the counts come from

The runs are in the history that the config keeps with its sinks. That is `_ldp.runs` when the config lists an `iceberg` sink, and the file of its `jsonl` sink otherwise. Without a history, the monitor reads the `added-records` of the commits of the table that append rows. An overwrite is a delete and an append, so the append holds the new batch. A commit that rewrites files, such as a delete of some rows, adds no new rows and is left out. An upsert is several commits, so for an upsert the snapshots are no count of a run, and it needs a history. The fallback is not a record either, because expiring snapshots removes the commits. `ldp plan` says so when a config has a `volume` section and neither of the two sinks. The result says which one it used in its metrics, as `source`.

The metrics of a result hold the numbers behind it. They are `observed`, the count of the latest run, and for the change also `baseline_median`, `change_pct` and `earlier_runs`.

## Part 4. A new column

From 7 August the export has a `discount` column, and nobody said so. The night has 8 orders, so the load is normal in size, and the volume alert is resolved. The schema monitor sees the new column. The SLA calls a change in the columns a warning, so the exit status is 0, and a warning reaches the channel because the channel asked for `monitor.warn`.

```bash
cp exports/monitor_orders_2026-08-07.csv landing/orders.csv
ldp run config/orders.json
ldp monitor config --now 2026-08-08T06:00:00Z 2>&1
```

<!-- docs-runner: output -->
```text
orders: read 8 rows, wrote 8 rows in ...s (quality: 7 of 7 checks passed)
...
Monitors at 2026-08-08T06:00:00Z
pipeline  table       monitor      status  observed   expected                                                       details
--------  ----------  -----------  ------  ---------  -------------------------------------------------------------  ---------------------------------------------------------------------------------------------------------------
orders    raw.orders  freshness    ok      10.4 hour  warn after 26 hour and error after 48 hour on column order_ts  the newest order_ts is 2026-08-07T19:34:26Z, 10.4 hour ago
orders    raw.orders  volume.rows  ok      8 rows     min 5 rows                                                     the latest run wrote 8 rows, inside the limits
orders    raw.orders  schema       warn    2 changes  warn                                                           the table gained the column discount (double), the source gained the column discount (double) in its latest run
(3 rows)
2 ok, 1 warn
6 configs have no metadata.sla, so there is nothing to check in customers.json, menu_items.json, order_lines.json, restaurants.json, restaurants_to_csv.json, reviews.json
Results recorded in _ldp.monitor_results
Alerts sent to file ../.ldp/alerts.jsonl (2)
```

Two things changed. `volume.rows` is `ok` again, so its alert is resolved, and `schema` is a `warn` with 2 changes, because the table gained the column and so did the source in the latest run. A change is said once, by the look that first sees it. The export keeps the column from now on, so the next look compares the table with the columns it has now, finds no change, and resolves the warning.

### What the schema monitor compares

The schema monitor says whether the columns of the table, or of its source, changed since the last look. It compares two things.

The columns of the table now are compared with the columns that the previous evaluation of the monitor kept in its result, so the history is the baseline and there is no file to keep. The first evaluation has nothing to compare with, so it is `ok` and records the baseline, which is a name and a type for each column and a fingerprint of the whole schema, in its metrics. A change is reported once, by the evaluation that first sees it, and the next evaluation compares with the new columns.

The columns that the source gave in the latest run are compared with those of the run before it, from the `run.extracted` events of the history. That reports a source that changed in a way that made the run fail, though the table did not change. It needs a history, and a report of the source stays until a later run has the columns of the run before it.

### The kinds of change

| Change | What it is |
|---|---|
| `added_column` | a column that was not there before |
| `removed_column` | a column that is not there any more |
| `widened_type` | a type that holds every value of the old one, such as `int` to `long`, `float` to `double`, or a `decimal` with more digits |
| `changed_type` | any other type, such as a column that was dropped and added again as another type |
| `other` | something that the fingerprint sees and the columns do not, such as the nullability or the order of the columns |

`allow` lists the kinds of change that stay `ok`, and it can hold `added_column` and `widened_type`. A removed column and another type are never allowed. A change that is not allowed has the status that `on_change` says, `warn` by default, or `error`, or `ok` when it is `ignore`. A table that does not exist yet has no schema, so the result is `unknown`.

If the restaurant wanted to take a new column in its stride, the SLA would say `"schema": {"on_change": "warn", "allow": ["added_column"]}`, and only a removed column or another type would raise a warning.

The metrics of a result hold the `fingerprint` and the `columns` of the table, which are the next baseline, and each change with where it happened, `table` or `source`, the column, the types before and after and whether it was allowed.

## Part 5. A day of low revenue

The night of 8 August has all its 8 orders, and every amount is a tenth of what it should be, as if a price list had been loaded with the decimal point in the wrong place. The load is the right size, the columns are the same as the night before, and every check of the load passes. Only the revenue shows it. A business number can be watched too, and this part watches the revenue of yesterday.

Load the night.

```bash
cp exports/monitor_orders_2026-08-08.csv landing/orders.csv
ldp run config/orders.json
```

<!-- docs-runner: output -->
```text
orders: read 8 rows, wrote 8 rows in ...s (quality: 7 of 7 checks passed)
...
```

The revenue is the metric `revenue` of `config/metrics.json`, and the metric is read from the gold table `gold.gold_daily_sales`, which dbt builds from the orders, their lines and the restaurants. These are the steps of [the warehousing guide](warehousing.md), so they go without comment. Copy the dbt project, and load the four small tables and the lines of the nights so far.

```bash
cp -R ../../examples/restaurant/dbt dbt
cp exports/restaurants.xlsx exports/customers.csv exports/menu_items.csv exports/reviews.csv landing/
for name in restaurants customers menu_items reviews; do
  ldp run config/$name.json
done
for day in 2026-08-03 2026-08-06 2026-08-07 2026-08-08; do
  cp exports/monitor_order_lines_$day.csv landing/order_lines.csv
  ldp run config/order_lines.json
done
```

<!-- docs-runner: output -->
```text
restaurants: read 6 rows, wrote 6 rows in ...s (quality: 4 of 4 checks passed)
...
customers: read 150 rows, wrote 150 rows in ...s (quality: 4 of 4 checks passed)
...
menu_items: read 36 rows, wrote 36 rows in ...s (quality: 5 of 5 checks passed)
...
reviews: read 94 rows, wrote 94 rows in ...s (quality: 5 of 5 checks passed)
...
order_lines: read 19 rows, wrote 19 rows in ...s (quality: 4 of 4 checks passed)
...
order_lines: read 8 rows, wrote 8 rows in ...s (quality: 4 of 4 checks passed)
...
order_lines: read 19 rows, wrote 19 rows in ...s (quality: 4 of 4 checks passed)
...
order_lines: read 19 rows, wrote 19 rows in ...s (quality: 4 of 4 checks passed)
...
```

The metrics file describes the gold tables of the whole data mart, and a gold build holds the metrics of its folder to the tables that exist. This chapter builds one gold table, so keep the model that it answers and the three metrics that read it.

```python
import json
from pathlib import Path

path = Path("config/metrics.json")
spec = json.loads(path.read_text())
spec["semantic_models"] = [model for model in spec["semantic_models"] if model["name"] == "daily_sales"]
spec["metrics"] = [metric for metric in spec["metrics"] if metric["name"] in ("revenue", "orders", "avg_ticket")]
path.write_text(json.dumps(spec, indent=2) + "\n")
```

Build the gold table with dbt, with the models that it needs and the customers that the data tests of the orders check against, and cut a release of it. A release is what the metric reads.

```bash
ldp dbt build dbt --select +gold_daily_sales stg_customers 2>&1
ldp gold build config 2>&1
```

<!-- docs-runner: output -->
```text
...
ldp: published silver.fct_orders: upsert, 0 -> 27 rows, snapshot ...
ldp: published gold.gold_daily_sales: overwrite, 0 -> 18 rows, snapshot ...
...
gold_daily_sales  checked        18 rows  snapshot ...  5 of 5 checks passed
...
metrics: 1 semantic model and 3 metrics fit the gold tables
release r1 cut with 1 table and 18 rows in gold
```

Now add the rule. An entry of `metrics` names the metric and says at what `grain` to measure it. This one says that a day of revenue must not be under 1000, and a normal day is 6330.

```python
import json
from pathlib import Path

path = Path("config/orders.json")
spec = json.loads(path.read_text())
spec["metadata"]["sla"]["metrics"] = [{"metric": "revenue", "grain": "day", "min": 1000}]
path.write_text(json.dumps(spec, indent=2) + "\n")
```

The look of the morning of 9 August measures the revenue of 8 August.

```bash
ldp monitor config --now 2026-08-09T06:00:00Z 2>&1 || echo "exit status $?"
```

<!-- docs-runner: output -->
```text
Monitors at 2026-08-09T06:00:00Z
pipeline  table       monitor               status  observed   expected                                                       details
--------  ----------  --------------------  ------  ---------  -------------------------------------------------------------  --------------------------------------------------------------------
orders    raw.orders  freshness             ok      10.4 hour  warn after 26 hour and error after 48 hour on column order_ts  the newest order_ts is 2026-08-08T19:34:26Z, 10.4 hour ago
orders    raw.orders  volume.rows           ok      8 rows     min 5 rows                                                     the latest run wrote 8 rows, inside the limits
orders    raw.orders  schema                ok      no change  warn                                                           the 8 columns of the table are as the last evaluation found them
orders    raw.orders  metric.revenue.value  error   633        min 1000                                                       revenue for day 2026-08-08 was 633, below the 1000 that min asks for
(4 rows)
3 ok, 1 error
8 configs have no metadata.sla, so there is nothing to check in customers.json, fct_orders.json, gold_daily_sales.json, menu_items.json, order_lines.json, restaurants.json, restaurants_to_csv.json, reviews.json
Results recorded in _ldp.monitor_results
Alerts sent to file ../.ldp/alerts.jsonl (2)
exit status 2
```

The freshness and the volume monitors are `ok`. The revenue of 8 August was 633, and the floor is 1000, so the metric monitor went to `error`. The schema warning is resolved, because the table has the same 8 columns as at the look before. That makes two alerts for the channel. The dbt step added two configs to the folder, `fct_orders.json` and `gold_daily_sales.json`, which have no SLA, so the monitor says that it has nothing to check in them.

### What a metric entry says

An entry of `metrics` names a metric of the metrics file that sits next to the config, or gives one query that returns one number, and it says at what `grain` to measure it.

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

An entry has at least one of `min`, `max` and `max_change_pct`. A `name` is lower case letters, digits and underscores that start with a letter.

### What is measured

The monitor measures the latest complete period of the grain, counted in UTC. At 10:00 on a Wednesday that is Tuesday for `day`, and the week that ended on Sunday for `week`. A week starts on Monday, and a quarter starts in January, April, July or October. `--now` moves the period, and the data is read as it is now. A metric is computed by `local_data_platform.metrics` from the current release of the gold tables, as `ldp metrics query` does, so the number is the one that the reports and the agents show. The metric may be over a table other than the one of the config, which is the case here, because the SLA is in the config of a raw table and the metric is over a gold table.

Nothing tells the monitor when a period has been loaded. A day that is still being loaded looks like a fall, so schedule `ldp monitor` after the load and after the gold build that cuts the release. Until that release holds the day, the day has no rows, and the result is `unknown`. The freshness monitor is the one that says that a table is late.

### Two results

An entry makes up to two results, so that each rule has its own history and its own status. `metric.<key>.value` holds the value against `min` and `max`, and `metric.<key>.change` holds it against the median of the periods before it. The key is the `name` of the entry, or its metric. A value that breaks a limit, or that is further from the median than `max_change_pct` percent, is an `error`, whether it fell or rose, and a value exactly at a limit is not. A change in percent is measured against the size of the median, so a loss that gets smaller is a rise.

### The baseline

The change needs 3 earlier values, so that one odd day is not the baseline. The earlier values come from the history of the monitors, so there is no query for the days before, and the first change report comes 3 periods after the first look. With fewer values, `metric.<key>.change` is `unknown` with the details `not enough history`, and it never raises an alert. The history has one look for each time that `ldp monitor` ran, and it may run many times in a period, so the baseline takes one value for each earlier period, the last that was measured. Only the looks of the same definition count, which is the metric or the query, the grain and the filter, so a history of another metric or grain is never mixed in. Changing a limit, the `lookback` or the name does not start a new baseline, and changing the metric, the query, the grain or the filter does.

`lookback` counts the latest period too, so a `lookback` of 3 or less leaves fewer than 3 earlier values, and the change would never report. `ldp plan` says so. When the median of the earlier values is 0, a change in percent has no meaning, and the result is `unknown`, unless the value is 0 too.

### When there is no value

| What happens | Status |
|---|---|
| The period has no rows, such as a day that the release does not hold yet, or a day that the restaurant was shut | `unknown` |
| The value is NULL, such as the average of a column that has no value in the period | `unknown` |
| The gold table does not exist yet | `unknown` |
| The duckdb package is not installed, and the details say `pip install "local-data-platform[duckdb]"` | `unknown` |
| A metric that the metrics file does not define, a grain or a filter that its model does not allow, or a metrics file that is not valid | `error` |
| A query that is not one read-only statement, or that cannot run, or that does not return one number | `error` |

An `unknown` result never raises an alert. An `error` for a definition that cannot run is on purpose, because a monitor that quietly never alerts is worse than one that says it is broken. The other monitors of the config still run. `ldp plan` finds the unknown metric, the grain and the filter before a run, and its message lists the valid names.

### A query instead of a metric

A folder that has no metrics file has no definitions to share, so an entry can give the number as a query. The query runs on the table of the config, which it calls by its name.

```json
{"name": "orders_seen", "sql": "SELECT count(*) FROM orders WHERE order_date = current_date - 1", "grain": "day", "min": 1}
```

The query goes through the statement guard of the MCP server, which lets one `SELECT` or `WITH` through and refuses `DELETE` and anything else, with the message of the guard in the details of an `error`. It runs on a DuckDB engine that has only that table, at its current snapshot, and it has to return one row of one number. A folder that has a metrics file refuses a query, because the number belongs in the file, where every reader gets it the same way. The grain of a query says how long a period is for the history, since the text of the query says which rows to count. The text runs as it is, so `current_date` is the date of the machine and not the moment of `--now`.

### What is stored

Only the value is stored in the history, with its period, grain and the names behind it. A row of a table is never stored. The metrics of a result are `value`, `period`, `grain`, `definition`, the `table` and, for a metric, what it was `read` from, and the limits and `lookback` of the rule. The change adds `earlier_values`, `baseline_median` and `change_pct`. An error of DuckDB that speaks about a value of a row, such as a value that cannot be cast, is said by its kind only, because its text may quote the row.

## Part 6. Hear about each problem once, and hear when it is over

The night of 9 August is normal again. Load it, rebuild the gold table so that the release holds the day, and look in the morning of 10 August.

```bash
cp exports/monitor_orders_2026-08-09.csv landing/orders.csv
cp exports/monitor_order_lines_2026-08-09.csv landing/order_lines.csv
ldp run config/orders.json
ldp run config/order_lines.json
ldp dbt build dbt --select +gold_daily_sales stg_customers 2>&1
ldp gold build config 2>&1
ldp monitor config --now 2026-08-10T06:00:00Z 2>&1
```

<!-- docs-runner: output -->
```text
orders: read 8 rows, wrote 8 rows in ...s (quality: 7 of 7 checks passed)
...
order_lines: read 19 rows, wrote 19 rows in ...s (quality: 4 of 4 checks passed)
...
ldp: published silver.fct_orders: upsert, 27 -> 35 rows, snapshot ...
ldp: published gold.gold_daily_sales: overwrite, 18 -> 23 rows, snapshot ...
...
release r2 cut with 1 table and 23 rows in gold
...
Monitors at 2026-08-10T06:00:00Z
pipeline  table       monitor               status  observed   expected                                                       details
--------  ----------  --------------------  ------  ---------  -------------------------------------------------------------  ----------------------------------------------------------------
orders    raw.orders  freshness             ok      10.4 hour  warn after 26 hour and error after 48 hour on column order_ts  the newest order_ts is 2026-08-09T19:34:26Z, 10.4 hour ago
orders    raw.orders  volume.rows           ok      8 rows     min 5 rows                                                     the latest run wrote 8 rows, inside the limits
orders    raw.orders  schema                ok      no change  warn                                                           the 8 columns of the table are as the last evaluation found them
orders    raw.orders  metric.revenue.value  ok      6330       min 1000                                                       revenue for day 2026-08-09 was 6330, inside the limits
(4 rows)
4 ok
8 configs have no metadata.sla, so there is nothing to check in customers.json, fct_orders.json, gold_daily_sales.json, menu_items.json, order_lines.json, restaurants.json, restaurants_to_csv.json, reviews.json
Results recorded in _ldp.monitor_results
Alerts sent to file ../.ldp/alerts.jsonl (1)
```

Everything is `ok`, so the exit status is 0. The revenue is back at 6330, so its alert is resolved. The monitor looks again five minutes later. Nothing changed state, so it sends nothing, and the output has no line about alerts.

```bash
ldp monitor config --now 2026-08-10T06:05:00Z 2>&1
```

<!-- docs-runner: output -->
```text
Monitors at 2026-08-10T06:05:00Z
...
4 ok
...
Results recorded in _ldp.monitor_results
```

That is the point of an alert that says a change of state. The file has the whole story of the five nights, one line for each change, and not one line for each look.

```python
import json
from pathlib import Path

for line in Path(".ldp/alerts.jsonl").read_text().splitlines():
    alert = json.loads(line)
    print(alert["at"], alert["event"], alert["monitor"], alert["status"], alert.get("previous_status", "-"))
```

<!-- docs-runner: output -->
```text
2026-08-05T22:00:00Z monitor.error freshness error -
2026-08-07T06:00:00Z monitor.resolved freshness ok error
2026-08-07T06:00:00Z monitor.error volume.rows error ok
2026-08-08T06:00:00Z monitor.resolved volume.rows ok error
2026-08-08T06:00:00Z monitor.warn schema warn ok
2026-08-09T06:00:00Z monitor.resolved schema ok warn
2026-08-09T06:00:00Z monitor.error metric.revenue.value error -
2026-08-10T06:00:00Z monitor.resolved metric.revenue.value ok error
```

### What a channel is told

Each channel sees a monitor through its own `on` list. It sees the monitor as alerting while it is in a status that the channel wants, which is `error` for a channel that has `monitor.error`, and `warn` as well for one that has `monitor.warn`.

| What happens to a monitor | What the channel sends |
|---|---|
| It becomes alerting, or goes from one alerting status to another | The alert for its new status. A monitor that was never looked at counts as `ok`, so a table that is broken at the first look alerts |
| It stops being alerting | `monitor.resolved`, when the channel wants it |
| It stays in the same alerting status | Nothing, unless the channel has `repeat_after_minutes` |
| It says `unknown` | Nothing, and the alert that is open stays open |

The previous state of a monitor comes from the history of the monitors, so there is no file of alert state. A look at a moment before results that are in the history does not see them, so `--now` can go back in time, and a look at an old moment can say again what an earlier look said. [The page on alerts](alerts.md) has the channels, the events, the repeat rule and the message.

A Slack channel is one more entry in the list. The URL of the webhook is never in the config, because a config is meant to be committed. `url_env` names the environment variable that holds it.

```json
{"type": "slack", "url_env": "LDP_SLACK_WEBHOOK", "on": ["monitor.error", "monitor.warn", "monitor.resolved"]}
```

Test a channel before you rely on it. `ldp alert test` sends one test message to each channel of the config, whatever its `on` says, and the file channel appends a line with the event `alert.test`.

```bash
ldp alert test config/orders.json
```

<!-- docs-runner: output -->
```text
file ../.ldp/alerts.jsonl: sent
1 of 1 channels got the test message
```

A channel that cannot be reached, that answers with an error status, whose environment variable is not set or whose file cannot be written is a warning in the log and nothing more. A run is not failed by it, a monitor keeps its exit status, and the results are recorded.

### The history

Every invocation of `ldp monitor` records its results. A config with an `iceberg` sink in `metadata.observability` gets them in `_ldp.monitor_results` of its catalog, in one commit for each catalog and invocation. Any other config, and one whose catalog does not exist yet, gets them in `.ldp/monitor.jsonl` next to the config, one JSON object for each result. Monitoring never creates a catalog. The history never holds rows of a table, only names, counts and times, and it is separate from the run events, so `ldp runs` shows the same runs before and after `ldp monitor`.

The Python API does what the command does. `evaluate` returns a result for each monitor, `record` stores them, and `read_results` reads them back.

```python
from local_data_platform import Config
from local_data_platform.monitoring import evaluate
from local_data_platform.monitoring.history import read_results, record

config = Config.from_json("config/orders.json")
results = evaluate(config, now="2026-08-10T06:10:00Z")
record([(config, results)])
history = read_results(config)
print(len(history), "results in the history")
print([(result.monitor, result.status) for result in history[-4:]])
```

<!-- docs-runner: output -->
```text
... results in the history
[('freshness', 'ok'), ('metric.revenue.value', 'ok'), ('schema', 'ok'), ('volume.rows', 'ok')]
```

The monitors are `freshness`, `volume.rows`, `volume.change`, `schema`, and `metric.<key>.value` and `metric.<key>.change` for each entry of `metrics`. The columns of `_ldp.monitor_results` are in [the table of the `_ldp` tables](observability.md#the-_ldp-namespace).

## Part 7. Every pipeline on one screen

Ten pipelines on a laptop need one command to look at in the morning. `ldp status` shows every pipeline of a folder on one screen. It is a view and not a monitor. It evaluates the monitors in memory with the code that `ldp monitor` runs, and it records no result and sends no alert, so it can run as often as a person wants, and it does not use up the change of state that `ldp monitor` alerts for. It creates nothing, and each `_ldp` table is read once for all the configs that share a catalog.

```bash
ldp status config --now 2026-08-10T06:20:00Z
```

<!-- docs-runner: output -->
```text
Status at 2026-08-10T06:20:00Z
pipeline            table                       owner             last_run  last_success_age     rows  freshness  volume  schema  metrics  quality_7d  open_alerts
------------------  --------------------------  ----------------  --------  -------------------  ----  ---------  ------  ------  -------  ----------  -----------
orders              raw.orders                  restaurant owner  -         0.0 minute (commit)    35  ok         ok      ok      ok                -            0
customers           raw.customers               restaurant owner  -         0.0 minute (commit)   150  no sla     no sla  no sla  no sla            -            -
fct_orders          silver.fct_orders           -                 -         0.0 minute (commit)    35  no sla     no sla  no sla  no sla            -            -
gold_daily_sales    gold.gold_daily_sales       -                 -         0.0 minute (commit)    23  no sla     no sla  no sla  no sla            -            -
menu_items          raw.menu_items              restaurant owner  -         0.0 minute (commit)    36  no sla     no sla  no sla  no sla            -            -
order_lines         raw.order_lines             restaurant owner  -         0.0 minute (commit)    84  no sla     no sla  no sla  no sla            -            -
restaurants         raw.restaurants             restaurant owner  -         0.0 minute (commit)     6  no sla     no sla  no sla  no sla            -            -
restaurants_to_csv  ../landing/restaurants.csv  restaurant owner  -         -                       -  no sla     no sla  no sla  no sla            -            -
reviews             raw.reviews                 restaurant owner  -         0.0 minute (commit)    94  no sla     no sla  no sla  no sla            -            -
(9 rows)
1 ok, 8 no sla
History goes back to 2026-08-05T22:00:00Z, the oldest event of any pipeline here. 8 pipelines have none.
```

The screen says what the chapter did. The orders are healthy, and the eight other configs have no SLA, so they say `no sla`. `last_run` and `quality_7d` are empty, and the last success says `0.0 minute`, because the loads of this chapter happened when you ran them, and `--now` says that it is 10 August 2026, so they are still to come. In real use you leave `--now` out, and the screen shows the real ages and the real last runs.

### What the columns say

| Column | What it says |
|---|---|
| `pipeline`, `table`, `owner` | The identifier of the config, its target table, and its `who` |
| `last_run` | The status of the latest run and how long ago it started, from the run history |
| `last_success_age` | How long ago the last run that went well started. `published`, `skipped_duplicate` and `succeeded` went well. A config with no run history shows the age of the last commit of the table instead, marked `(commit)` |
| `rows` | The rows of the table, from the summary of its current snapshot |
| `freshness`, `volume`, `schema`, `metrics` | What the monitors of that section say now, the worst of them when a section has several. `-` is a section that the SLA does not have, and `no sla` is a config that has no `metadata.sla` |
| `quality_7d` | How many checks failed in the last 7 days, counted in `_ldp.quality_results`, or in the events of a config that keeps them in a file. `-` says that no check ever ran |
| `open_alerts` | How many monitors have an alert open on a channel of the config, which is how many it would still be telling someone about. A monitor that says `unknown` keeps open what it held before. `-` says that the config has no channel |

The rows come worst first, and then by name. A monitor in `error`, a run that `failed` or went `upstream_failed`, and a file that cannot be read as a config make a row an `error`. A monitor in `warn`, and a run that a quality check held back with `blocked_quality`, make it a `warn`. A monitor that says `unknown` comes next, then a pipeline whose monitors are all `ok`, and last a config that has no SLA and no run that went wrong. Ages count from the start of the run.

The last line says how far back the history goes, which is the oldest run event or monitor result of any pipeline on the screen, so that a history that was trimmed shows, and so does a baseline that is shorter than it should be. `--now` looks at another moment. The last run, the last success, the failed checks and the oldest event leave out what came after it, and the monitors read the history as `ldp monitor --now` does.

The exit status of `ldp status` is 0 when the screen was printed, whatever the monitors say, and 1 when `ldp` itself failed, such as a config that cannot be read. The row of that config is on the screen, and the reason is on stderr.

### A page to open, and JSON to read

`--html FILE` also writes one page that has the table, the monitors behind each row and the last 20 runs of each pipeline. It is built on the same page shell as the gold catalog, so it is one file with no script, it asks for nothing from the network, and its links are anchors inside it. `--json` prints the rows as a list of objects, with ages in seconds and times in UTC, and the monitors and the last 20 runs of each pipeline in them.

```bash
ldp status config --now 2026-08-10T06:20:00Z --html status.html
```

<!-- docs-runner: output -->
```text
...
wrote status.html, 9 pipelines, ... KB. Open it as a page. It needs no network.
```

A program can read the same rows. `rows` returns a `StatusRow` for each config, the worst first, and it is what a panel or a gate in another tool calls. The data health panel of every report that `ldp report build` writes calls it, and so does the freshness gate of that command. A `Run` has the names of its failing checks and, once it ended, the moment that it finished. A config that cannot be read or evaluated is a row too, with the reason in `problem`.

```python
from local_data_platform.monitoring.status import rows

for row in rows("config", now="2026-08-10T06:20:00Z"):
    print(row.pipeline, row.health, row.freshness, row.open_alerts, row.last_run and row.last_run.status)
```

<!-- docs-runner: output -->
```text
orders ok ok 0 None
customers no sla no sla None None
fct_orders no sla no sla None None
gold_daily_sales no sla no sla None None
menu_items no sla no sla None None
order_lines no sla no sla None None
restaurants no sla no sla None None
restaurants_to_csv no sla no sla None None
reviews no sla no sla None None
```

An agent that asks [the MCP server](agents.md#the-sla-verdict-in-describe_table) about one table gets the SLA verdict of that table in `describe_table`, from the latest results that `ldp monitor` stored, or from freshness worked out at that moment.

## Part 8. Look every 15 minutes

Everything so far was a look that you asked for. `ldp monitor` looks once and exits, so something has to call it, and there is no server to do that. cron or launchd calls it every 15 minutes, in the same way that [the orchestration chapter](orchestration.md#part-7-a-scheduler) lets them call `ldp schedule tick` every 5 minutes. A look reads metadata and runs at most one small query for each metric, so a look every 15 minutes is light work.

On a Mac, use launchd. Apple has deprecated cron, and cron jobs do not run while the Mac sleeps. launchd runs an interval that went by during the sleep once, when the Mac wakes. This is the recipe of `ldp schedule install --print launchd` with `monitor` in place of `schedule tick`, and 900 seconds in place of 300. Use the absolute path of your own `ldp`, which `command -v ldp` prints, and the absolute path of your own `config` folder, because launchd starts a command with almost no environment. The `EnvironmentVariables` entry holds `HEALTHCHECK_URL`, which the heartbeat below uses. Save the file as `~/Library/LaunchAgents/local-data-platform.monitor.restaurant.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>local-data-platform.monitor.restaurant</string>
	<key>ProgramArguments</key>
	<array>
		<string>/Users/you/local-data-platform/.venv/bin/ldp</string>
		<string>monitor</string>
		<string>/Users/you/local-data-platform/ldp_demo/monitoring/config</string>
	</array>
	<key>StartInterval</key>
	<integer>900</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>WorkingDirectory</key>
	<string>/Users/you/local-data-platform/ldp_demo/monitoring/config</string>
	<key>StandardOutPath</key>
	<string>/Users/you/Library/Logs/local-data-platform.monitor.restaurant.log</string>
	<key>StandardErrorPath</key>
	<string>/Users/you/Library/Logs/local-data-platform.monitor.restaurant.log</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>HEALTHCHECK_URL</key>
		<string>https://hc-ping.com/your-check-uuid</string>
	</dict>
</dict>
</plist>
```

Load it with `launchctl bootstrap`, and stop it with `launchctl bootout`.

<!-- docs-runner: skip it changes what your Mac runs in the background -->
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local-data-platform.monitor.restaurant.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local-data-platform.monitor.restaurant.plist
```

On Linux, use cron. This is the line of `ldp schedule install --print cron` with `monitor` in place of `schedule tick`, and `*/15` in place of `*/5`. It makes the `.ldp` folder first, because the shell opens the log before `ldp` can make it. The line above it sets `HEALTHCHECK_URL`, which the heartbeat below uses, because cron starts a command with almost no environment too.

```text
HEALTHCHECK_URL=https://hc-ping.com/your-check-uuid
# ldp monitor for /home/you/local-data-platform/ldp_demo/monitoring/config, every 15 minutes
*/15 * * * * mkdir -p /home/you/local-data-platform/ldp_demo/monitoring/config/.ldp && /home/you/local-data-platform/.venv/bin/ldp monitor /home/you/local-data-platform/ldp_demo/monitoring/config >> /home/you/local-data-platform/ldp_demo/monitoring/config/.ldp/monitor.log 2>&1
```

Add it to your crontab with `crontab -e`, after you put your own paths in it.

<!-- docs-runner: skip it changes your crontab -->
```bash
crontab -e
```

cron and launchd start a command with almost no environment, so a variable that you exported in your shell is not set for the job. A Slack channel whose `url_env` is not set in the job is a warning in the log and nothing more, and the alert is not sent. Put every variable that a channel names in the crontab or in the `EnvironmentVariables` of the plist, as the two recipes above do, and keep that file readable by you only, because a webhook URL is a secret.

### A heartbeat that an outside service watches

A monitor that looks every 15 minutes cannot tell you that it stopped. A laptop that sleeps, a scheduler that died and a log that filled the disk all look the same from inside, which is silence. The event `monitor.heartbeat` is a ping that a channel gets after every look at a config that completed, whatever the monitors found. A service outside, that expects a ping every 15 minutes, can raise the alarm when the pings stop.

The file channel shows what a heartbeat is. Add a second channel that wants only the heartbeat, and look once.

```python
import json
from pathlib import Path

path = Path("config/orders.json")
spec = json.loads(path.read_text())
spec["metadata"]["observability"]["alerts"].append(
    {"type": "file", "path": "../.ldp/heartbeat.jsonl", "on": ["monitor.heartbeat"]})
path.write_text(json.dumps(spec, indent=2) + "\n")
```

```bash
ldp monitor config --now 2026-08-10T06:30:00Z 2>&1
cat .ldp/heartbeat.jsonl
```

<!-- docs-runner: output -->
```text
Monitors at 2026-08-10T06:30:00Z
...
4 ok
...
{"at": "2026-08-10T06:30:00Z", "command": "ldp status config/orders.json", "event": "monitor.heartbeat", "monitors": 4, "owner": "restaurant owner", "pipeline": "orders", "results": {"ok": 4}, "schema": "ldp.alert", "table": "raw.orders", "version": 1}
```

The heartbeat says how many monitors are in each status and how many there are. A config whose look failed sends none, and a config that has no `metadata.sla` is not looked at.

[Healthchecks.io](https://healthchecks.io) is a service that does the waiting, and one laptop needs one check. Create a check with a simple schedule that has a period of 15 minutes and a grace time of 10 minutes, and copy its ping URL, which looks like `https://hc-ping.com/` and a UUID. Its ping endpoint takes a GET, a HEAD or a POST, and a `webhook` channel posts the heartbeat as JSON. The URL goes in the environment variable that `url_env` names, as in the two recipes above, and never in the config.

```json
{"type": "webhook", "url_env": "HEALTHCHECK_URL", "on": ["monitor.heartbeat"]}
```

When the pings stop for longer than the period and the grace time, Healthchecks.io sends an alert through the integrations that you set up for the check. The body of each ping is the heartbeat, which has the pipeline, the table and the owner of the config and a count of monitors for each status, and it never has a row of a table. Those names leave your laptop, so give the service names that you are happy to share.

### When the laptop sleeps

Nothing alerts while the laptop is asleep or off. A look needs a running machine, so a night that the laptop spends asleep is a night with no look and no alert, and a table that went late in it is found by the first look after the machine wakes. With launchd that look comes when the Mac wakes, because launchd runs an interval that went by during the sleep once, and after a restart it comes when the agent loads, because of `RunAtLoad`. With cron it comes at the next quarter of an hour after the wake. The look says what is wrong now, and the history has a gap that it does not fill.

The heartbeat does not change that. It makes the silence visible, from outside, after the grace time. A run that has to alert at 3 in the morning needs a machine that is on at 3 in the morning, which is a server and not a laptop.

### Keeping the history small

Every invocation of `ldp monitor` is one commit to `_ldp.monitor_results` for each catalog that it records into. A commit adds one small data file, one manifest and one manifest list, so a monitor that runs every 15 minutes adds about 290 files a day to that one table, which is about 26,000 files in 90 days. The table drops its old metadata files by itself, and nothing else leaves it. A run of a pipeline adds commits to `_ldp.runs` and, when it has checks, to `_ldp.quality_results`, and the MCP server adds them to `_ldp.audit`, so those tables grow too, more slowly.

`ldp maintain` with `--system` trims all four tables of the catalog of a config. Run it once a week. The first command is a dry run. For each table it says how many rows would go, how many files hold them and the first and the last day that they cover, and it changes nothing.

```bash
ldp maintain config/orders.json --system
```

<!-- docs-runner: output -->
```text
Maintenance of the _ldp tables of catalog raw (dry run; pass --apply to make the changes)
Rows before ..., the start of the day 90 days back:
  _ldp.runs ...
  _ldp.quality_results ...
  _ldp.monitor_results ...
  _ldp.audit            not there
Snapshots older than 7 days, keeping the last 20:
...
Orphan files older than 72 hours:
...
```

Your numbers for `_ldp.monitor_results` depend on the day that you run it. The results of this chapter are stamped with August 2026, so once 90 days have gone by they are older than the cutoff and the dry run says that it would delete them, and before that it says that it has nothing to delete. `--apply` makes the changes.

<!-- docs-runner: skip it deletes rows of the history, and which rows depends on the day that you run it -->
```bash
ldp maintain config/orders.json --system --apply
ldp maintain config/orders.json --system --keep-days 30 --apply
```

A weekly job is one more line of the crontab, and with launchd it is the same plist with a `StartCalendarInterval` in place of the `StartInterval`.

```text
17 3 * * 0 /home/you/local-data-platform/.venv/bin/ldp maintain /home/you/local-data-platform/ldp_demo/monitoring/config/orders.json --system --apply >> /home/you/local-data-platform/ldp_demo/monitoring/config/.ldp/maintain.log 2>&1
```

`--json` prints the reports as one object. The catalog is the one of the `iceberg` sink of the config, or the one of its Iceberg target or source when it has no such sink, and the tables are in the namespace of the sink, which is `_ldp` unless the sink names another. A table that is not there is skipped, and the command creates no table.

It does three things to each table, in this order.

1. It deletes the rows older than `--keep-days` days, which is 90 unless you give another number. The days are whole UTC days. A row goes when it is before the start of the day that holds the moment N days back, so at least N days stay and at most one day more. Every `_ldp` table is partitioned by `day(ts)`, so the delete drops whole data files from the table and rewrites none.
2. It expires the snapshots committed more than `--expire` days ago, which is 7 unless you give another number, and it keeps the last `--retain-last` snapshots of the table, which is 20 unless you give another number. The snapshots of a table that gets a commit every 15 minutes are what make its metadata large.
3. It finds the orphan files, which are the files under the table that no snapshot names and that are older than `--orphan-age-hours`, 72 unless you give another number, and with `--apply` it deletes them. This step is what brings files off the disk, because a delete and an expiry change the table and nothing else.

Given `--expire` alone it does the first two, and given `--orphans` alone it does the first and the third, as `ldp maintain` does for a table. It can run while `ldp monitor` does. A delete or an expiry that meets a commit made at the same moment plans again, up to 3 times, and a file that is younger than `--orphan-age-hours` is never an orphan.

The files of the days that went do not leave the disk at the delete. Dropping a file from a table does not remove it, and three things keep it. The snapshots from before the delete name it, and the snapshot of the delete names it as a deleted entry, so it stays until they expire. Then the last 50 metadata versions of the table still name those snapshots, so it stays for 50 more commits, which is `--keep-metadata-versions`. Only after that is it an orphan. With a weekly run and `ldp monitor` every 15 minutes, 50 commits take half a day, so a file leaves at the first run after its snapshots have expired, which is two or three weekly runs after the delete. The table is smaller from the delete on, and the disk follows. A dry run cannot show the files that a later run will free, and it can list one snapshot fewer than a real run expires, because the delete is a snapshot of its own.

The exit status of `ldp maintain --system` is 0 when the tables were trimmed or the dry run was printed, and 1 when `ldp` itself failed, such as a config that has no Iceberg catalog, or a file could not be deleted.

Rows older than the cutoff are gone from `ldp runs`, from `ldp status` and from the baselines of the monitors. The volume change needs 3 earlier runs of a pipeline in `_ldp.runs`, as [the volume monitor](#the-two-results-of-the-volume-monitor) says, so a `--keep-days` that holds fewer than 4 runs of the slowest pipeline leaves its `volume.change` at `unknown`. The last line of [the status screen](#part-7-every-pipeline-on-one-screen) says how far back the history goes, so a trim shows there.

```python
from local_data_platform.maintenance.cli import run_system_maintenance

report = run_system_maintenance("config/orders.json", keep_days=90)
for name, table in report["tables"].items():
    if table["exists"]:
        print(name, table["delete"]["rows_deleted"], "rows are before", report["cutoff"])
```

<!-- docs-runner: output -->
```text
_ldp.runs ... rows are before ...
_ldp.quality_results ... rows are before ...
_ldp.monitor_results ... rows are before ...
```

`run_system_maintenance` is a dry run until it gets `apply=True`, and it returns what `--json` prints.

## Exit statuses

Every `ldp` command follows one table, and the commands of this chapter use it for what they look at.

| Exit | Meaning | What a scheduler does |
|---|---|---|
| 0 | Success, including a look at a table that is `ok`, `warn` or `unknown` | Nothing |
| 1 | An error, such as a config that cannot be read, a catalog that is not there or a failed write | Retries it, because the cause may pass |
| 2 | An SLA breach, which `ldp monitor` reports when a monitor is in error | Never retries it, because looking again finds the same table, and tells a person |
| 3 | A data gate held a run back, such as a quality check that blocked a load, which `ldp run` reports, or a report that `ldp report build` held back because its data is stale | Never retries it, because the data has to change first, and tells a person |

`ldp monitor` uses 0, 1 and 2. `ldp status` and `ldp maintain --system` use 0 and 1. `ldp run` and the commands that build gold tables use 3 when a data gate holds a load back.

## What the monitors cannot do

A monitor on a laptop is a small tool, and these are its limits.

- It looks at one moment. A table that was late and was fixed between two looks never alerts. Looks 15 minutes apart make that window small and not zero.
- It needs a machine that is running. Nothing alerts while the laptop is asleep or off, and a heartbeat only tells an outside service that the pings stopped.
- It says nothing about a row. The monitors look at ages, counts, columns and one number for each rule, and a bad value in a row is a job for a quality check of the load.
- The freshness of a table that an engine wrote without column bounds is `unknown`, and an unknown never alerts.
- A change in the volume or in a metric needs 3 earlier values, so the first report of a change comes 3 periods after the first look.
- A change is measured against the median of the periods before it, which has no sense of the weekday. A business that is busier at weekends needs a wide `max_change_pct`, or a `min` and a `max`.
- Nothing tells the monitor that a period has been loaded, so a day that is still loading looks like a fall. Schedule the look after the load and after the gold build.
- The history is in the catalog of the `iceberg` sink, or in a file next to the config. With a local catalog, two machines that look at the same table each keep a history of their own, and each one alerts.
- Delivery is best effort. A channel that is down is a warning in the log, and the alert is not sent again at the next look. A channel with `repeat_after_minutes` gets it back when the next repeat is due, and a channel without it does not.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `ldp monitor` exits with 2 | At least one monitor is in `error`. The table names it, and `ldp status` shows it with the other pipelines | Read the details column and fix the cause. The first look after the fix sends `monitor.resolved` |
| A monitor says `unknown` | It could not tell. The table may have no upper bounds for the column, the gold release may not hold the day yet, or the history may have fewer than 3 earlier values | Nothing alerts for it. Read its details, and see [when there is no value](#when-there-is-no-value) for the cases |
| `volume.change` or a metric change says `not enough history` | A change needs 3 earlier values | Wait for 3 more runs or periods, or leave `max_change_pct` out |
| The details of a metric say `pip install "local-data-platform[duckdb]"` | The metric monitor runs a DuckDB engine, and the package is not installed | `pip install -e ".[dbt,excel]"` from the clone, or `pip install "local-data-platform[duckdb]"` |
| An alert did not arrive | The channel does not list the event, such as a warning for a channel without `monitor.warn`, or the variable that `url_env` names is not set, or the channel was down | `ldp alert test CONFIG` sends a message to each channel and says which got it. The log of the job names the variable |
| The job alerts to a file but not to Slack | cron and launchd start a command with almost no environment, so the variable is not set for the job | Set it in the crontab or in `EnvironmentVariables` of the plist |
| An alert came again for a look at an old moment | A look at a moment before results that are in the history does not see them | Look at a moment after the last look, or leave `--now` out |
| `ldp status` shows no last run and no failed checks with `--now` in the past | `--now` leaves out what came after it, and the loads of this chapter happened today | Leave `--now` out, and the screen shows the real runs |
| The recipe runs and nothing is recorded | The job runs, and its log says why. A wrong path to `ldp` or to the folder is the usual cause | Read `.ldp/monitor.log` or the log in `~/Library/Logs`, and put absolute paths in the recipe |
| You want to start again | | Delete `ldp_demo/monitoring`, and begin again at Make the data |

## What to read next

- [Alerts to Slack, a webhook or a file](alerts.md) has the channels, the events, the repeat rule and the schema of the message.
- [Run events and the `_ldp` namespace](observability.md) explains the tables that the history of the monitors is kept in, and the run events that `ldp status` reads.
- [MCP server for agents](agents.md) gives an agent the SLA verdict of a table, which is what a person reads on the status screen.
- [Orchestrating with workflows, backfills and a scheduler](orchestration.md) is the chapter before this one. It lets cron or launchd run `ldp schedule tick`, and the recipes of Part 8 follow its recipes.
- [A gold layer with contracts, releases, metrics and files](gold.md) explains the release that the revenue monitor reads, and how people read the same number.
- [`examples/restaurant`](https://github.com/tusharchou/local-data-platform/tree/main/examples/restaurant) has the generator and the `run.py` walkthrough that this chapter did by hand. `make demo-monitor` runs the same five nights in about 15 seconds with dbt, and in about a second without it, where it leaves out the revenue step.
