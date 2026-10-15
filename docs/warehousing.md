# Warehousing: from Excel and CSV exports to dbt-tested tables

**You will learn** how to load an Excel sheet and CSV files into Iceberg tables, model them with dbt, publish the models back to Iceberg, test them, load them incrementally, and read a table as it was before a late batch changed it.

**It takes** about 45 minutes. The commands run in about two minutes, and the rest is reading.

**You need** Python 3.12 or newer, git, and a clone of this repo, because the data comes from the restaurant example in `examples/restaurant`. Nothing is downloaded after the install, and there is no server and no account. The test suite runs every command on this page, except the install and the one that serves the docs, in `tests/test_docs_warehousing.py`, so the output you see here is what the code prints.

## The story

A small restaurant chain has six restaurants. Its system exports the restaurants as an Excel workbook, and the customers and the orders as CSV files. The orders come in batches. The first batch holds January to June 2026. The second holds July, and five orders from the end of June that a till only synced in July.

The chapter has three parts, and they are what a small data team does with exports like these.

1. Load them into Iceberg tables, with checks, and query them.
2. Put a dbt project on top of the tables, and publish a model of your own back to Iceberg.
3. Use the finished restaurant project to get monthly customer churn. Then load the second batch, see what changes, and look at a table as it was.

## Install

The restaurant example lives in the repo, so you work from a clone. The `dbt` extra adds dbt-core and the dbt-duckdb adapter, and the `excel` extra adds the reader for `.xlsx` files.

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

Everything this guide creates goes into `ldp_demo/warehousing`, and git ignores `ldp_demo`, so you can delete the folder to start again. The generator uses a fixed seed, so you get the same files as this page shows.

```bash
mkdir -p ldp_demo/warehousing
cd ldp_demo/warehousing
python ../../examples/restaurant/generate.py --out exports
```

<!-- docs-runner: output -->
```text
Wrote 22 files to exports: 6 restaurants, 150 customers, 883 orders in 9 batches, 2195 order lines and 94 reviews
```

This guide uses three of the files, and the second batch later on.

| File | What it is |
|---|---|
| `restaurants.xlsx` | The six restaurants, as an Excel workbook with a title row above the header |
| `customers.csv` | The 150 customers |
| `orders_batch1.csv` | The 745 orders from January to June 2026 |
| `orders_batch2.csv` | The 108 orders of the second batch, 103 from July and 5 late ones from June |

The other files are used by later milestones of the [roadmap](roadmap.md). From here on, every command runs in `ldp_demo/warehousing`.

## Part 1. Load the raw tables

A restaurant system drops its exports into a folder. Make that folder, which is called `landing`, and put the first files in it. The orders config reads `landing/orders.csv`, so batch 1 goes there under that name. Then copy the example's configs. There is one JSON config for each dataset.

```bash
mkdir landing
cp exports/restaurants.xlsx exports/customers.csv landing/
cp exports/orders_batch1.csv landing/orders.csv
cp -R ../../examples/restaurant/config config
ls config landing
```

<!-- docs-runner: output -->
```text
config:
customers.json
orders.json
restaurants.json
restaurants_to_csv.json

landing:
customers.csv
orders.csv
restaurants.xlsx
```

A config says where a dataset comes from, where it goes, and what must be true of it first. This is the part of `config/restaurants.json` that describes the Excel workbook and the Iceberg table it becomes.

```json
    "source": {
      "name": "restaurants",
      "format": "XLSX",
      "path": "../landing/restaurants.xlsx",
      "sheet": "Restaurants",
      "header_row": 2
    },
    "target": {
      "name": "restaurants",
      "format": "ICEBERG",
      "catalog": {
        "identifier": "raw",
        "warehouse_path": "../warehouse"
      },
      "write_mode": "overwrite"
    },
```

The paths are relative to the folder of the config. The workbook has a title row, so `header_row` is 2 and the row above it is skipped. The table is `raw.restaurants`, in the namespace `raw`, and every load replaces it, because an export of the restaurants is always complete. The orders config says `append` instead, because every batch adds rows, and it partitions the table by `day(order_ts)` and `restaurant_id`.

The same config holds the checks. They run before anything is written, and `on_failure` is `fail`, so a batch that breaks one is stopped and the table is not touched.

```json
    "quality": {
      "on_failure": "fail",
      "checks": [
        {"check": "row_count", "min": 1},
        {"check": "not_null", "columns": ["restaurant_id", "name"]},
        {"check": "unique", "columns": ["restaurant_id"]},
        {"check": "schema", "columns": {
          "restaurant_id": "string", "name": "string", "location": "string", "cuisine": "string",
          "created_at": "timestamp[us]"}}
      ]
    },
```

`ldp plan` shows what a config will do without running it. The route is the pipeline that moves the data, picked from the source and target formats.

```bash
ldp plan config/restaurants.json
```

<!-- docs-runner: output -->
```text
identifier  restaurants
...
route       XLSX -> ICEBERG [ExcelToIceberg]
target      raw.restaurants
write_mode  overwrite
catalog     local
quality     4 checks, on_failure=fail
...
```

The workbook can also become a CSV file, which is the route to use when another tool wants the sheet as a file. The config for it has the same source and a CSV target.

```bash
ldp run config/restaurants_to_csv.json
```

<!-- docs-runner: output -->
```text
restaurants_to_csv: read 6 rows, wrote 6 rows in ...s
```

Now load the three raw tables. The restaurants and the customers are overwritten, and the orders are appended.

```bash
ldp run config/restaurants.json
ldp run config/customers.json
ldp run config/orders.json
```

<!-- docs-runner: output -->
```text
restaurants: read 6 rows, wrote 6 rows in ...s (quality: 4 of 4 checks passed)
  target raw.restaurants: overwrite, 0 -> 6 rows, snapshot ...
...
customers: read 150 rows, wrote 150 rows in ...s (quality: 4 of 4 checks passed)
  target raw.customers: overwrite, 0 -> 150 rows, snapshot ...
...
orders: read 745 rows, wrote 745 rows in ...s (quality: 5 of 5 checks passed)
  target raw.orders: append, 0 -> 745 rows, snapshot ...
...
```

Each write is a commit, and each commit is a snapshot of the table. `ldp snapshots` lists them.

```bash
ldp snapshots config/orders.json
```

<!-- docs-runner: output -->
```text
Snapshots of Iceberg table raw.orders, oldest first:
...
... append ... 745 ... 745 ... NULL
(1 row)
```

`ldp query` runs SQL over the tables with DuckDB. Give it a folder of configs and every table in the folder is there, named `namespace.table`, so SQL can join them.

```bash
ldp query config "SELECT r.name, count(*) AS orders FROM raw.orders o JOIN raw.restaurants r USING (restaurant_id) GROUP BY r.name ORDER BY orders DESC"
```

<!-- docs-runner: output -->
```text
name             orders
---------------  ------
Biryani Bazaar      146
Spice Route         145
Chaat Junction      141
Kolkata Kitchen     126
Brew & Bun          111
Dosa Corner          76
(6 rows)
```

That is part 1. The data is in Iceberg, it was checked on the way in, and you can query it.

## Part 2. A dbt project over the tables

dbt turns `select` statements into tables and tests them. You write each model as a SQL file, dbt runs the models in the order they depend on each other, and the tests you declare in YAML run on the results. dbt does not know Iceberg, so `ldp dbt` puts LDP on both sides of it.

1. **Before dbt runs**, LDP registers every Iceberg table that your configs describe as a source, in a DuckDB file inside the project. A model reads one with `{{ source('raw', 'orders') }}`.
2. **dbt runs** against that file, in a subprocess, as it would on any laptop.
3. **After a successful build**, LDP writes each model that you marked `ldp_publish` back to Iceberg. It goes through the same route as any other load, with the checks, the run events and the snapshots. If any model or test failed, nothing is published.

Make a project. `--sources` is the folder of configs whose tables become the sources, and `--warehouse` is where the published tables go.

```bash
ldp dbt init first_project --sources config --warehouse warehouse
```

<!-- docs-runner: output -->
```text
ldp: created a dbt project in ...
  dbt_project.yml
  profiles.yml
  ldp_dbt.json
  models/staging/stg_example.sql
  models/gold/gold_example.sql
  .gitignore
Next: ldp dbt build first_project
```

Two of the files are about the connection between dbt and LDP. `profiles.yml` is dbt's own file. It sends dbt to a DuckDB file in `target`, and it turns off the download of DuckDB extensions, so dbt never fetches one.

```bash
cat first_project/profiles.yml
```

<!-- docs-runner: output -->
```text
first_project:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: target/ldp_build.duckdb
      threads: 4
      settings:
        # The views over Iceberg tables need DuckDB's iceberg extension. dbt must never download one.
        autoinstall_known_extensions: false
```

`ldp_dbt.json` is what LDP needs. It names the build file, the configs whose tables are the sources, and where the published models go.

```bash
cat first_project/ldp_dbt.json
```

<!-- docs-runner: output -->
```text
{
  "database": "target/ldp_build.duckdb",
  "sources": {
    "configs": [
      "../config"
    ],
    "catalogs": []
  },
  "publish": {
    "catalog": {
      "identifier": "gold",
      "warehouse_path": "../warehouse"
    },
    "config_dir": "target/ldp"
  }
}
```

The project has two example models, a staging view and a gold table. A staging model tidies a source, and a gold model is the table people use. Add a third, a model of your own. It counts the orders and the revenue for each channel. The `meta` line is the mark that tells LDP to publish it, and the folder `models/gold` makes it a table, as `dbt_project.yml` says.

```bash
cat > first_project/models/gold/orders_by_channel.sql <<'EOF'
{{ config(meta={'ldp_publish': true}) }}

select
    channel,
    count(*) as orders,
    round(sum(total_amount), 2) as revenue
from {{ source('raw', 'orders') }}
group by channel
EOF
```

Build the project. LDP registers the sources and prints that it did, then dbt builds the three models, and then LDP publishes the two that carry the mark.

```bash
ldp dbt build first_project
```

<!-- docs-runner: output -->
```text
...
... Found 3 models, 3 sources, ...
...
... Done. PASS=3 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=3
ldp: published gold.gold_example: overwrite, 0 -> 1 rows, snapshot ...
ldp: published gold.orders_by_channel: overwrite, 0 -> 3 rows, snapshot ...
```

The sources are registered as views over the Iceberg tables when DuckDB's `iceberg` extension is installed, and as copies of them when it is not, with a warning that says so. Nothing is downloaded either way, and the results are the same.

LDP also wrote `models/_ldp_sources.yml`, which tells dbt the columns of each raw table, and that is how `{{ source('raw', 'orders') }}` works. The file is rewritten on every build, so do not edit it.

```bash
sed -n 1,10p first_project/models/_ldp_sources.yml
```

<!-- docs-runner: output -->
```text
# Generated by `ldp dbt`. It is rewritten on every build, so do not edit it.
version: 2
sources:
  - name: "raw"
    schema: "raw"
    tables:
      - name: "customers"
        columns:
          - name: "customer_id"
            data_type: "VARCHAR"
```

`stg_example` is in dbt only, because it has no mark. The other two models are now Iceberg tables, in the namespace `gold`. Each has a config in `first_project/target/ldp`, and it is an ordinary dataset config. Its source is the DuckDB build file, in the `DUCKDB` format, and its target is the Iceberg table.

```bash
cat first_project/target/ldp/orders_by_channel.json
```

<!-- docs-runner: output -->
```text
{
  "identifier": "orders_by_channel",
  "where": "dbt project first_project",
  "how": "ldp dbt build",
  "metadata": {
    "source": {
      "name": "orders_by_channel",
      "format": "DUCKDB",
      "path": "../ldp_build.duckdb",
      "relation": "main.orders_by_channel"
    },
    "target": {
      "name": "orders_by_channel",
      "format": "ICEBERG",
      "write_mode": "overwrite",
      "catalog": {
        "identifier": "gold",
        "warehouse_path": "../../../warehouse"
      }
    }
  }
}
```

Because it is an ordinary config, the commands that work on any table work on a published model, such as `ldp snapshots`, `ldp query`, `ldp runs`, `ldp maintain` and `ldp mcp`. Query it.

```bash
ldp query first_project/target/ldp "SELECT * FROM orders_by_channel ORDER BY revenue DESC"
```

<!-- docs-runner: output -->
```text
channel   orders  revenue
--------  ------  -------
dine_in      388   299290
delivery     194   151510
takeaway     163   123040
(3 rows)
```

That is part 2. A dbt project reads the lakehouse, and what it builds goes back into the lakehouse.

## Part 3. The restaurant project

The finished project for the restaurant chain is in the example. Copy it next to your own.

```bash
cp -R ../../examples/restaurant/dbt restaurant_dbt
find restaurant_dbt/models -name "*.sql" | sort
```

<!-- docs-runner: output -->
```text
restaurant_dbt/models/gold/churn_by_month.sql
restaurant_dbt/models/gold/customer_activity_monthly.sql
restaurant_dbt/models/gold/revenue_by_restaurant_day.sql
restaurant_dbt/models/silver/fct_orders.sql
restaurant_dbt/models/staging/stg_customers.sql
restaurant_dbt/models/staging/stg_orders.sql
restaurant_dbt/models/staging/stg_restaurants.sql
```

It has seven models.

| Model | What it is |
|---|---|
| `stg_restaurants`, `stg_customers`, `stg_orders` | Views that tidy the three raw tables. `stg_orders` keeps every version of an order, because the raw orders are append-only |
| `fct_orders` | One row for each order, the latest version of it. It is incremental, and it is published to the namespace `silver` |
| `customer_activity_monthly` | One row for each customer and each month in which they ordered |
| `churn_by_month` | The share of customers who stop ordering in each month. It is published to `gold` |
| `revenue_by_restaurant_day` | Orders and revenue for each restaurant on each day. It is published to `gold` |

Build it. dbt runs the seven models and 26 data tests, and LDP publishes the three models that carry the mark.

```bash
ldp dbt build restaurant_dbt
```

<!-- docs-runner: output -->
```text
...
... Found 7 models, 26 data tests, 3 sources, ...
...
... Done. PASS=33 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=33
ldp: published silver.fct_orders: upsert, 0 -> 745 rows, snapshot ...
ldp: published gold.churn_by_month: overwrite, 0 -> 5 rows, snapshot ...
ldp: published gold.revenue_by_restaurant_day: overwrite, 0 -> 534 rows, snapshot ...
```

The three tables have configs in `config`, next to the ones for the raw tables, because this project's `ldp_dbt.json` publishes there. So one folder of configs now holds the raw, silver and gold tables, and one query can join them.

### Tests

The 26 data tests are declared in `models/schema.yml`. A key must be unique and not null. An order must belong to a restaurant that exists, so its `restaurant_id` has to be in `stg_restaurants`. The channel must be one of three values.

```yaml
      - name: order_id
        description: The order's key.
        data_tests: [unique, not_null]
      - name: restaurant_id
        description: The restaurant that took the order.
        data_tests:
          - not_null
          - relationships:
              arguments:
                to: ref('stg_restaurants')
                field: restaurant_id
```

```yaml
      - name: channel
        description: How the order was served.
        data_tests:
          - accepted_values:
              arguments:
                values: ['dine_in', 'takeaway', 'delivery']
```

A test that does not fit a ready-made one is a SQL file in `tests`. It passes when it returns no rows. This one says that a churn rate is a share.

```sql
select month, churn_rate
from {{ ref('churn_by_month') }}
where churn_rate < 0 or churn_rate > 1
```

`ldp dbt build` runs the tests too, and a failing test fails the build, so nothing is published from a build whose tests did not pass. To run only the tests, for example before you build a new batch, use `ldp dbt test`. It publishes nothing.

```bash
ldp dbt test restaurant_dbt
```

<!-- docs-runner: output -->
```text
...
... Done. PASS=26 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=26
```

### Churn

A customer churns in the month they stop ordering. For a month M, `customers_at_start` is the number of customers with an order in the month before, and `churned` is how many of them have no order in month M. The rate is the second divided by the first. The first month has no row, because there is no month before it. The definition is in the model and in its description, and later milestones reuse it.

```bash
ldp query config "SELECT month, customers_at_start, churned, churn_rate FROM churn_by_month ORDER BY month"
```

<!-- docs-runner: output -->
```text
month       customers_at_start  churned  churn_rate
----------  ------------------  -------  ----------
2026-02-01                  68       16    0.235294
2026-03-01                  68       24    0.352941
2026-04-01                  71       24    0.338028
2026-05-01                  75       23    0.306667
2026-06-01                  74       21    0.283784
(5 rows)
```

Of the 74 customers who ordered in May, 21 did not order in June. Keep that number in mind.

### Documentation

dbt writes a documentation site from the models, the descriptions in `schema.yml`, the tests and the lineage between the models. `ldp dbt docs` generates it.

```bash
ldp dbt docs restaurant_dbt
```

<!-- docs-runner: output -->
```text
...
... Catalog written to ...
```

To read it in a browser, add `--serve`. It serves the site on `http://127.0.0.1:8080` until you press Ctrl-C.

<!-- docs-runner: skip it serves the docs until Ctrl-C -->
```bash
ldp dbt docs restaurant_dbt --serve
```

### A second batch

The restaurant system has sent July. Before you load it, write down the time, because you will use it later to look back at this moment. The `sleep` makes sure the time is after the last commit of the first build, since Iceberg commit times have milliseconds and `date` prints whole seconds.

```bash
sleep 1
date -u +"%Y-%m-%d %H:%M:%S" > batch_1_published.txt
```

Now replace the file that the orders config reads with batch 2, load it, and build again.

```bash
cp exports/orders_batch2.csv landing/orders.csv
ldp run config/orders.json
ldp dbt build restaurant_dbt
```

<!-- docs-runner: output -->
```text
orders: read 108 rows, wrote 108 rows in ...s (quality: 5 of 5 checks passed)
  target raw.orders: append, 745 -> 853 rows, snapshot ...
...
... Done. PASS=33 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=33
ldp: published silver.fct_orders: upsert, 745 -> 853 rows, snapshot ...
ldp: published gold.churn_by_month: overwrite, 5 -> 6 rows, snapshot ...
ldp: published gold.revenue_by_restaurant_day: overwrite, 534 -> 621 rows, snapshot ...
```

Five of the 108 orders were placed in June and only reached the lake in July. A model that kept only the orders placed after the newest one it already had would skip them. `fct_orders` does not, because it compares each incoming order with its own row. This is the model's header and the end of its query.

```sql
{{ config(
    materialized='incremental',
    unique_key='order_id',
    incremental_strategy='delete+insert',
    meta={'ldp_publish': true, 'ldp_namespace': 'silver'}
) }}
```

```sql
from {{ ref('stg_orders') }} as incoming
{% if is_incremental() %}
where not exists (
    select 1
    from {{ this }} as loaded
    where loaded.order_id = incoming.order_id and loaded.updated_at >= incoming.updated_at
)
{% endif %}
qualify rank() over (partition by order_id order by updated_at desc) = 1
```

On the first build the table does not exist, so `is_incremental()` is false and every order comes through, the latest version of each. On the next builds an order is taken when the table has no row for it, or only an older version. A taken order replaces its earlier row, as `unique_key` and `delete+insert` say.

LDP publishes the model with an upsert on `order_id`, so only the rows that are new or changed are written. The second snapshot of `silver.fct_orders` adds 108 records, and no other row was written.

```bash
ldp snapshots config/fct_orders.json
```

<!-- docs-runner: output -->
```text
Snapshots of Iceberg table silver.fct_orders, oldest first:
...
... append ... 745 ... 745 ... NULL
... append ... 853 ... 108 ... NULL
(2 rows)
```

### Time travel

The late orders belong to customers who looked as if they had stopped ordering in June. Now that the orders are here, June's churn is lower.

```bash
ldp query config/churn_by_month.json "SELECT month, customers_at_start, churned, churn_rate FROM churn_by_month WHERE month = DATE '2026-06-01'"
```

<!-- docs-runner: output -->
```text
month       customers_at_start  churned  churn_rate
----------  ------------------  -------  ----------
2026-06-01                  74       16    0.216216
(1 row)
```

It was 21 churned customers, and now it is 16. The old number is not lost, because every build made a snapshot of `churn_by_month`. It was overwritten, and an overwrite is a delete and an append, so after two builds the table has three snapshots.

```bash
ldp snapshots config/churn_by_month.json
```

<!-- docs-runner: output -->
```text
Snapshots of Iceberg table gold.churn_by_month, oldest first:
...
... append ... 5 ... 5 ... NULL
... delete ... 0 ... NULL ... 5
... append ... 6 ... 6 ... NULL
(3 rows)
```

`--snapshot` reads a table as of one of them. The first line below takes the id of the first row of the list, which is the table as batch 1 left it.

```bash
FIRST=$(ldp snapshots config/churn_by_month.json | awk '$1 ~ /^[0-9]+$/ && !seen {print $1; seen = 1}')
ldp query config/churn_by_month.json "SELECT month, customers_at_start, churned, churn_rate FROM churn_by_month WHERE month = DATE '2026-06-01'" --snapshot "$FIRST"
```

<!-- docs-runner: output -->
```text
month       customers_at_start  churned  churn_rate
----------  ------------------  -------  ----------
2026-06-01                  74       21    0.283784
(1 row)
```

`--as-of` takes a time instead of an id. It reads the newest snapshot that was committed at or before the time, and the time is UTC unless you give an offset. Use the time you wrote down.

```bash
ldp query config/churn_by_month.json "SELECT month, customers_at_start, churned, churn_rate FROM churn_by_month WHERE month = DATE '2026-06-01'" --as-of "$(cat batch_1_published.txt)"
```

<!-- docs-runner: output -->
```text
month       customers_at_start  churned  churn_rate
----------  ------------------  -------  ----------
2026-06-01                  74       21    0.283784
(1 row)
```

A report that someone printed after batch 1 said that June lost 21 customers. Now you can show where today's 16 comes from.

### One query over everything

Because the published tables have configs in the same folder as the raw ones, a query over the folder joins any of them. This one puts the churn next to the number of orders in each month.

```bash
ldp query config "SELECT c.month, c.churn_rate, count(o.order_id) AS orders FROM churn_by_month c JOIN fct_orders o ON date_trunc('month', o.order_ts) = c.month GROUP BY ALL ORDER BY c.month"
```

<!-- docs-runner: output -->
```text
month       churn_rate  orders
----------  ----------  ------
2026-02-01    0.235294     114
2026-03-01    0.352941     133
2026-04-01    0.338028     131
2026-05-01    0.306667     127
2026-06-01    0.216216     126
2026-07-01         0.4     103
(6 rows)
```

That is part 3. The models are tested, the loads are incremental, and every build can be read again as it was.

Running `ldp dbt build` on a schedule, so that a new batch is loaded and built without you, is not part of this chapter. It is the subject of the [orchestration guide](orchestration.md), which runs this project as a task of a workflow.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `ldp dbt` says it needs the dbt extra | dbt is not installed in this environment | `pip install -e ".[dbt,excel]"` from the clone |
| A warning that the sources are copies | DuckDB's `iceberg` extension is not installed, so LDP copied each table into the build file instead of viewing it | Nothing, the results are the same. To get views, run `python -c "import duckdb; duckdb.connect().execute('INSTALL iceberg')"` once, while you are online |
| `dbt build failed with exit status 1, so nothing was published` | A model or a test failed, and the lines after the message name it | Fix the model, the data or the test and build again. The Iceberg tables are as they were |
| `ldp run` stops on a failed check | A batch broke a rule in the config. Nothing was written | Look at the check in the message, fix the file, and run again |
| A published incremental model gains a column and the build fails | An upsert replaces whole rows and cannot add a column | Publish it once with `ldp run config/fct_orders.json --mode overwrite`, and the next build upserts again |
| You want to start again | | Delete `ldp_demo/warehousing`, and begin again at Make the data |

## What to read next

- [Warehousing with dbt](design/warehousing.md) is the design behind `ldp dbt`. It has the contract for sources, the publish step, the meta keys and the schema rules.
- [`examples/restaurant`](https://github.com/tusharchou/local-data-platform/tree/main/examples/restaurant) has the generator, the configs, the dbt project and the `run.py` walkthrough that this chapter did by hand. `make demo-warehouse` runs it in about 25 seconds.
- [Orchestrating with workflows, backfills and a scheduler](orchestration.md) runs the loads and the dbt build of this chapter every night, backfills a range of days and retries what failed.
- [MCP server for agents](agents.md) shows how to give an AI agent read-only access to the tables you published, with `ldp mcp --config config`.
- [Recipes](recipes.md) has more small tasks, such as copying an Excel sheet into a Google Drive folder.
