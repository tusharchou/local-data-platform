# Restaurant chain: Excel and CSV exports to tested tables, with dbt

A small restaurant chain's data on a laptop. The restaurant system exports its restaurants as an Excel workbook and
its customers, menu, orders, order lines and reviews as CSV files. The example loads them into Iceberg, models them
with dbt, tests the models, and publishes the ones you mark back to Iceberg as versioned tables. It ends with monthly
customer churn, and with what time travel shows when late orders change history. Everything is synthetic and
deterministic, runs offline, and takes about 10 seconds.

```bash
pip install -e ".[dbt,excel]"                                              # from the repo root
python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer warehouse   # or: make demo-warehouse
```

This is the `warehouse` layer. It is the first layer of an example that the later milestones of the
[roadmap](../../docs/roadmap.md) extend, so the same restaurants, customers and orders go through each layer.
`--layer` runs one layer, and the layers that it needs first. Without the `dbt` or the `excel`
extra, `run.py` says which one to install and exits with 1. The design is in [PRD.md](PRD.md), and the
[warehousing guide](../../docs/warehousing.md) does the same steps by hand with the `ldp` commands. The second layer,
`orchestration`, is [below](#the-orchestration-layer), and the third, `gold`, is [below that](#the-gold-layer).

## What happens

| Step | What the platform does | Where |
|---|---|---|
| 1. Excel to CSV | `ExcelToCSV` turns the sheet `Restaurants` of `restaurants.xlsx`, whose header is on row 2 below a title row, into `landing/restaurants.csv` | `config/restaurants_to_csv.json` |
| 2. Ingest | `ExcelToIceberg` and `CSVToIceberg` load `raw.restaurants`, `raw.customers`, `raw.menu_items`, `raw.reviews` and the first batch of `raw.orders` and `raw.order_lines` (January to June 2026). Each load passes its quality checks first. The orders are partitioned by `day(order_ts)` and `restaurant_id` | `config/*.json` |
| 3. Model | `ldp dbt build` registers the raw tables as dbt sources, runs 12 models and 41 data tests, and publishes `silver.fct_orders` and the gold tables `churn_by_month`, `gold_daily_sales`, `gold_menu_item_weekly` and `gold_restaurant_scorecard` | `dbt/` |
| 4. Churn | `ldp query` reads churn for February to June | |
| 5. Batch 2 | July arrives with 5 late orders from June. The build upserts only those 108 rows of `fct_orders` and rebuilds the gold tables | |
| 6. Time travel | `ldp query --snapshot` reads June's churn as it was published after batch 1, and now | |

The walkthrough prints each step. This is what it prints for the build, the churn and the time travel
(`<workdir>` is the folder you gave):

```text
== 3. Model it with dbt: ldp dbt build
   $ ldp dbt build <workdir>/dbt
   12 models and 41 data tests: 53 of 53 passed, in 3.4s
   published silver.fct_orders: upsert, 0 -> 745 rows
   published gold.churn_by_month: overwrite, 0 -> 5 rows
   published gold.gold_daily_sales: overwrite, 0 -> 534 rows
   published gold.gold_menu_item_weekly: overwrite, 0 -> 795 rows
   published gold.gold_restaurant_scorecard: overwrite, 0 -> 6 rows

== 4. Churn by month, February to June
     month       customers_at_start  churned  churn_rate
     2026-02-01                  68       16       0.235
     2026-03-01                  68       24       0.353
     2026-04-01                  71       24       0.338
     2026-05-01                  75       23       0.307
     2026-06-01                  74       21       0.284

== 5. Batch 2 arrives: 103 orders from July and 5 late ones from June
   published silver.fct_orders: upsert, 745 -> 853 rows
   fct_orders upserted 108 rows, the new and the late ones, and no other row was written.

== 6. Time travel: June's churn before and after the late orders
   June as it was published after batch 1:
     2026-06-01                  74       21       0.284
   June now, with the late orders:
     2026-06-01                  74       16       0.216
```

## Churn

A customer churns **in the month they stop ordering**. For month M:

- `customers_at_start` is the number of customers with an order in month M minus 1,
- `churned` is the number of those customers with no order in month M,
- `churn_rate` is `churned` divided by `customers_at_start`, or null when `customers_at_start` is 0, and
- `active_customers` is the number of customers with an order in month M, which is 0 in a month with no order.

The first month has no row, because there is no month before it. The months run from the first month with an order to
the last, so a month in which nobody ordered still has a row. The definition is in the model
(`dbt/models/gold/churn_by_month.sql`) and in its dbt description, and the later milestones that report churn, the
gold layer in 0.1.4 and the insights in 0.1.7, use the same convention.

An example from [issue #47](https://github.com/tusharchou/local-data-platform/issues/47), which
`tests/test_restaurant_warehouse.py` reproduces:

| Orders | Month | `customers_at_start` | `churned` | `churn_rate` | `active_customers` |
|---|---|---|---|---|---|
| C1, C2 and C3 in January | 2023-01 | no row | | | |
| C4 in February | 2023-02 | 3 | 3 | 1.0 | 1 |
| C4 and C1 in March | 2023-03 | 1 | 0 | 0.0 | 2 |
| C1 in April | 2023-04 | 2 | 1 | 0.5 | 1 |
| nobody in May | 2023-05 | 1 | 1 | 1.0 | 0 |
| C1 in June | 2023-06 | 0 | 0 | null | 1 |

### The churn as metrics

`config/metrics.json` defines two metrics over `gold.churn_by_month`, so that a person, a report or an agent asks for the
number and does not write the SQL again. `churn_rate` is `churned` over `customers_at_start`, in percent, and
`active_customers` is the customers with an order in the month. The time dimension of the model allows only the grain
`month`, because adding up months would count a customer once for each month, so a query by quarter or by year, or with no
grain, is refused with the grains that are allowed. The restaurant example publishes no release, so the tables are read as
they are now. This is what the command prints after the run, for every month of the model.

```bash
ldp metrics query ldp_demo/restaurant/config churn_rate,active_customers --grain month
```

```text
month       churn_rate  active_customers
----------  ----------  ----------------
2026-02-01       23.5%                68
2026-03-01       35.3%                71
2026-04-01       33.8%                75
2026-05-01       30.7%                74
2026-06-01       21.6%                80
2026-07-01       40.0%                64
(6 rows)
read the tables as they are now, because no release has been cut yet
```

A month that has no churn rate, such as June of the example of issue #47, prints `NULL`. `tests/test_restaurant_warehouse.py`
checks that the metrics give the same value as the model for every month, for this data and for the example of issue #47,
and `tests/test_metrics.py` runs the same file over the five rows of that example without dbt.

## Late orders, and how `fct_orders` loads

Batch 2 holds July's orders and five orders that were placed at the end of June and only reached the lake in July,
because a till was offline. Their `order_ts` is in June. `fct_orders` is incremental. It takes an incoming order when
it has no row for that order yet, or only an older version of it (a smaller `updated_at`, which is when the
restaurant system last changed the order), and when several versions arrive together it keeps the newest. It compares
each order with its own earlier row, so a late order is simply an order it has not seen. A filter on `order_ts`, or on
the newest `updated_at` in the table, would have skipped the five late orders, and would drop a correction that
reached the lake out of order. A changed order replaces its earlier row (`unique_key='order_id'`, `delete+insert`),
and `ldp dbt` publishes the model with an upsert, so only new and changed rows are written to Iceberg.

The five late orders belong to customers who had ordered in May and not in June, so June looked like they had left.
When the orders arrive, June's churn falls from 21 of 74 customers to 16 of 74. The snapshot that `churn_by_month` had
after batch 1 still holds the old number, and `ldp query --snapshot` reads it.

## The tables

| Table | Made by | What it holds |
|---|---|---|
| `raw.restaurants` | `config/restaurants.json`, overwrite | The 6 restaurants, from the Excel export |
| `raw.customers` | `config/customers.json`, overwrite | The 150 customers |
| `raw.menu_items` | `config/menu_items.json`, overwrite | The 36 menu items |
| `raw.reviews` | `config/reviews.json`, overwrite | The 94 reviews |
| `raw.orders` | `config/orders.json`, append | Every order of every batch loaded, and nothing is removed |
| `raw.order_lines` | `config/order_lines.json`, append | The lines of every order of every batch loaded |
| `silver.fct_orders` | dbt, incremental, upsert on `order_id` | One row for each order, the latest version |
| `gold.churn_by_month` | dbt, table, overwrite | Churn and active customers by month |
| `gold.gold_daily_sales` | dbt, table, overwrite | Orders, revenue and the average ticket for each restaurant on each day |
| `gold.gold_menu_item_weekly` | dbt, table, overwrite | What each menu item sold in each week |
| `gold.gold_restaurant_scorecard` | dbt, table, overwrite | The last 30 days, the rating and the rank in its city for each restaurant |

The other models, the six `stg_` views and `customer_activity_monthly`, stay in dbt. A
model is published when its config has `meta={'ldp_publish': true}`, and `ldp_namespace` says where. The
published models get a config in `config/` next to the ingest configs, so one folder holds every table. A published
model also carries its contract, which is the description of the model and of its columns in `schema.yml` and, for
the gold models, an `owner` and a `grain` in their `meta`. LDP writes it into the Iceberg table, so
`describe_table` of `ldp mcp` shows it. These commands read the tables.

```bash
ldp snapshots ldp_demo/restaurant/config/churn_by_month.json
ldp query ldp_demo/restaurant/config "SELECT c.month, c.churn_rate, count(o.order_id) AS orders FROM churn_by_month c JOIN fct_orders o ON date_trunc('month', o.order_ts) = c.month GROUP BY ALL ORDER BY c.month"
ldp query ldp_demo/restaurant/config/churn_by_month.json "SELECT * FROM churn_by_month WHERE month = DATE '2026-06-01'" --snapshot SNAPSHOT_ID
ldp metrics query ldp_demo/restaurant/config churn_rate,active_customers --grain month
ldp runs ldp_demo/restaurant/config/fct_orders.json
ldp dbt docs ldp_demo/restaurant/dbt --serve
ldp mcp --config ldp_demo/restaurant/config
```

## The tests

`dbt/models/schema.yml` has 41 data tests. Every key has a `unique` and a `not_null` test, except `stg_orders.order_id`,
because the raw orders keep every version of an order, so it is not unique there, and `fct_orders` is where it must be.
Orders have `relationships` tests to customers and to restaurants, `channel` has `accepted_values`, `active_customers` is
`not_null`, and a singular test in `dbt/tests/` checks that `churn_rate` is between 0 and 1. All of them pass on the
generated data.

The quality checks of the ingest configs run before a batch is written, and look at one file. They see its types, its
channels, its restaurants, whether a key is missing and whether an order is in it twice. dbt's tests look at everything
that has been loaded. The same new order in two files, with the same `updated_at`, passes the ingest, because each file has it once,
and fails `unique_fct_orders_order_id` in the build, because `fct_orders` cannot tell which row is the later one. The
build then publishes nothing, because nothing is published unless every model and every test passed, and
`tests/test_restaurant_warehouse.py` does exactly that. Two versions of an order with different `updated_at` are not a
duplicate, and the later one is kept. A batch that repeats orders that were loaded before changes nothing downstream,
because `fct_orders` only takes orders it has no row for and versions that are newer than the one it has.

## The data

`generate.py` writes the data. The same seed always gives the same files, so every number in this README can be
checked.

| File | Rows | What it is |
|---|---|---|
| `restaurants.xlsx` | 6 | An Excel export with an `About` sheet first and a title row above the header |
| `customers.csv` | 150 | Customers and their home city |
| `orders_batch1.csv` | 745 | Orders from January to June 2026 |
| `orders_batch2.csv` | 108 | Orders of July 2026, and 5 late ones from June |
| `orders_2026-08-01.csv` to `orders_2026-08-07.csv` | 30 in all | One drop file a day, for the milestones that schedule a load for each day |
| `order_lines_<batch>.csv` | 2195 in all | The lines of each order. In a daily drop a line also has the `order_ts` of its order, so that a window can pick the lines of a day |
| `menu_items.csv`, `reviews.csv` | 36 and 94 | The menu, and the reviews of some orders |

`python examples/restaurant/generate.py --out DIR` writes all of them. The walkthrough lands each batch as
`landing/orders.csv` and `landing/order_lines.csv`, the way an export lands in the same place every time. Two
restaurants, `R02` and `R04`, are in Mumbai, so that the rank of a restaurant in its city means something.

## The orchestration layer

```bash
python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer orchestration   # or: make demo-orchestration
```

The second layer runs the restaurant's nightly drops as a workflow, the way a scheduler would. It needs no extra. It loads the
four small tables that the models read, runs the `models` task, which is the dbt project above, and the `gold` task, which
checks the daily sales table and cuts a release, when the `dbt` extra is installed, and prints a skip line when it is not.
It takes about 10 seconds with dbt and about a second without it. The folder it works in has no `metrics.json`, because
the workflow builds one gold table and a gold build holds the metrics of its folder to the tables that exist.
`--layer orchestration` runs this layer on its own, in a work folder that it clears first, as the other layer does.

| Step | What the platform does | Where |
|---|---|---|
| 1. Drops | Three nights of orders and order lines land in `drops/`. In the file of the third night, one order has a channel that the checks do not accept | `drops/` |
| 2. Plan | `ldp workflow plan` lists the tasks `orders`, `order_lines`, `models`, `gold` and `tidy` in the order they run, with the key of the window | `workflows/daily_orders.json` |
| 3. Backfill | `ldp backfill --from 2026-08-01 --to 2026-08-04` publishes 1 and 2 August, a data gate holds back 3 August with exit status 3, and the tasks that wait for the orders do not run | `workflows/orders.json`, `workflows/order_lines.json` |
| 4. Fix | After the row is fixed, the same command runs only 3 August | |
| 5. Third run | Every window is done, so nothing is read and nothing is written | |
| 6. Next night | `ldp schedule tick --now 2026-08-05T02:05:00Z` loads 4 August. `ldp schedule list`, `ldp runs`, `ldp commits` and `ldp schedule install --print cron` show what happened | |

Without dbt, this is what it prints for the backfills (`<workdir>` is the folder you gave).

```text
== 3. Backfill the three days: the first run
   $ ldp backfill <workdir>/workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04
     [1/3] 2026-08-01T00:00:00Z/2026-08-02T00:00:00Z  published
     [2/3] 2026-08-02T00:00:00Z/2026-08-03T00:00:00Z  published
     [3/3] 2026-08-03T00:00:00Z/2026-08-04T00:00:00Z  blocked
     backfill daily_orders: 2 published, 0 skipped, 1 blocked, 0 failed in 0.4s. Events: <workdir>/workflows/.ldp/events.jsonl
     orders: DataQualityError: 1 of 7 data quality checks failed:
       - accepted_values(channel): 1 of 7 rows have a channel outside ['dine_in', 'takeaway', 'delivery']; e.g. ['drive_thru']
   Exit status 3: a data gate held 3 August back, and the data has to change before another run

== 4. Fix the row, and run the same command again
   $ ldp backfill <workdir>/workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04
     [3/3] 2026-08-03T00:00:00Z/2026-08-04T00:00:00Z  published
     backfill daily_orders: 1 published, 2 skipped, 0 blocked, 0 failed in 0.1s. Events: <workdir>/workflows/.ldp/events.jsonl
```

The [orchestration guide](../../docs/orchestration.md) does the same steps by hand, and adds a replaced day, a retried
task, launchd and cron, and Airflow, Dagster and Prefect.

## The gold layer

```bash
python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer gold   # or: make demo-gold
```

The third layer is the data mart that people who are not engineers can use, which is the restaurant group's revenue, its
best sellers, its ratings and its churn, as tables with an owner, a meaning and a gate. It needs the `dbt` and `excel`
extras, loads the raw tables itself, and takes about 5 seconds.

| Step | What the platform does | Where |
|---|---|---|
| 1. Raw tables | The restaurants, customers, menu and reviews, and the orders and order lines of January to July, load with the checks of their configs | `config/*.json` |
| 2. A faulty batch | The orders of 1 to 7 August arrive as one file with a negative amount, an order of a restaurant that does not exist and an order that is in the file twice. The checks of the orders config stop it, and each failed check is named | `config/orders.json` |
| 3. The week again | The restaurant system sends the week without the faults, and it loads | |
| 4. Model | `ldp dbt build` runs the 12 models and the 41 data tests and publishes the five tables below, each gold table with its contract and its gate | `dbt/` |
| 5. Release | `ldp gold build` checks the four gold tables, checks that the metrics fit them, and cuts release `r1`, which is what readers see | `config/` |
| 6. A metric | `ldp metrics query` gives revenue, orders and the average ticket by cuisine and month | `config/metrics.json` |
| 7. For people | `ldp gold export` writes an Excel file of the scorecard, `ldp gold duckdb` one DuckDB file of the four tables, and `ldp gold docs` a catalog page | `out/` |

**The gate.** This is what the faulty file gets, from the second step of the layer (`<workdir>` is the folder you gave).
The three faults are in three different orders, and the checks find each of them. Nothing of the file is written, so the
raw table has no bad row that a later build would have to carry.

```text
== 2. A faulty batch arrives, and the gate stops it
   The restaurant system sends the orders of 1 to 7 August as one file, landing/orders.csv. It has three
   faults, a negative amount, a restaurant that does not exist and an order that is in the file twice,
   each in an order of its own:
     O000855  total_amount is -40.00
     O000858  restaurant_id is R99
     O000861  the order is in the file twice
   $ ldp run <workdir>/config/orders.json
     ldp: error: 3 of 7 data quality checks failed:
       - accepted_values(restaurant_id): 1 of 31 rows have a restaurant_id outside ['R01', 'R02', 'R03', 'R04', 'R05', 'R06']; e.g. ['R99']
       - unique(order_id, updated_at): 1 duplicated key(s) in (order_id, updated_at) across 2 rows; e.g. {'order_id': 'O000861', 'updated_at': '2026-08-03T12:09:48'} x2
       - range(total_amount): 1 of 31 rows violate total_amount >= 0: 1 below min, 0 above max
   Exit status 3 means that a data gate held the run back, and a retry would not help, because
   the file has to change. Nothing was written, and raw.orders still has 853 rows.
```

Exit status 3 is the status of a data gate in every `ldp` command, so a workflow marks the task `blocked_quality` and a
scheduler does not retry it. The checks that look at one file are in the config of the orders, and the dbt tests are the
second gate, for what one file cannot show, such as the same order in two files.

**The release.** The four gold tables are built by dbt, so `ldp gold build` checks them. Each has the gate that its
model declares in `meta`, a list of checks that also ran before dbt published it.

```text
== 5. Check the gold tables and cut a release: ldp gold build
   $ ldp gold build <workdir>/config
     churn_by_month             checked         7 rows  snapshot <id>  4 of 4 checks passed
     gold_daily_sales           checked       643 rows  snapshot <id>  5 of 5 checks passed
     gold_menu_item_weekly      checked       953 rows  snapshot <id>  5 of 5 checks passed
     gold_restaurant_scorecard  checked         6 rows  snapshot <id>  5 of 5 checks passed
     gold build <workdir>/config: 0 built, 4 checked, 0 blocked, 0 failed, 0 not run in 0.1s
     metrics: 3 semantic models and 6 metrics fit the gold tables
     release r1 cut with 4 tables and 1609 rows in gold
```

A gold table that breaks its gate stops the release, and readers keep the last one. `tests/test_restaurant_gold.py`
breaks each of the four tables twice, with a value out of range and with a repeated row, and checks that.

**The gold tables.**

| Table | Owner | One row is | What is in it |
|---|---|---|---|
| `gold_daily_sales` | restaurant operations | A restaurant on a day it had an order | Orders, revenue and the average ticket, with the city and the cuisine |
| `gold_menu_item_weekly` | kitchen operations | A menu item in a week in which it was ordered | Orders, quantity and revenue. A week starts on Monday |
| `gold_restaurant_scorecard` | restaurant operations | A restaurant | Orders and revenue of the 30 days that end on the newest order, the rating from all its reviews, and its rank in its city by revenue |
| `churn_by_month` | customer insights | A month | Customers at the start, churned, the churn rate and the active customers |

`gold_daily_sales` replaces `revenue_by_restaurant_day` of the warehouse layer, so the example keeps one daily sales table.
Two restaurants are in Mumbai, so that the rank in the city of the scorecard is 1 for one of them and 2 for the other.

**The metrics.** `config/metrics.json` defines six metrics over three of the tables, and every reader gets the same
numbers, from the command line, from an agent that calls `query_metric`, or from Python.

| Metric | Table | How it is computed | Grains |
|---|---|---|---|
| `revenue` | `gold_daily_sales` | The sum of `revenue` | day to year |
| `orders` | `gold_daily_sales` | The sum of `orders` | day to year |
| `avg_ticket` | `gold_daily_sales` | The sum of `revenue` over the sum of `orders` | day to year |
| `avg_rating` | `gold_restaurant_scorecard` | The sum of the stars over the number of reviews | none, it is as of one day |
| `churn_rate` | `churn_by_month` | `churned` over `customers_at_start` | month |
| `active_customers` | `churn_by_month` | The sum of `active_customers` | month |

An average ticket is the revenue of a period over its orders, and not the average of the daily average tickets. The rating of
a city is all its stars over all its reviews, and not the average of the ratings of its restaurants. The churn is only by month,
because adding up months would count a customer once for each month.

```bash
ldp metrics query ldp_demo/restaurant/config revenue,avg_ticket --by cuisine --grain month
ldp gold list ldp_demo/restaurant/config
ldp gold show ldp_demo/restaurant/config gold_daily_sales
ldp gold export ldp_demo/restaurant/config gold_restaurant_scorecard scorecard.xlsx
ldp mcp --config ldp_demo/restaurant/config --layer gold
```

**The daily workflow.** `workflows/daily_orders.json` runs the same steps every night. It loads the day's orders and lines,
builds the daily sales model with dbt, runs the `gold` task, which checks the gold table and cuts a release, and tidies the
orders table. A day that a gate holds back cuts no release, so the readers of the gold layer see the last good one. The
[orchestration layer](#the-orchestration-layer) above shows it, and the work folder of that layer has the releases `r1` to
`r4`, one for each night that passed.

## Files

| Path | What it is |
|---|---|
| `generate.py` | The seeded generator |
| `config/` | The seven ingest configs and `metrics.json`. `run.py` copies them into the work folder, and `ldp dbt` writes the published models' configs next to them |
| `dbt/` | The dbt project: `dbt_project.yml`, `profiles.yml`, `ldp_dbt.json`, the models, `schema.yml` and a singular test |
| `workflows/` | The daily workflow, `daily_orders.json`, and the two windowed configs it runs, `orders.json` and `order_lines.json`. They read the files of `drops/` |
| `run.py` | The narrated walkthrough |
| `airflow_dag.py` | A one-task Airflow 3 DAG that loads the daily drops, one data interval at a time. [The Airflow guide](../../docs/airflow.md) runs it |
| `PRD.md` | The product requirements, and the tables of the example |

The work folder gets `landing/`, `warehouse/`, `config/`, `dbt/`, `workflows/`, `drops/` and `out/`, `.ldp/` when a scheduler
ticks, and a marker file. `run.py` clears those folders on each run, and refuses a folder that it did not create.

## Tests

`tests/test_restaurant_example.py` covers the generator, the configs, the ingest and the dbt project without running
dbt. `tests/test_restaurant_warehouse.py` runs the whole walkthrough with dbt (it needs the `dbt` extra, and `pytest -m dbt`
runs it) and checks the numbers above, a batch with a duplicate order, `ldp dbt docs` and the #47 churn example.
`tests/test_orchestration_demo.py` runs the orchestration layer offline, without dbt in every job and with dbt in the
`dbt` job, and `tests/test_docs_orchestration.py` runs every command of the orchestration guide.
`tests/test_restaurant_gold.py` runs the gold layer with dbt, and checks the blocked batch, the row counts, every gold
gate, the release and the metrics against numbers that it works out in plain Python from the generated data.
