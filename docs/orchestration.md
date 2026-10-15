# Orchestrate the lakehouse with windows, backfills, retries and a scheduler on your laptop

**You will learn** how to describe a nightly load once in a workflow file, run it for one day, backfill a range of days, stop at a bad row and carry on after you fix it, replace a day whose source changed, retry a task that failed, and let launchd or cron run it for you. You will also see where Airflow, Dagster and Prefect fit.

**It takes** about 45 minutes. The commands run in about a minute, and the rest is reading.

**You need** Python 3.12 or newer, git, and a clone of this repo, because the data comes from the restaurant example in `examples/restaurant`. The chapter uses the `dbt` and `excel` extras, as [the warehousing guide](warehousing.md) does, and the dbt project of that guide. Nothing is downloaded after the install, and there is no server and no account. The test suite runs every command on this page, except the install, the two that change your computer's schedule, and the ones that need Dagster or Prefect, in `tests/test_docs_orchestration.py`, so the output you see here is what the code prints.

## The story

The restaurant chain's system exports its orders every night, as two CSV files, one for the orders of the day and one for their lines. Someone has to load the two files into the lakehouse every night, check them, build the models that read them and tidy up. Three things go wrong sooner or later. A file has a row that breaks a rule. A night is loaded twice because someone ran the job again to be safe. The laptop was off, and a night was not loaded at all.

The chapter has eight parts, and they are what a small data team does about it.

1. Give each night a window and a key, so that a night is loaded once whatever happens.
2. Declare the night in a workflow file.
3. Run it for one day.
4. Backfill a few days, stop at a bad row, fix it and carry on.
5. Replace a day that the source corrected.
6. Retry a task, and let a late file wait for the next run.
7. Let a scheduler run it, with launchd on a Mac or cron on Linux.
8. See how Airflow, Dagster and Prefect call the same thing.

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

Everything this guide creates goes into `ldp_demo/orchestration`, and git ignores `ldp_demo`, so you can delete the folder to start again. The generator uses a fixed seed, so you get the same files as this page shows.

```bash
mkdir -p ldp_demo/orchestration
cd ldp_demo/orchestration
python ../../examples/restaurant/generate.py --out exports
```

<!-- docs-runner: output -->
```text
Wrote 22 files to exports: 6 restaurants, 150 customers, 883 orders in 9 batches, 2195 order lines and 94 reviews
```

This guide uses the restaurants and customers, which change rarely, and the seven nightly drops from 1 to 7 August 2026. A drop is an orders file and an order lines file. Each line carries the time of its order, as a restaurant system would export it, so that a window can pick the lines of a day.

| File | Rows | What it is |
|---|---|---|
| `restaurants.xlsx` and `customers.csv` | 6 and 150 | The two tables the models join to |
| `orders_2026-08-01.csv` to `orders_2026-08-07.csv` | 3, 4, 7, 7, 1, 3 and 5 | The orders of each night |
| `order_lines_2026-08-01.csv` to `order_lines_2026-08-07.csv` | 8, 10, 20, 16, 4, 11 and 13 | The lines of each night |

Make the folders, copy in the configs, the dbt project and the workflow files of the example, and load the two small tables once, as in the first part of the warehousing guide.

```bash
mkdir landing drops
cp exports/restaurants.xlsx exports/customers.csv landing/
cp -R ../../examples/restaurant/config config
cp -R ../../examples/restaurant/dbt dbt
cp -R ../../examples/restaurant/workflows workflows
ldp run config/restaurants.json
ldp run config/customers.json
```

<!-- docs-runner: output -->
```text
restaurants: read 6 rows, wrote 6 rows in ...s (quality: 4 of 4 checks passed)
...
customers: read 150 rows, wrote 150 rows in ...s (quality: 4 of 4 checks passed)
```

The restaurant system exports one night at a time. Three nights have arrived, 1 to 3 August. Copy them into `drops`, the folder the workflow reads. Then make the third night go wrong. In its first order, the channel becomes `drive_thru`, which the orders config does not accept.

```bash
cp exports/orders_2026-08-0[123].csv exports/order_lines_2026-08-0[123].csv drops/
sed -i.bak '2s/,dine_in,/,drive_thru,/' drops/orders_2026-08-03.csv
rm drops/orders_2026-08-03.csv.bak
head -2 drops/orders_2026-08-03.csv
```

<!-- docs-runner: output -->
```text
order_id,restaurant_id,customer_id,order_ts,channel,total_amount,updated_at
O000861,R03,C0037,2026-08-03 12:07:48,drive_thru,30.00,2026-08-03 12:09:48
```

## Part 1. A window and a key

A job that runs every night needs to know which night it is loading. LDP calls that a window, a start and an end with the end left out, so `2026-08-01/2026-08-02` is the first of August. The config `workflows/orders.json` is the orders config of the warehousing guide with two additions. Its source path has the day of the window in it, and `window` says that a window is a day and which column holds the time of a row.

```json
    "source": {
      "name": "orders",
      "format": "CSV",
      "path": "../drops/orders_{window_start:%Y-%m-%d}.csv"
    },
```

```json
    "window": {"size": "day", "column": "order_ts"},
```

A run for a window reads the file of that window and keeps only the rows of that window. The run also gets an idempotency key, which is a hash of the pipeline, the target table and the window. `ldp plan` prints the key without running anything.

```bash
ldp plan workflows/orders.json --window 2026-08-01/2026-08-02
```

<!-- docs-runner: output -->
```text
...
source_path      ../drops/orders_2026-08-01.csv
window           2026-08-01T00:00:00Z/2026-08-02T00:00:00Z
idempotency_key  847cb7c1bd0b8f12c9b11f8808b877bda33a711d98636e8a297befff111dbd98
```

The key is the whole idea. A run that publishes its rows also writes the key into the snapshot, and a run for the same window finds the key and writes nothing. It does not matter whether a person, a scheduler or a retry starts the run, because the key is the same. The [exactly-once guide](exactly_once.md) explains how, and everything below leans on it.

## Part 2. A workflow file

A night has more than one step. The orders and their lines are loaded, the models read them, and old snapshots are tidied away. A workflow file declares the steps and what each waits for. This is `workflows/daily_orders.json`.

```json
{
  "apiVersion": "ldp/v1",
  "kind": "Workflow",
  "identifier": "daily_orders",
  "metadata": {
    "schedule": "5 2 * * *",
    "window": {"size": "day"},
    "retries": {"max_attempts": 2, "delay_s": 1}
  },
  "tasks": [
    {"id": "orders", "pipeline": "orders.json"},
    {"id": "order_lines", "pipeline": "order_lines.json"},
    {
      "id": "models",
      "depends_on": ["orders", "order_lines"],
      "dbt": {"project": "../dbt", "select": ["+revenue_by_restaurant_day", "stg_customers"]}
    },
    {
      "id": "tidy",
      "depends_on": ["models"],
      "maintain": {"config": "orders.json", "expire": 7, "apply": true}
    }
  ]
}
```

The file says four things.

| Key | What it says |
|---|---|
| `kind` and `apiVersion` | That this is a workflow and not a dataset config. Every JSON file of LDP can say what it is, and `ldp schema --kind Workflow` prints the schema of this one |
| `metadata.schedule` | When it runs. `5 2 * * *` is five past two in the morning, and the run is for the day before. `metadata.window.size` says that a window is one day, and `window.timezone` counts the days in another time zone, with UTC as the default |
| `metadata.retries` | How often a task that fails with an error that another try may fix is run again. `max_attempts` counts every try, so 2 is one retry, and `delay_s` is the longest wait before it. A task can have `retries` of its own |
| `tasks` | The steps. Each has an `id`, an optional `depends_on`, and one kind. The kinds are `pipeline` (a dataset config), `dbt` (a dbt project, built for the window) and `maintain` (the options of `ldp maintain`). Paths are relative to the workflow file |

Here the orders and their lines wait for nothing, the models wait for both, and the tidy task waits for the models. The `select` of the dbt task builds the daily revenue model, what it reads, and `stg_customers`, which a data test of `fct_orders` reads, and it runs their data tests. A model that fails a data test blocks the task, as a quality check blocks a pipeline. Tasks run one at a time, in dependency order.

`metadata` can also have `catchup` and `start`, which Part 7 uses, and `observability`, which adds event sinks to the ledger. `ldp workflow plan` checks the file and every config it names, and prints the tasks in the order they run with the key of each pipeline for a window.

```bash
ldp workflow plan workflows/daily_orders.json --window 2026-08-01/2026-08-02
```

<!-- docs-runner: output -->
```text
workflow  daily_orders
...
window    2026-08-01T00:00:00Z/2026-08-02T00:00:00Z
tasks     4
...
1 ... orders ... pipeline ... 847cb7c1bd0b ...
2 ... order_lines ... pipeline ... 4a19d60bf8a5 ...
3 ... models ... dbt ... orders, order_lines ...
4 ... tidy ... maintain ... models ...
(4 rows)
```

The key of the orders task is the key that `ldp plan` printed a moment ago.

## Part 3. Run one day

`ldp workflow run` runs the tasks for one window. dbt writes its own log to standard error before the summary, and this page leaves the log out where it shows output.

```bash
ldp workflow run workflows/daily_orders.json --window 2026-08-01/2026-08-02
```

<!-- docs-runner: output -->
```text
workflow daily_orders  run ...  window 2026-08-01T00:00:00Z/2026-08-02T00:00:00Z
...
orders ... pipeline ... published ... 3 ... 3 ...
order_lines ... pipeline ... published ... 8 ... 8 ...
models ... dbt ... succeeded ...
tidy ... maintain ... succeeded ...
(4 rows)
models published silver.fct_orders: upsert, 0 -> 3 rows, ...
models published gold.revenue_by_restaurant_day: overwrite, 0 -> 3 rows, ...
succeeded: 2 published, 2 succeeded in ...
```

The orders and their lines were written once, under their keys. The models were built, and the two models that are marked for publishing were written to Iceberg as the warehousing guide describes. Run the same command again.

```bash
ldp workflow run workflows/daily_orders.json --window 2026-08-01/2026-08-02
```

<!-- docs-runner: output -->
```text
orders ... pipeline ... skipped_duplicate ...
order_lines ... pipeline ... skipped_duplicate ...
models ... dbt ... skipped_duplicate ...
tidy ... maintain ... succeeded ...
(4 rows)
succeeded: 3 skipped_duplicate, 1 succeeded in ...
```

Nothing was read and nothing was written. The two pipelines found their keys. The dbt task has no key, because a model is a function of its inputs, so the runner looks at the ledger instead. It skips a build when the last build of that window succeeded, the project is the same, and no task before it wrote anything in this run.

## Part 4. A backfill, a bad row and a fix

A new pipeline starts with a history to load, and a pipeline that was broken for a week has a gap. `ldp backfill` runs the workflow for every window of a range, oldest first. The end of the range is not part of it, so this is 1, 2 and 3 August. `--dry-run` says what each window would do and runs nothing.

```bash
ldp backfill workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04 --dry-run
```

<!-- docs-runner: output -->
```text
2026-08-01T00:00:00Z/2026-08-02T00:00:00Z  847cb7c1bd0b (+1)  published
2026-08-02T00:00:00Z/2026-08-03T00:00:00Z  eb361a7bf00b (+1)  missing
2026-08-03T00:00:00Z/2026-08-04T00:00:00Z  870564893d79 (+1)  missing
(3 rows)
3 windows of size day, oldest first: 1 published, 2 missing. Nothing was run.
```

The first day is done, because Part 3 loaded it. A window is done when its pipelines have published under their keys and the last run of the window finished every task. Run the backfill. The third night has the bad row, so the command ends with exit status 3, and `|| echo` prints the status, so that the page can show it.

```bash
ldp backfill workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04 2>&1 || echo "exit status $?"
```

<!-- docs-runner: output -->
```text
...
[2/3] 2026-08-02T00:00:00Z/2026-08-03T00:00:00Z  published
[3/3] 2026-08-03T00:00:00Z/2026-08-04T00:00:00Z  blocked
...
orders: DataQualityError: 1 of 5 data quality checks failed:
- accepted_values(channel): 1 of 7 rows have a channel outside ['dine_in', 'takeaway', 'delivery']; e.g. ['drive_thru']
...
backfill daily_orders: 1 published, 1 skipped, 1 blocked, 0 failed in ...
...
exit status 3
```

The first day was skipped without being read, the second was loaded, and the third was blocked by the checks of the orders config. Nothing of the third night's orders was written. Exit status 3 means that a data gate held a run back. It is not an error to retry, because the data has to change before another run helps, and every scheduler should treat it that way. The [table of exit statuses](#exit-statuses) is below.

The ledger next to the workflow file records every run. `ldp runs` lists them, newest first. This is the third night.

```bash
ldp runs workflows/.ldp/events.jsonl --limit 5
```

<!-- docs-runner: output -->
```text
... tidy ... upstream_failed ...
... models ... upstream_failed ...
... order_lines ... published ...
... orders ... blocked_quality ...
... daily_orders ... blocked_quality ...
(5 rows)
```

The orders were blocked. The order lines did not depend on them, so they were loaded. The models and the tidy task waited for the orders and did not run, so they are `upstream_failed`. A failed or blocked task stops what depends on it, and the rest still runs.

Now the restaurant system sends the third night again, with the right channel. Run the same command. There is no flag for resuming.

```bash
sed -i.bak '2s/,drive_thru,/,dine_in,/' drops/orders_2026-08-03.csv
rm drops/orders_2026-08-03.csv.bak
ldp backfill workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04
```

<!-- docs-runner: output -->
```text
[3/3] 2026-08-03T00:00:00Z/2026-08-04T00:00:00Z  published
backfill daily_orders: 1 published, 2 skipped, 0 blocked, 0 failed in ...
```

Only the third night ran. Its orders were published, its order lines were found by their key, and the models and the tidy task ran, because they had not run for that night yet. A third run does nothing at all.

```bash
ldp backfill workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04
```

<!-- docs-runner: output -->
```text
backfill daily_orders: 0 published, 3 skipped, 0 blocked, 0 failed in ...
```

So a backfill that stopped at a gate, or a crash, or Ctrl-C, is resumed by running it again. The windows that are done are skipped without being read, however long ago they ran.

## Part 5. Replace a day

Sometimes the source changes after a night was loaded, and the restaurant system corrects an amount. The key of the window is on the table, so running the backfill again skips it, and the correction is not loaded. Correct the first order of 2 August, from 260.00 to 10.00, in the file.

```python
import csv

path = "drops/orders_2026-08-02.csv"
with open(path, newline="") as handle:
    rows = list(csv.reader(handle))
rows[1][5] = "10.00"
with open(path, "w", newline="") as handle:
    csv.writer(handle, lineterminator="\n").writerows(rows)
print(rows[1])
```

<!-- docs-runner: output -->
```text
['O000857', 'R05', 'C0100', '2026-08-02 13:22:11', 'takeaway', '10.00', '2026-08-02 13:29:11']
```

```bash
ldp backfill workflows/daily_orders.json --from 2026-08-02 --to 2026-08-03
```

<!-- docs-runner: output -->
```text
backfill daily_orders: 0 published, 1 skipped, 0 blocked, 0 failed in ...
```

`--replace` writes a window again, under a replace key of its own, with a window overwrite. The overwrite replaces only the rows of that window, which is why every pipeline of the workflow has a `window.column`, and a workflow that lacks one is refused before anything runs. The dbt task builds the window again whatever it built before.

```bash
ldp backfill workflows/daily_orders.json --from 2026-08-02 --to 2026-08-03 --replace
```

<!-- docs-runner: output -->
```text
[1/1] 2026-08-02T00:00:00Z/2026-08-03T00:00:00Z  published
backfill daily_orders: 1 published, 0 skipped, 0 blocked, 0 failed in ...
```

The day has the same four orders as before, and the corrected amount.

```bash
ldp query workflows "SELECT cast(order_ts AS date) AS day, count(*) AS orders, sum(total_amount) AS amount FROM raw.orders GROUP BY 1 ORDER BY 1"
```

<!-- docs-runner: output -->
```text
day         orders  amount
----------  ------  ------
2026-08-01       3    1950
2026-08-02       4    4100
2026-08-03       7    4150
```

Replacing the same window again, with the same file and the same config, is skipped too, because the replace key is on the table. `ldp query` reads the tables of every config in a folder, so pointing it at `workflows` queries the tables of this workflow.

## Part 6. A late file and a retry

The fourth night's drop has not arrived. Run the workflow for it anyway, as a scheduler would at five past two. The files are missing, which is an error that another try may fix, so each task is tried twice, with at most a second between the tries.

```bash
ldp workflow run workflows/daily_orders.json --window 2026-08-04/2026-08-05 2>&1 || echo "exit status $?"
```

<!-- docs-runner: output -->
```text
...
orders ... pipeline ... failed ...
order_lines ... pipeline ... failed ...
models ... dbt ... upstream_failed ...
tidy ... maintain ... upstream_failed ...
...
exit status 1
```

Exit status 1 means an error. A scheduler should retry that, because the cause may pass, and it is the right thing to do with a file that has not landed. The `attempts` column of the table says that each pipeline was tried twice, and `ldp runs --attempts` lists every try.

```bash
ldp runs workflows/.ldp/events.jsonl --attempts --limit 8
```

<!-- docs-runner: output -->
```text
... order_lines ... failed ...
... order_lines ... failed ...
... orders ... failed ...
... orders ... failed ...
...
```

A retry is the next attempt of the same run. The runner keeps the run id of the task and the idempotency key, and counts the attempt up. The first commit of attempt N fences attempts 1 to N - 1, so a try that was slow, or that a crash left behind, cannot publish after a later one. A load that reached the table before the error came back is found by its key and not written twice.

## Part 7. A scheduler

`ldp schedule tick` is the whole scheduler. It looks at every workflow file and every dataset config with a schedule under a folder, runs every window that is due and not done, and exits. cron or launchd calls it every 5 minutes. There is no server and no daemon, and a laptop that was asleep catches up at the first tick after it wakes.

The restaurant system sends the fourth night, and it is five past two on 5 August. `--now` gives the tick that time, and without it the tick uses the clock. The window of the latest fire is the day before, 4 August, and the last run of that window failed, so the tick runs it again as the next attempt of the same run.

```bash
cp exports/orders_2026-08-04.csv exports/order_lines_2026-08-04.csv drops/
ldp schedule tick . --now 2026-08-05T02:05:00Z
```

<!-- docs-runner: output -->
```text
daily_orders  2026-08-04T00:00:00Z/2026-08-05T00:00:00Z  published  (attempt 2)
tick 2026-08-05T02:05:00Z: 1 workflow, 1 window due, 1 published, 0 skipped, 0 blocked, 0 failed in ...
```

The workflow run went on with its second attempt, and each pipeline task went on with its third. The earlier tries are in the ledger, and the table has the night once.

```bash
ldp runs workflows/.ldp/events.jsonl --attempts --limit 14
```

<!-- docs-runner: output -->
```text
... 3 ... orders ... published ...
...
... 2 ... orders ... failed ...
... 1 ... orders ... failed ...
...
```

`ldp commits` lists the publishes on a table, each with its key. There is one for each of the four nights, and two for the replacement of 2 August, which share a key of their own. A replacement deletes the rows of its window and then appends the new ones.

```bash
ldp commits workflows/orders.json
```

<!-- docs-runner: output -->
```text
LDP publishes on main of raw.orders, newest first:
...
... 11044a7508f8922c39f01479a2ab261a859b214d254d4bab5e6cb79af30f533e ...
... append ...
... delete ...
... 870564893d79ec5278ead654393bc3fc37db221d3d82a422a5ffa06a1293245f ...
... eb361a7bf00b446f0ee928ded59510f48365c2e107aea583e7752a66ac985ea0 ...
... 847cb7c1bd0b8f12c9b11f8808b877bda33a711d98636e8a297befff111dbd98 ...
(6 rows)
```

`ldp schedule list` says what is scheduled, when it fires next in UTC and in your local time, how the last run of each ended, what is due, and what was missed.

```bash
ldp schedule list . --now 2026-08-05T02:06:00Z
```

<!-- docs-runner: output -->
```text
...
daily_orders  Workflow  workflows/daily_orders.json
schedule     5 2 * * *, in UTC, windows of one day
next fire    2026-08-06T02:05:00Z, ... local
last window  2026-08-04T00:00:00Z/2026-08-05T00:00:00Z succeeded
due          nothing
missed       none
```

Now suppose that the laptop was off for four nights and wakes on the morning of 9 August. By default a tick runs only the window of the latest fire, so it runs 8 August. The nights before it are missed, and the listing names the command that fills them.

```bash
ldp schedule list . --now 2026-08-09T02:06:00Z
```

<!-- docs-runner: output -->
```text
...
due          1 window, 2026-08-08T00:00:00Z/2026-08-09T00:00:00Z
missed       3 windows. Fill them with
ldp backfill workflows/daily_orders.json --from 2026-08-05 --to 2026-08-08
```

That is the backfill of Part 4, and it skips what is done. To make a tick run every window it missed, set `"catchup": true` and a `"start"` in the `metadata` of the workflow, and a tick runs at most 1000 windows, oldest first, and goes on where it stopped at the next tick.

A tick takes a lock on `.ldp/locks/schedule.lock` in the folder and does not wait for it, so a tick that starts while another runs does nothing and exits with 0. The operating system releases the lock when the process ends, so a crash leaves none behind. A tick exits with 0 when nothing failed or was blocked, with 3 when a data gate is the only problem, and with 1 otherwise, and a workflow that cannot be ticked does not stop the others.

### The recipes for launchd and cron

`ldp schedule install` prints the recipe that calls the tick every 5 minutes, with the absolute path of the `ldp` command and of the folder, and it never writes to a system location.

On a Mac, use launchd. Apple has deprecated cron, and cron jobs do not run while the Mac sleeps. launchd runs an interval that went by during the sleep once, when the Mac wakes.

```bash
ldp schedule install --print launchd .
```

<!-- docs-runner: output -->
```text
<?xml version="1.0" encoding="UTF-8"?>
...
<key>StartInterval</key>
<integer>300</integer>
<key>RunAtLoad</key>
<true/>
...
```

The comment at the top of the plist says where to save it and how to load and unload it. Save it into `~/Library/LaunchAgents`, and load it with `launchctl bootstrap`.

<!-- docs-runner: skip it changes what your Mac runs in the background -->
```bash
ldp schedule install --print launchd . > ~/Library/LaunchAgents/restaurant.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/restaurant.plist
```

On Linux, use cron. The line makes the `.ldp` folder first, because the shell opens the log before the tick can make it.

```bash
ldp schedule install --print cron .
```

<!-- docs-runner: output -->
```text
# ldp schedule tick for ..., every 5 minutes
*/5 * * * * mkdir -p ... && ... schedule tick ... >> .../.ldp/schedule.log 2>&1
```

<!-- docs-runner: skip it changes your crontab -->
```bash
(crontab -l 2>/dev/null; ldp schedule install --print cron .) | crontab -
```

## Part 8. Airflow, Dagster and Prefect

Everything above runs on your laptop with no service. If your team already runs an orchestrator, it can call the same code.

**Airflow 3.** `LdpRunOperator` runs one config as a task. Airflow's data interval becomes the window, its try number becomes the attempt, and its run id gives the run id, so an Airflow retry or backfill is a retry or backfill of a window and a day is loaded once. The [Airflow guide](airflow.md) installs Airflow, runs the daily drops of this example and shows a BashOperator recipe that calls `ldp run --window`.

**Dagster and Prefect.** A workflow is a Python call, so any orchestrator can run it. `run_workflow` runs the workflow for a window and returns a result with a `status`, an `exit_code` that follows the table below, and the result of each task.

The drop of 5 August has landed. This runs the workflow for it from Python.

```bash
cp exports/orders_2026-08-05.csv exports/order_lines_2026-08-05.csv drops/
```

```python
from local_data_platform.orchestration import run_workflow

result = run_workflow("workflows/daily_orders.json", window="2026-08-05/2026-08-06")
print(result.status, result.exit_code, result.counts())
```

<!-- docs-runner: output -->
```text
succeeded 0 {'published': 2, 'succeeded': 2}
```

A wrapper only has to turn a result that is not a success into a failure of its own kind. Leave the retries to the workflow, which retries what another try may fix, and to the orchestrator for the rest.

<!-- docs-runner: skip it needs Dagster, which this repo does not install -->
```python
from dagster import asset

from local_data_platform.orchestration import run_workflow


@asset
def daily_orders(context):
    result = run_workflow("workflows/daily_orders.json", window="yesterday")
    context.log.info(result.counts())
    if result.exit_code:
        raise RuntimeError(f"{result.workflow} {result.status}: {result.counts()}")
```

<!-- docs-runner: skip it needs Prefect, which this repo does not install -->
```python
from prefect import flow

from local_data_platform.orchestration import run_workflow


@flow
def daily_orders(window: str = "yesterday"):
    result = run_workflow("workflows/daily_orders.json", window=window)
    if result.exit_code:
        raise RuntimeError(f"{result.workflow} {result.status}: {result.counts()}")
    return result.to_dict()
```

An exit status 3 is a data gate. If your orchestrator retries every failure, make the wrapper raise something that it does not retry for 3, as `LdpRunOperator` does with `AirflowFailException`. Both snippets were run against this example, with Dagster 1.13.26 and Prefect 3.8.8.

## Exit statuses

Every `ldp` command follows one table, and `ldp backfill`, `ldp workflow run` and `ldp schedule tick` use it for the whole range, run or tick.

| Exit | Meaning | What a scheduler does |
|---|---|---|
| 0 | Success, including a window that published and a window that was skipped because it was done | Nothing |
| 1 | An error, such as a bad config, a missing file or a failed write | Retries it, because the cause may pass |
| 2 | Reserved for an SLA breach that `ldp monitor` reports in 0.1.5 | |
| 3 | A data gate held a run back, such as a quality check that blocked a write, or a failing dbt data test | Never retries it, because the data has to change first, and tells a person |

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `ldp backfill` exits with 3 and a window says `blocked` | A quality check or a dbt data test held the window back, and the message names it | Fix the source, and run the same command again. The windows that are done are skipped |
| `ldp backfill` or a tick exits with 1 and a window says `failed` | A task raised an error, such as a file that is not there, after its retries | Fix the cause. The next tick runs the window as the next attempt of the same run, and `ldp backfill` runs it as a run of its own |
| A source changed after a window was loaded, and nothing happens | The key of the window is on the table, so the window is skipped | Run `ldp backfill ... --replace` for that window |
| `--replace` says that a pipeline needs a `window.column` | A replace would add the rows of the window again, because there is no column to tell them apart | Set `metadata.window.column` to the column that holds the time of each row |
| A tick prints that another tick holds the lock and exits with 0 | A tick of the same folder is still running | Nothing. That tick does the work, and the next one finds nothing to do |
| `ldp workflow plan` says that dbt was not found | The dbt task needs the `dbt` extra | `pip install -e ".[dbt,excel]"` from the clone |
| You want to start again | | Delete `ldp_demo/orchestration`, and begin again at Make the data |

## What to read next

- [Exactly-once writes](exactly_once.md) explains the key, the run id, the attempt and the fence that every part of this chapter leans on.
- [Run a config from Airflow 3](airflow.md) installs Airflow on a laptop and runs the daily drops of this example from a DAG.
- The [contract](design/v0_1_1.md#workflow-files-013) has the rules of workflow files, backfills and schedules, down to the time zone and clock change cases.
- [`examples/restaurant`](https://github.com/tusharchou/local-data-platform/tree/main/examples/restaurant) has the generator, the configs, the workflow files and the `run.py` walkthrough that this chapter did by hand. `make demo-orchestration` runs it in about 20 seconds with dbt, and in about a second without.
- [Warehousing with Excel, CSV and dbt](warehousing.md) is the chapter before this one, and has the dbt project that the workflow builds.
