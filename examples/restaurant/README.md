# Restaurant chain: Excel and CSV exports to tested tables, with dbt

A small restaurant chain's data on a laptop. The restaurant system exports its restaurants as an Excel workbook and
its customers and orders as CSV files. The example loads them into Iceberg, models them with dbt, tests the models,
and publishes the ones you mark back to Iceberg as versioned tables. It ends with monthly customer churn, and with
what time travel shows when late orders change history. Everything is synthetic and deterministic, runs offline, and
takes about 25 seconds.

```bash
pip install -e ".[dbt,excel]"                                              # from the repo root
python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer warehouse   # or: make demo-warehouse
```

This is the `warehouse` layer. It is the first layer of an example that the later milestones of the
[roadmap](../../docs/roadmap.md) extend, so the same restaurants, customers and orders go through each layer.
`--layer` runs one layer, and the layers that it needs first. Without the `dbt` or the `excel`
extra, `run.py` says which one to install and exits with 1. The design is in [PRD.md](PRD.md), and the
[warehousing guide](../../docs/warehousing.md) does the same steps by hand with the `ldp` commands. The second layer,
`orchestration`, is [below](#the-orchestration-layer).

## What happens

| Step | What the platform does | Where |
|---|---|---|
| 1. Excel to CSV | `ExcelToCSV` turns the sheet `Restaurants` of `restaurants.xlsx`, whose header is on row 2 below a title row, into `landing/restaurants.csv` | `config/restaurants_to_csv.json` |
| 2. Ingest | `ExcelToIceberg` and `CSVToIceberg` load `raw.restaurants`, `raw.customers` and the first batch of `raw.orders` (January to June 2026). Each load passes its quality checks first. The orders are partitioned by `day(order_ts)` and `restaurant_id` | `config/*.json` |
| 3. Model | `ldp dbt build` registers the raw tables as dbt sources, runs 7 models and 26 data tests, and publishes `silver.fct_orders`, `gold.churn_by_month` and `gold.revenue_by_restaurant_day` | `dbt/` |
| 4. Churn | `ldp query` reads churn for February to June | |
| 5. Batch 2 | July arrives with 5 late orders from June. The build upserts only those 108 rows of `fct_orders` and rebuilds the gold tables | |
| 6. Time travel | `ldp query --snapshot` reads June's churn as it was published after batch 1, and now | |

The walkthrough prints each step. This is what it prints for the build, the churn and the time travel
(`<workdir>` is the folder you gave):

```text
== 3. Model it with dbt: ldp dbt build
   $ ldp dbt build <workdir>/dbt
   7 models and 26 data tests: 33 of 33 passed, in 10.9s
   published silver.fct_orders: upsert, 0 -> 745 rows
   published gold.churn_by_month: overwrite, 0 -> 5 rows
   published gold.revenue_by_restaurant_day: overwrite, 0 -> 534 rows

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
- `churned` is the number of those customers with no order in month M, and
- `churn_rate` is `churned` divided by `customers_at_start`, or null when `customers_at_start` is 0.

The first month has no row, because there is no month before it. The months run from the first month with an order to
the last, so a month in which nobody ordered still has a row. The definition is in the model
(`dbt/models/gold/churn_by_month.sql`) and in its dbt description, and the later milestones that report churn, the
gold layer in 0.1.4 and the insights in 0.1.7, use the same convention.

An example from [issue #47](https://github.com/tusharchou/local-data-platform/issues/47), which
`tests/test_restaurant_warehouse.py` reproduces:

| Orders | Month | `customers_at_start` | `churned` | `churn_rate` |
|---|---|---|---|---|
| C1, C2 and C3 in January | 2023-01 | no row | | |
| C4 in February | 2023-02 | 3 | 3 | 1.0 |
| C4 and C1 in March | 2023-03 | 1 | 0 | 0.0 |
| C1 in April | 2023-04 | 2 | 1 | 0.5 |
| nobody in May | 2023-05 | 1 | 1 | 1.0 |
| C1 in June | 2023-06 | 0 | 0 | null |

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
| `raw.orders` | `config/orders.json`, append | Every order of every batch loaded, and nothing is removed |
| `silver.fct_orders` | dbt, incremental, upsert on `order_id` | One row for each order, the latest version |
| `gold.churn_by_month` | dbt, table, overwrite | Churn by month |
| `gold.revenue_by_restaurant_day` | dbt, table, overwrite | Orders and revenue for each restaurant on each day |

The other models, `stg_restaurants`, `stg_customers`, `stg_orders` and `customer_activity_monthly`, stay in dbt. A
model is published when its config has `meta={'ldp_publish': true}`, and `ldp_namespace` says where. The
published models get a config in `config/` next to the ingest configs, so one folder holds every table:

```bash
ldp snapshots ldp_demo/restaurant/config/churn_by_month.json
ldp query ldp_demo/restaurant/config "SELECT c.month, c.churn_rate, count(o.order_id) AS orders FROM churn_by_month c JOIN fct_orders o ON date_trunc('month', o.order_ts) = c.month GROUP BY ALL ORDER BY c.month"
ldp query ldp_demo/restaurant/config/churn_by_month.json "SELECT * FROM churn_by_month WHERE month = DATE '2026-06-01'" --snapshot SNAPSHOT_ID
ldp runs ldp_demo/restaurant/config/fct_orders.json
ldp dbt docs ldp_demo/restaurant/dbt --serve
ldp mcp --config ldp_demo/restaurant/config
```

## The tests

`dbt/models/schema.yml` has 26 data tests. Every key has a `unique` and a `not_null` test, except `stg_orders.order_id`:
the raw orders keep every version of an order, so it is not unique there, and `fct_orders` is where it must be.
Orders have `relationships` tests to customers and to restaurants, `channel` has `accepted_values`, and a singular test
in `dbt/tests/` checks that `churn_rate` is between 0 and 1. All of them pass on the generated data.

The quality checks of the ingest configs run before a batch is written, and look at one file: its types, its channels
and whether a key is missing. dbt's tests look at everything that has been loaded. A batch that has the same new order
twice, with the same `updated_at`, passes the ingest and fails `unique_fct_orders_order_id` in the build, because
`fct_orders` cannot tell which row is the later one. The build then publishes nothing, because nothing is published
unless every model and every test passed, and `tests/test_restaurant_warehouse.py` does exactly that. Two versions of an
order with different `updated_at` are not a duplicate, and the later one is kept. A batch that repeats orders that were
loaded before changes nothing downstream, because `fct_orders` only takes orders it has no row for and versions that are
newer than the one it has.

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
`landing/orders.csv`, the way an export lands in the same place every time. This milestone loads the restaurants,
customers and orders. The menu, order lines and reviews wait for the layers that use them.

## The orchestration layer

```bash
python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer orchestration   # or: make demo-orchestration
```

The second layer runs the restaurant's nightly drops as a workflow, the way a scheduler would. It needs no extra. It loads the
two small tables that the models read, runs the `models` task, which is the dbt project above, when the `dbt` extra is
installed, and prints a skip line when it is not. It takes about 20 seconds with dbt and about a second without it.
`--layer orchestration` runs this layer on its own, in a work folder that it clears first, as the other layer does.

| Step | What the platform does | Where |
|---|---|---|
| 1. Drops | Three nights of orders and order lines land in `drops/`. In the file of the third night, one order has a channel that the checks do not accept | `drops/` |
| 2. Plan | `ldp workflow plan` lists the tasks `orders`, `order_lines`, `models` and `tidy` in the order they run, with the key of the window | `workflows/daily_orders.json` |
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
     orders: DataQualityError: 1 of 5 data quality checks failed:
       - accepted_values(channel): 1 of 7 rows have a channel outside ['dine_in', 'takeaway', 'delivery']; e.g. ['drive_thru']
   Exit status 3: a data gate held 3 August back, and the data has to change before another run

== 4. Fix the row, and run the same command again
   $ ldp backfill <workdir>/workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04
     [3/3] 2026-08-03T00:00:00Z/2026-08-04T00:00:00Z  published
     backfill daily_orders: 1 published, 2 skipped, 0 blocked, 0 failed in 0.1s. Events: <workdir>/workflows/.ldp/events.jsonl
```

The [orchestration guide](../../docs/orchestration.md) does the same steps by hand, and adds a replaced day, a retried
task, launchd and cron, and Airflow, Dagster and Prefect.

## Files

| Path | What it is |
|---|---|
| `generate.py` | The seeded generator |
| `config/` | The four ingest configs. `run.py` copies them into the work folder, and `ldp dbt` writes the published models' configs next to them |
| `dbt/` | The dbt project: `dbt_project.yml`, `profiles.yml`, `ldp_dbt.json`, the models, `schema.yml` and a singular test |
| `workflows/` | The daily workflow, `daily_orders.json`, and the two windowed configs it runs, `orders.json` and `order_lines.json`. They read the files of `drops/` |
| `run.py` | The narrated walkthrough |
| `airflow_dag.py` | A one-task Airflow 3 DAG that loads the daily drops, one data interval at a time. [The Airflow guide](../../docs/airflow.md) runs it |
| `PRD.md` | The product requirements, and the tables of the example |

The work folder gets `landing/`, `warehouse/`, `config/`, `dbt/`, `workflows/` and `drops/`, `.ldp/` when a scheduler ticks, and a
marker file. `run.py` clears those folders on each run, and refuses a folder that it did not create.

## Tests

`tests/test_restaurant_example.py` covers the generator, the configs, the ingest and the dbt project without running
dbt. `tests/test_restaurant_warehouse.py` runs the whole walkthrough with dbt (it needs the `dbt` extra, and `pytest -m dbt`
runs it) and checks the numbers above, a batch with a duplicate order, `ldp dbt docs` and the #47 churn example.
`tests/test_orchestration_demo.py` runs the orchestration layer offline, without dbt in every job and with dbt in the
`dbt` job, and `tests/test_docs_orchestration.py` runs every command of the orchestration guide.
