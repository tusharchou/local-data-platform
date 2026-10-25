# A gold layer that people who are not engineers can use, with owners, releases, metrics and files

**You will learn** how bronze, silver and gold tables differ, how to publish a gold table that says who owns it, what it means and what one row is, how a release gives every reader the same version of all the gold tables, how to find a table with `ldp gold list`, `ldp gold show` and a catalog page, how to define a number once as a metric and ask for it, how to hand a table to someone as an Excel file or one DuckDB file, and how to let an AI agent read only the gold tables.

**It takes** about 45 minutes. The commands run in about a minute, and the rest is reading.

**You need** Python 3.12 or newer, git, and a clone of this repo, because the data comes from the restaurant example in `examples/restaurant`. The chapter uses the `dbt`, `excel` and `mcp` extras, and the dbt project of [the warehousing guide](warehousing.md). No command of the chapter downloads anything after the install, and there is no server and no account. The test suite runs every command on this page, except the install, in `tests/test_docs_gold.py`, so the output you see here is what the code prints.

## The story

The restaurant group has six restaurants in five cities. The owners, the kitchen managers and the customer team want to answer their own questions. They want to know how much each restaurant sold last month, which dishes sell best, how guests rate the restaurants and how many customers stop ordering. Today each question is a request to an engineer, and three things go wrong sooner or later.

1. Two people ask for the average ticket and get two numbers. One averaged the daily averages, and the other divided the revenue by the orders.
2. A table is read while it is half rebuilt, so a report adds up to a different total than the report before it.
3. A table that nobody can explain is used for something it does not mean.

A gold layer is the answer to all three. It is a set of tables that are meant to be read by people. Each table says who owns it, what it means, what one row is and what each column holds, and each is checked before it is written. All of them are published together as a release, so every reader sees tables from the same build. A number such as the average ticket is defined once. And the tables reach people who do not write code as a page, an Excel file and one DuckDB file.

The chapter has eight parts. A section for people who are not engineers follows them, and it can be read on its own.

1. Meet the three layers and load the bronze tables.
2. Stop a bad batch at the gate.
3. Publish gold tables with dbt and read their contract.
4. Check the tables and cut a release.
5. Find a table.
6. Ask for a number.
7. Give the tables to people.
8. Let an agent read the gold layer.

## Install

The restaurant example lives in the repo, so you work from a clone. This is the install of the warehousing guide, with the `mcp` extra added for the last part.

<!-- docs-runner: skip it installs the package, and this run already has it -->
```bash
git clone https://github.com/tusharchou/local-data-platform.git
cd local-data-platform
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dbt,excel,mcp]"
ldp --version
```

## Make the data

Everything this guide creates goes into `ldp_demo/gold`, and git ignores `ldp_demo`, so you can delete the folder to start again. The generator uses a fixed seed, so you get the same files as this page shows.

```bash
mkdir -p ldp_demo/gold
cd ldp_demo/gold
python ../../examples/restaurant/generate.py --out exports
```

<!-- docs-runner: output -->
```text
Wrote 22 files to exports: 6 restaurants, 150 customers, 883 orders in 9 batches, 2195 order lines and 94 reviews
```

This guide uses the four small tables and the two batches of orders, which are January to June and July. The first week of August comes later, in the second part.

| File | Rows | What it is |
|---|---|---|
| `restaurants.xlsx`, `customers.csv`, `menu_items.csv` and `reviews.csv` | 6, 150, 36 and 94 | The small tables the models read |
| `orders_batch1.csv` and `order_lines_batch1.csv` | 745 and 1829 | The orders of January to June and their lines |
| `orders_batch2.csv` and `order_lines_batch2.csv` | 108 and 284 | The orders of July and their lines |

## Part 1. Bronze, silver and gold

A lakehouse keeps data in layers, and each layer makes a different promise.

| Layer | What is in it | Who reads it | What it promises |
|---|---|---|---|
| Bronze | The data as the source sent it, one table for each export | Engineers | Only that the file passed the checks of its config |
| Silver | The same data cleaned, typed and deduplicated, and joined where it has to be | Engineers and analysts | That the data tests of its model pass |
| Gold | Tables made to answer questions, such as sales for each restaurant and day | Anyone | That it means what it says, and that it was checked before it was written |

The restaurant lake has six bronze tables in the namespace `raw`, one silver table, `silver.fct_orders`, and four gold tables in the namespace `gold`. LDP asks nothing of bronze and silver tables, and it holds a table to the gold promise when the contract of the table says that its layer is gold, or when its namespace is called `gold`. The contract is the owner, the meaning, the grain and the description of each column, and Part 3 shows it.

The bronze tables come first. Each has a config that says where its file is, where the table goes and which checks the file has to pass. Copy the exports and the configs, and load the six tables that the models read, with the orders and the order lines of January to June.

```bash
mkdir landing
cp exports/restaurants.xlsx exports/customers.csv exports/menu_items.csv exports/reviews.csv landing/
cp exports/orders_batch1.csv landing/orders.csv
cp exports/order_lines_batch1.csv landing/order_lines.csv
cp -R ../../examples/restaurant/config config
ldp run config/restaurants.json
ldp run config/customers.json
ldp run config/menu_items.json
ldp run config/reviews.json
ldp run config/orders.json
ldp run config/order_lines.json
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
orders: read 745 rows, wrote 745 rows in ...s (quality: 7 of 7 checks passed)
...
order_lines: read 1829 rows, wrote 1829 rows in ...s (quality: 4 of 4 checks passed)
```

July arrives as a second batch, and it is appended to the two tables that grow.

```bash
cp exports/orders_batch2.csv landing/orders.csv
cp exports/order_lines_batch2.csv landing/order_lines.csv
ldp run config/orders.json
ldp run config/order_lines.json
```

<!-- docs-runner: output -->
```text
orders: read 108 rows, wrote 108 rows in ...s (quality: 7 of 7 checks passed)
  target raw.orders: append, 745 -> 853 rows, snapshot ...
...
order_lines: read 284 rows, wrote 284 rows in ...s (quality: 4 of 4 checks passed)
  target raw.order_lines: append, 1829 -> 2113 rows, snapshot ...
```

## Part 2. A bad batch stops at the gate

The restaurant system now sends the orders of 1 to 7 August as one file. Someone typed three things wrong. A total is negative, an order says it is from restaurant `R99`, which does not exist, and one order is in the file twice. The generator has a function that writes this file, so you can see what the checks do with it.

```python
import sys

sys.path.insert(0, "../../examples/restaurant")
import generate

dataset = generate.generate(generate.SEED)
path, faults = generate.write_faulty_week("landing", dataset)
for fault in faults:
    print(fault.order_id, fault.detail)
```

<!-- docs-runner: output -->
```text
O000855 total_amount is -40.00
O000858 restaurant_id is R99
O000861 the order is in the file twice
```

Load it. The `|| echo` prints the exit status, so that the page can show it.

```bash
ldp run config/orders.json 2>&1 || echo "exit status $?"
```

<!-- docs-runner: output -->
```text
ldp: error: 3 of 7 data quality checks failed:
  - accepted_values(restaurant_id): 1 of 31 rows have a restaurant_id outside ['R01', 'R02', 'R03', 'R04', 'R05', 'R06']; e.g. ['R99']
  - unique(order_id, updated_at): 1 duplicated key(s) in (order_id, updated_at) across 2 rows; e.g. {'order_id': 'O000861', 'updated_at': '2026-08-03T12:09:48'} x2
  - range(total_amount): 1 of 31 rows violate total_amount >= 0: 1 below min, 0 above max
exit status 3
```

Three of the seven checks of the orders config failed, and each failed check is named with an example row. The checks look at the whole file before anything is written, so nothing was written. The table still has the 853 orders it had.

```bash
ldp query config "SELECT count(*) AS orders FROM raw.orders"
```

<!-- docs-runner: output -->
```text
orders
------
   853
(1 row)
```

Exit status 3 is the status of a data gate in every `ldp` command. A scheduler must not retry it, because a retry reads the same file, and the file has to change first. This is the first gate of the lake. It keeps the faults out of bronze, so no silver or gold table has to carry them. The [orchestration guide](orchestration.md) shows what a workflow does when a night is held back like this.

The restaurant fixes the file and sends the week again. It has 30 orders and 82 lines, and they load.

```python
import sys

sys.path.insert(0, "../../examples/restaurant")
import generate

dataset = generate.generate(generate.SEED)
generate.write_week("landing", dataset)
```

```bash
ldp run config/orders.json
ldp run config/order_lines.json
```

<!-- docs-runner: output -->
```text
orders: read 30 rows, wrote 30 rows in ...s (quality: 7 of 7 checks passed)
  target raw.orders: append, 853 -> 883 rows, snapshot ...
...
order_lines: read 82 rows, wrote 82 rows in ...s (quality: 4 of 4 checks passed)
  target raw.order_lines: append, 2113 -> 2195 rows, snapshot ...
```

## Part 3. Publish gold tables with dbt, and read their contract

The restaurant's dbt project is the one of the [warehousing guide](warehousing.md), with twelve models. Copy it next to the configs. It reads the raw tables, builds a silver table, `fct_orders`, and four gold tables.

```bash
cp -R ../../examples/restaurant/dbt dbt
ls dbt/models/gold
```

<!-- docs-runner: output -->
```text
churn_by_month.sql
customer_activity_monthly.sql
gold_daily_sales.sql
gold_menu_item_weekly.sql
gold_restaurant_scorecard.sql
```

`customer_activity_monthly` is a step on the way to the churn, and it is not published. The other four are gold tables, and this is what makes a model one. The top of `gold_daily_sales.sql` is its configuration.

```sql
{{ config(
    tags=['sales'],
    meta={
        'ldp_publish': true,
        'ldp_namespace': 'gold',
        'owner': 'restaurant operations',
        'grain': 'one row for each restaurant on each day it had an order',
        'ldp_quality': {
            'on_failure': 'fail',
            'checks': [
                {'check': 'row_count', 'min': 1},
                {'check': 'not_null', 'columns': ['restaurant_day_id', 'day', 'restaurant_id', 'orders', 'revenue']},
                {'check': 'unique', 'columns': ['restaurant_day_id']},
                {'check': 'range', 'column': 'orders', 'min': 1},
                {'check': 'range', 'column': 'revenue', 'min': 0}
            ]
        }
    }
) }}
```

Each key does one job.

| Key | What it does |
|---|---|
| `ldp_publish` | Writes the model to an Iceberg table when the build passes, so that it is versioned and every tool can read it |
| `ldp_namespace` | Puts the table in the namespace `gold`, which makes it a gold table |
| `owner` | Says who answers questions about the table |
| `grain` | Says what one row is |
| `tags` | Free labels that the catalog shows |
| `ldp_quality` | The gate. These checks run on the rows before the table is written, and with `on_failure` set to `fail` a table that breaks one is not written |

The meaning of the table and of each column is written in `schema.yml`, next to the tests of the model, because dbt already reads that file.

```yaml
  - name: gold_daily_sales
    description: >
      Orders, revenue and the average ticket for each restaurant on each day it had an order. The average ticket is the
      revenue divided by the orders.
    columns:
      - name: restaurant_day_id
        description: The restaurant and the day, as restaurant_id:YYYY-MM-DD.
        data_tests: [unique, not_null]
      - name: day
        description: The day.
```

Build the project. dbt runs the twelve models and the 41 data tests, and when every one of them passes LDP publishes the five models that carry the mark. Each gold table goes through its gate first.

```bash
ldp dbt build dbt
```

<!-- docs-runner: output -->
```text
...
... Found 12 models, 41 data tests, 6 sources, ...
...
... Done. PASS=53 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=53
ldp: published silver.fct_orders: upsert, 0 -> 883 rows, snapshot ...
ldp: published gold.churn_by_month: overwrite, 0 -> 7 rows, snapshot ...
  Data quality: 4 of 4 checks passed
...
ldp: published gold.gold_daily_sales: overwrite, 0 -> 643 rows, snapshot ...
  Data quality: 5 of 5 checks passed
...
ldp: published gold.gold_menu_item_weekly: overwrite, 0 -> 953 rows, snapshot ...
  Data quality: 5 of 5 checks passed
...
ldp: published gold.gold_restaurant_scorecard: overwrite, 0 -> 6 rows, snapshot ...
  Data quality: 5 of 5 checks passed
...
```

LDP also wrote a config for each published table into `config`, because `dbt/ldp_dbt.json` says so. The config is where the contract of the table lives. Read it back.

```python
import json

docs = json.load(open("config/gold_daily_sales.json"))["metadata"]["docs"]
print(json.dumps({key: docs[key] for key in ("layer", "owner", "grain", "tags")}, indent=2))
print(len(docs["columns"]), "columns described")
```

<!-- docs-runner: output -->
```text
{
  "layer": "gold",
  "owner": "restaurant operations",
  "grain": "one row for each restaurant on each day it had an order",
  "tags": [
    "sales"
  ]
}
9 columns described
```

LDP takes the description of the model and of each column, the tags, the owner and the grain from the dbt project, and it names the layer from the namespace. The config is rewritten by every build, so the model and `schema.yml` are what you edit. A build also writes the contract into the Iceberg table itself, as its comment, its owner, its layer, its grain and the description of each column, in one commit that adds no snapshot. So a tool that reads the table, with no config at all, finds what it means. `ldp gold sync` writes it again when something other than a build changed it, and `--dry-run` says what would change.

```bash
ldp gold sync config/gold_daily_sales.json --dry-run
```

<!-- docs-runner: output -->
```text
gold_daily_sales: gold.gold_daily_sales already has its contract, with 9 columns described. Nothing was written.
```

The contract is also a gate. A gold table whose column has no description is refused, and a table that is described but has no owner or no grain is refused too. To see it, copy the configs to a folder that you throw away, and delete the description of the `city` column from the copy.

```bash
cp -R config trial
sed -i.bak '/"city": "The city the restaurant is in\./d' trial/gold_daily_sales.json
rm trial/gold_daily_sales.json.bak
ldp gold sync trial/gold_daily_sales.json --dry-run 2>&1 || echo "exit status $?"
rm -r trial
```

<!-- docs-runner: output -->
```text
ldp: error: gold.gold_daily_sales does not meet its gold contract, and nothing was written:
  - 1 column written without a description in metadata.docs.columns: city
exit status 3
```

It is exit status 3 again, because the cure is a person writing a sentence, and a retry cannot do that. A run of a gold config stops in the same way before it writes the table. `ldp dbt build` checks the contract too. It publishes nothing when a gold model has no description, owner or grain, and it stops at the table that has a column with no description, after the models before it. Either way a gold table cannot reach a reader with a column that nobody described.

### Without dbt

dbt is the usual way to build a gold table, and it is not the only one. A gold table can also be one SQL file and one small config. The `DUCKDB` source takes the SQL in `query_file`, and `inputs` lists the configs of the tables that the SQL reads. The robot-episode example builds two gold tables this way, and `make demo-robotics` runs it. This is the source of one of them.

```json
    "source": {
      "name": "gold_robot_daily",
      "format": "DUCKDB",
      "query_file": "models/gold_robot_daily.sql",
      "inputs": [
        "gold_episode_stats.json"
      ]
    },
```

The SQL file is a plain `SELECT` over the tables that `inputs` names.

```sql
SELECT
    robot_id,
    CAST(start_ts AS DATE) AS recorded_on,
    count(*) AS episodes,
    count(*) FILTER (WHERE success) AS successes,
    avg(CASE WHEN success THEN 1.0 ELSE 0.0 END) AS success_rate,
    CAST(sum(frames_recorded) AS BIGINT) AS frames,
    CAST(sum(dropped_frames) AS BIGINT) AS dropped_frames,
    sum(dropped_frames) / sum(frames_recorded) AS drop_rate,
    max(max_skew_ms) AS max_skew_ms
FROM gold_episode_stats
GROUP BY ALL
ORDER BY robot_id, recorded_on;
```

The same config has a `quality` block for the gate and a `docs` block for the contract, in the same keys as the dbt route writes. `ldp gold build`, in the next part, builds a table like this one, and only checks a table that dbt or something else built.

## Part 4. Check the tables and cut a release

A gold table is rebuilt on its own schedule, so two tables that are read at the same moment can come from different builds. A release fixes that for readers. It names the snapshot of every gold table, and a reader that reads the release sees all the tables as one build left them. `ldp gold build` checks every gold table of a folder and cuts a release when all of them pass. dbt built these tables, so the command only checks them, and it says so in the status of each line.

```bash
ldp gold build config
```

<!-- docs-runner: output -->
```text
churn_by_month             checked         7 rows  snapshot ...  4 of 4 checks passed
gold_daily_sales           checked       643 rows  snapshot ...  5 of 5 checks passed
gold_menu_item_weekly      checked       953 rows  snapshot ...  5 of 5 checks passed
gold_restaurant_scorecard  checked         6 rows  snapshot ...  5 of 5 checks passed
gold build ...: 0 built, 4 checked, 0 blocked, 0 failed, 0 not run in ...s
metrics: 3 semantic models and 6 metrics fit the gold tables
release r1 cut with 4 tables and 1609 rows in gold
```

The checks ran again, on the snapshot that each table has now. The line about metrics comes from `config/metrics.json`, which Part 6 reads. A build checks that every table and column the metrics name still exists, and it cuts no release if one does not. Run the same command again. Nothing changed, so there is nothing to release.

```bash
ldp gold build config
```

<!-- docs-runner: output -->
```text
...
no release cut, because the tables are at the snapshots of r1. Readers see r1.
```

The nightly workflow runs `ldp dbt build` again. Do the same, and the gold tables are written again. The rows are the same, but each table has a new snapshot, so the live tables are now ahead of the release.

```bash
ldp dbt build dbt > /dev/null
ldp gold list config
```

<!-- docs-runner: output -->
```text
4 gold tables in config
table ... release
...
gold.gold_daily_sales ... published ... r1 (behind live)
...
```

The last column of `ldp gold list` says `r1 (behind live)`. Readers of the release still see the first build, and they will until the next release is cut. A reader that wants the newest tables asks for them with `--live`, and one that wants an old release asks for it by name.

```bash
ldp query config --release r1 "SELECT count(*) AS days FROM gold.gold_daily_sales"
ldp query config --live "SELECT count(*) AS days FROM gold.gold_daily_sales"
```

<!-- docs-runner: output -->
```text
days
----
 643
(1 row)
...
days
----
 643
(1 row)
```

Both give the same count here, because the build ran on the same files. When the data changes, `--release r1` goes on giving the old numbers. Cut the next release.

```bash
ldp gold build config
ldp gold releases config
```

<!-- docs-runner: output -->
```text
...
release r2 cut with 4 tables and 1609 rows in gold
Releases of gold in ..., newest first. r2 is current.
release  cut_utc ...
-------  ...
r2 ...       4  1609
r1 ...       4  1609
(2 rows)
```

`ldp gold releases config --show r1` lists the tables of one release with the snapshot, the dataset version that pins it and the run that built it. A pinned snapshot is never expired, so `ldp maintain` keeps every released snapshot, and a release can be read for as long as you keep the warehouse.

Now the part that matters most. A release is cut only when every table passes its gate. To see a gate hold one back, tighten the check of the revenue of the daily sales in a throwaway copy of the configs, so that it asks for more than the table has.

```bash
cp -R config trial
sed -i.bak 's/"min": 0$/"min": 1000000/' trial/gold_daily_sales.json
rm trial/gold_daily_sales.json.bak
ldp gold build trial 2>&1 || echo "exit status $?"
rm -r trial
```

<!-- docs-runner: output -->
```text
churn_by_month             checked         7 rows  snapshot ...  4 of 4 checks passed
gold_daily_sales           blocked       643 rows  snapshot ...  1 of 5 checks failed (range(revenue))
gold_menu_item_weekly      not run              -  -
gold_restaurant_scorecard  not run              -  -
gold build ...: 0 built, 1 checked, 1 blocked, 0 failed, 2 not run in ...s
no release cut, because gold_daily_sales was blocked. Readers still see r2.
ldp: gold_daily_sales blocked
  blocked by failed quality checks on the current snapshot, and nothing was written:
    - range(revenue): 643 of 643 rows violate revenue >= 1000000: 643 below min, 0 above max
exit status 3
```

The build stopped at the first table that failed. The tables after it were not run, no release was cut, and readers still see `r2`. It is exit status 3, so a scheduler tells a person and does not retry. This is why a report that reads the release never sees a half-built layer, and why a failure of the night is a message and not a wrong number.

A workflow runs the same build as a task. The workflow of the [orchestration guide](orchestration.md) has a `gold` task that checks the gold tables and cuts a release for each night, after the dbt task.

## Part 5. Find a table

A person who wants a number first needs to know which table has it. `ldp gold list` is the shelf. It has a row for each gold table with what it means, who owns it, what one row is, how many rows it has, when it last changed, how its last run and its gate ended, and which release it is in.

```bash
ldp gold list config
```

<!-- docs-runner: output -->
```text
4 gold tables in config
table ... release
...
gold.churn_by_month ... customer insights ... one row for each month ... published ... 4 of 4 passed ... r2
gold.gold_daily_sales ... restaurant operations ... published ... 5 of 5 passed ... r2
gold.gold_menu_item_weekly ... kitchen operations ... published ... 5 of 5 passed ... r2
gold.gold_restaurant_scorecard ... restaurant operations ... published ... 5 of 5 passed ... r2
(4 rows)
```

The descriptions are cut to fit the line. `--all-layers` lists the bronze and silver tables too, with `-` or `no description` where they have no contract, and `--json` prints the same as JSON for a script. `--strict` exits with 1 when a gold table has no description, owner or grain, or has a column without a description, so it can sit in CI.

```bash
ldp gold list config --strict > /dev/null && echo "every gold table has its contract"
```

<!-- docs-runner: output -->
```text
every gold table has its contract
```

`ldp gold show` is the page of one table. It has the description, the owner, the grain and the key, every column with its type and its meaning, the metrics defined on the table, its last runs, the releases that include it, a few sample rows and a line that reads it.

```bash
ldp gold show config gold_daily_sales --no-samples
```

<!-- docs-runner: output -->
```text
gold.gold_daily_sales  (gold)
  Orders, revenue and the average ticket for each restaurant on each day it had an order. The average ticket is the revenue divided by the orders.
  Owner ... restaurant operations
  Grain ... one row for each restaurant on each day it had an order
  Tags ... sales
  Config ... gold_daily_sales.json
  Rows ... 643
...
  Quality ... 5 of 5 passed
  Release ... r2

Columns (9)
...
  restaurant_day_id  string  The restaurant and the day, as restaurant_id:YYYY-MM-DD.
...
Metrics
...
  avg_ticket ... currency
...
Releases that include it (2)
...
  r2 (current) ... 643 ... checked  yes
  r1 ... 643 ... checked  no
...
Read it
  ldp query config "SELECT * FROM gold.gold_daily_sales LIMIT 10"
```

`--no-samples` leaves the sample rows out, which a table whose rows must not be shown to everyone needs. A table that carries personal data should have them left out of every catalog that leaves the team.

The same facts can be written to one file. `ldp gold docs` writes a page that has no dependency on the network, and `--format md` writes Markdown for a MkDocs site or a wiki.

```bash
mkdir out
ldp gold docs config --out out/catalog.html
ldp gold docs config --format md --no-samples --out out/catalog.md
```

<!-- docs-runner: output -->
```text
wrote out/catalog.html, 4 tables, ... KB. Open it as a page. It needs no network.
wrote out/catalog.md, 4 tables, ... KB. Open it as a Markdown file.
```

`out/catalog.html` is one file with its style and its search script inside it, so it opens from a folder, from an e-mail and from a laptop that is offline. Its search box filters the tables as you type, by name, owner, column or metric. The section for people who are not engineers below shows how a person uses it.

## Part 6. Ask for a number

The average ticket of the story is where two people got two numbers. In July, the average of the daily average tickets and the revenue divided by the orders are not the same number.

```bash
ldp query config "SELECT round(avg(avg_ticket), 2) AS average_of_daily_averages, round(sum(revenue) / sum(orders), 2) AS avg_ticket FROM gold.gold_daily_sales WHERE day >= DATE '2026-07-01' AND day < DATE '2026-08-01'"
```

<!-- docs-runner: output -->
```text
average_of_daily_averages  avg_ticket
-------------------------  ----------
                   817.56      841.84
(1 row)
```

A day with one order counts as much as a day with ten when the daily averages are averaged, so the first number is not the answer to the question. A metric says which is meant, once, and everyone who asks for it gets the same number. Metrics live in a file next to the configs, `config/metrics.json`. It has semantic models, which say which table a number comes from, which columns are dimensions that it can be split by, which column is the time and at which grains it may be reported, and which measures can be added up. This is the semantic model of the daily sales.

```json
    {
      "name": "daily_sales",
      "table": "gold.gold_daily_sales",
      "description": "One row for each restaurant on each day it had an order.",
      "time_dimension": {"column": "day", "grains": ["day", "week", "month", "quarter", "year"]},
      "dimensions": [
        {"name": "restaurant", "column": "restaurant_name", "description": "The restaurant."},
        {"name": "city", "description": "The city the restaurant is in."},
        {"name": "cuisine", "description": "The kind of food the restaurant serves."}
      ],
      "measures": [
        {"name": "revenue_sum", "agg": "sum", "expr": "revenue", "description": "What the orders cost."},
        {"name": "order_sum", "agg": "sum", "expr": "orders", "description": "How many orders there were."}
      ]
    },
```

A metric is made of the measures. A ratio adds up its two measures and divides, so the average ticket of a month is its revenue over its orders.

```json
    {
      "name": "avg_ticket",
      "type": "ratio",
      "numerator": "revenue_sum",
      "denominator": "order_sum",
      "format": "currency",
      "description": "What one order costs on average, which is the revenue over the orders, and not the average of the daily averages."
    },
```

`ldp metrics list` shows what can be asked for, and `ldp metrics validate` checks the file against the tables, as `ldp gold build` does before it cuts a release.

```bash
ldp metrics list config
ldp metrics validate config
```

<!-- docs-runner: output -->
```text
metric ... grains
...
revenue ... restaurant operations ... currency ... gold.gold_daily_sales ... day, week, month, quarter, year
...
churn_rate ... customer insights ... percent ... gold.churn_by_month ... month
...
(6 rows)
metrics.json: 3 semantic models and 6 metrics fit the gold tables of ...
```

`ldp metrics query` computes them. Ask for the revenue, the orders and the average ticket of each cuisine for each month since July.

```bash
ldp metrics query config revenue,orders,avg_ticket --by cuisine --grain month --from 2026-07-01
```

<!-- docs-runner: output -->
```text
month ... cuisine ... revenue  orders  avg_ticket
...
2026-07-01  Bengali       13,290.00      15      886.00
...
(12 rows)
read release r2, cut ... UTC
```

The numbers come from the current release, and the last line says which one. `--release` and `--live` choose another, as for `ldp query`. `--filter city=Mumbai` keeps the rows of one dimension value, and the value is never put into the SQL text. It is bound as a parameter, so a value that looks like SQL is only a value. `--sql` prints the SQL that would run and runs nothing, which is how you check what a number means.

```bash
ldp metrics query config avg_ticket --by cuisine --grain month --from 2026-07-01 --sql
```

<!-- docs-runner: output -->
```text
SELECT
  CAST(date_trunc('month', "day") AS DATE) AS "month",
  "cuisine",
  sum(revenue) / NULLIF(sum(orders), 0) AS "avg_ticket"
FROM "gold"."gold_daily_sales"
WHERE "day" >= $1
GROUP BY 1, 2
ORDER BY 1, 2
-- $1 = DATE '2026-07-01'
```

A metric also refuses what it cannot answer correctly. The churn is a share of the customers of the month before, so adding up months would count a customer once for each month. Its time dimension allows only `month`, and a question by quarter is refused with the grains that are allowed. A misspelled metric gets a hint.

```bash
ldp metrics query config churn_rate --grain quarter 2>&1 || echo "exit status $?"
ldp metrics query config revenu 2>&1 || echo "exit status $?"
```

<!-- docs-runner: output -->
```text
ldp: error: 'churn_rate' cannot be reported by quarter. The time dimension of customer_churn allows the grains ['month']
exit status 1
ldp: error: unknown metric 'revenu'. Did you mean 'revenue'? The metrics are ['revenue', 'orders', 'avg_ticket', 'avg_rating', 'churn_rate', 'active_customers']
exit status 1
```

## Part 7. Give the tables to people

Most people who need a table open Excel, and some use a BI tool. LDP writes three kinds of file for them, and each is written from the current release, so a file agrees with every other reader. The Excel file of one table has the rows on its first sheet, under a bold header that stays in view and has a filter. A second sheet, `about`, has what the catalog knows about the table.

```bash
ldp gold export config gold_restaurant_scorecard out/scorecard.xlsx
```

<!-- docs-runner: output -->
```text
wrote out/scorecard.xlsx, 6 rows of gold.gold_restaurant_scorecard, read from release r2, cut ... UTC, snapshot ...
```

Open the workbook the way Excel does, with the two sheets.

```python
import openpyxl

book = openpyxl.load_workbook("out/scorecard.xlsx")
print(book.sheetnames)
header, *data = book["gold_restaurant_scorecard"].values
print(header)
for row in sorted(data):
    if row[2] == "Mumbai":
        print(row)
for label, value, _ in book["about"].values:
    if label in ("Table", "Owner", "Grain", "Release", "Rows"):
        print(label, "|", value)
```

<!-- docs-runner: output -->
```text
['gold_restaurant_scorecard', 'about']
('restaurant_id', 'restaurant_name', 'city', 'cuisine', 'as_of', 'orders_30d', 'revenue_30d', 'review_count', 'rating_sum', 'avg_rating', 'city_rank')
('R02', 'Spice Route', 'Mumbai', 'North Indian', datetime.datetime(2026, 8, 7, 0, 0), 15, 14570, 22, 74, 3.36, 2)
('R04', 'Brew & Bun', 'Mumbai', 'Cafe', datetime.datetime(2026, 8, 7, 0, 0), 14, 15390, 20, 76, 3.8, 1)
Table | gold.gold_restaurant_scorecard
Owner | restaurant operations
Grain | one row for each restaurant
Release | r2
Rows | 6
```

The `about` sheet also has the description, the snapshot, the build time and a line for each column with its type in plain words and its meaning, so a table that is forwarded by e-mail still says what it is. A sheet holds 1,048,575 rows at most, so a bigger table is exported as CSV, Parquet or JSONL, by the end of the file name.

```bash
ldp gold export config gold_daily_sales out/daily_sales.csv
head -1 out/daily_sales.csv
```

<!-- docs-runner: output -->
```text
wrote out/daily_sales.csv, 643 rows of gold.gold_daily_sales, read from release r2, cut ... UTC, snapshot ...
"restaurant_day_id","day","restaurant_id","restaurant_name","city","cuisine","orders","revenue","avg_ticket"
```

The third file is for people who want to ask their own questions. `ldp gold duckdb` writes the whole gold layer as one DuckDB file. It has a copy of each gold table of the release, a comment on each table and column from its contract, a table `_catalog` with what each table is and who owns it, and a table `_metrics` with the definition of each metric. It is a copy, not a view, so it needs no extension and no access to the lake, and it can be put on a shared drive.

```bash
ldp gold duckdb config --out out/gold.duckdb
```

<!-- docs-runner: output -->
```text
wrote out/gold.duckdb, 4 tables and 6 metrics, from release r2, cut ... UTC, ... KB
```

Ask the file which tables it holds and who owns them, how much each city sold, and how the average ticket is defined.

```python
import duckdb

with duckdb.connect("out/gold.duckdb", read_only=True) as file:
    for row in file.execute("SELECT table_name, owner, release FROM _catalog ORDER BY table_name").fetchall():
        print(row)
    sql = ("SELECT city, round(sum(revenue), 2), sum(orders) FROM gold_daily_sales "
           "GROUP BY city ORDER BY sum(revenue) DESC")
    for row in file.execute(sql).fetchall():
        print(row)
    print(file.execute("SELECT definition FROM _metrics WHERE metric = 'avg_ticket'").fetchone()[0])
```

<!-- docs-runner: output -->
```text
('churn_by_month', 'customer insights', 'r2')
('gold_daily_sales', 'restaurant operations', 'r2')
('gold_menu_item_weekly', 'kitchen operations', 'r2')
('gold_restaurant_scorecard', 'restaurant operations', 'r2')
('Mumbai', 263900.0, 293)
('Hyderabad', 196800.0, 185)
('Kolkata', 134790.0, 148)
('Delhi', 62340.0, 167)
('Bengaluru', 30100.0, 90)
sum(revenue) / NULLIF(sum(orders), 0)
```

`ldp gold build config --duckdb out/gold.duckdb` writes the same file right after every release, so the file on the shared drive always matches the release that readers see. A `{release}` in the file name gives every release a file of its own.

## Part 8. Let an agent read the gold layer

An AI agent that can read the whole lake finds `raw.orders` as readily as `gold.gold_daily_sales`, and it has no way to know which of them to trust. `ldp mcp --layer gold` serves only the gold tables, which are the ones that have an owner, a meaning and a gate, and nothing upstream of them. It also serves the metrics, so the agent asks for the average ticket by name and does not write the division. Run on its own, the server waits for a client, so `< /dev/null` closes its input and it stops after it says what it serves.

```bash
ldp mcp --config config --layer gold < /dev/null 2>&1
```

<!-- docs-runner: output -->
```text
ldp mcp: serving 4 tables of the gold layer, 6 metrics over stdio (gold.churn_by_month, gold.gold_daily_sales, gold.gold_menu_item_weekly, gold.gold_restaurant_scorecard); audit log ...
```

An agent client starts the same command and talks to it over stdio. This client does what an agent would do first. It asks which tables there are and what they mean, and then it asks for a metric.

```python
import anyio
from mcp import Client, StdioServerParameters


async def ask():
    server = StdioServerParameters(command="ldp", args=["mcp", "--config", "config", "--layer", "gold"])
    async with Client(server) as client:
        listed = await client.call_tool("list_tables", {})
        for table in listed.structured_content["tables"]:
            print(table["table"], "|", table["owner"], "|", table["grain"])
        answer = await client.call_tool("query_metric", {"metrics": ["revenue", "avg_ticket"], "by": ["city"]})
        print([column["name"] for column in answer.structured_content["columns"]])
        for row in answer.structured_content["rows"]:
            print([round(value, 2) if isinstance(value, float) else value for value in row])
        print("read", answer.structured_content["source"])


anyio.run(ask)
```

<!-- docs-runner: output -->
```text
gold.churn_by_month | customer insights | one row for each month
gold.gold_daily_sales | restaurant operations | one row for each restaurant on each day it had an order
gold.gold_menu_item_weekly | kitchen operations | one row for each menu item in each week in which it was ordered
gold.gold_restaurant_scorecard | restaurant operations | one row for each restaurant
['city', 'revenue', 'avg_ticket']
['Bengaluru', 30100.0, 334.44]
['Delhi', 62340.0, 373.29]
['Hyderabad', 196800.0, 1063.78]
['Kolkata', 134790.0, 910.74]
['Mumbai', 263900.0, 900.68]
read release r2, cut ... UTC
```

The server is read-only, runs the SQL it is given in a locked-down DuckDB, binds the values of a metric as parameters, and writes an audit record of every call. The [guide for agents](agents.md) has the tools, the limits and the audit log.

## For people who are not engineers

This section is written to be sent on its own. It is for someone who is handed a gold layer and wants to use it without learning to code. Someone on the data team gives you a folder with `catalog.html`, `gold.duckdb` and perhaps some Excel files. They are made by `ldp gold docs`, `ldp gold duckdb` and `ldp gold export`, which the parts above ran.

**Open the catalog.** Double-click `catalog.html`. It opens in your browser, and it works with no network, so it is the same from a train and from an e-mail attachment. It has one section for each table the team has published. At the top is a search box.

**Find a table.** Type a word that you expect, such as `revenue`, `rating`, `churn` or the name of a person or a team, and the page hides every table that does not have all your words in its name, owner, columns or metrics. It says how many of the tables are shown. Each section tells you what you need to decide whether the table is the right one.

| What the section says | What it means for you |
|---|---|
| The line under the name | What the table is, in the words of its owner |
| Owner | Who to ask. A gold table always has one |
| Grain | What one row is. If you want sales for each restaurant and day, you want a table that says so |
| Rows, refreshed, quality and release | How big it is, when it last changed, whether its checks passed, and which release of the whole layer it belongs to. Two tables of the same release agree with each other |
| Columns | Every column, its type and what it holds |
| Metrics | The numbers that are defined once for this table. Use these, and not your own sum, when a number goes into a report |
| Sample | A few real rows, so that you can see the table |

**Get the rows into Excel.** The page has a few sample rows and not the whole table, and it has no download button. Ask the person who runs the platform for the table by name, such as `gold.gold_restaurant_scorecard`, or run one command yourself if `ldp` is installed on your computer, with the folder of configs, the table and the file you want.

```text
ldp gold export CONFIG_FOLDER gold.gold_restaurant_scorecard scorecard.xlsx
```

Open `scorecard.xlsx` in Excel. The first sheet has the rows, with a header that stays in view and a filter on each column. The second sheet is called `about`, and it repeats what the catalog says, with the release and the time the table was built, so it stays with the file when it is forwarded. A table with more than 1,048,575 rows does not fit a sheet, so ask for a CSV file instead.

**Or open the DuckDB file.** `gold.duckdb` holds every gold table of the release, so you can ask your own questions in SQL, or point a tool at it. DuckDB is a free program that opens it, and it needs no account. Install it from [duckdb.org](https://duckdb.org/install/), and then run `duckdb -ui gold.duckdb` to open its notebook in your browser. The notebook fetches its pages from the DuckDB site, so it needs a network. `duckdb gold.duckdb` opens the plain command line, which needs none. Start with these two questions.

```sql
SELECT table_name, description, owner, release FROM _catalog;
SELECT metric, description, definition FROM _metrics;
```

The first lists the tables and who owns them, and the second lists the numbers that are defined once and how each one is worked out. The tables have the names that the catalog shows, without `gold.` in front, and a comment on each table and each column says what it is. A BI tool that reads DuckDB files opens it in the same way.

**Know what you have.** The file and the Excel sheet are a copy of one release, and they say which one on the `about` sheet and in `_catalog`. They do not change after that, so two people who got the same release have the same numbers. When the team cuts a new release, ask for a new file, and the release name tells you which one you are holding.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `ldp run` stops with `data quality checks failed` and exit status 3 | A batch broke a rule of its config, and the message names each check. Nothing was written | Fix the file, or ask the source to send it again, and run again. A retry of the same file fails again |
| `ldp dbt build` stops and names a gold model | The model has no description, owner or grain, or one of its columns has no description | Write it in the `meta` of the model or in `schema.yml`, and build again |
| `ldp gold sync` or a run exits with 3 and says `does not meet its gold contract` | A column of a gold table has no description, or a description names a column that the table does not have | Fix the description in the model, and build again |
| `ldp gold build` says `blocked` and exits with 3 | A check of a gold table failed on its current snapshot, and the lines after it name the check | Fix the model or the data and build again. Readers keep the last release in the meantime |
| `no release cut, because the tables are at the snapshots of r2` | Nothing changed since the last release | Nothing. Build the tables again, and cut a release after |
| `ldp gold list` shows `r2 (behind live)` | A table was written after the release | Run `ldp gold build` to cut the next release when you want readers to see it |
| `ldp metrics query` says that a metric cannot be reported by a grain | The time dimension of the metric does not allow it | Use one of the grains in the message |
| `ldp gold build` says `the metrics do not fit the gold tables`, cuts no release and exits with 1 | A table or a column that `metrics.json` names was renamed or removed. The message names the metric, the measure and the column | Fix `metrics.json`, and run `ldp metrics validate config` to check it again |
| `ldp gold export` says `an Excel sheet holds 1048575 below its header` | The table has more rows than a sheet can hold | Export CSV or Parquet, by the end of the file name |
| `ldp mcp` says `The MCP server needs the mcp package` | The MCP SDK is not installed | `pip install -e ".[mcp]"` from the clone |
| You want to start again | | Delete `ldp_demo/gold`, and begin again at Make the data |

## What to read next

- [The v0.1.4 contract](design/v0_1_4.md) has the rules behind this chapter. They are the keys of a contract, the order of a gold build, how a release is cut and read, the metrics file, and the files for people.
- [`examples/restaurant`](https://github.com/tusharchou/local-data-platform/tree/main/examples/restaurant) has the generator, the configs, the dbt project, the metrics file and the `run.py` walkthrough that this chapter did by hand. `make demo-gold` runs it in about 10 seconds, and it needs the `dbt` and `excel` extras.
- [Orchestrating with workflows, backfills and a scheduler](orchestration.md) runs the loads, the dbt build and the `gold` task of this chapter every night, and cuts a release for each night.
- [MCP server for agents](agents.md) has the tools of `ldp mcp`, its limits and its audit log.
- [Warehousing with Excel, CSV and dbt](warehousing.md) is the chapter before the orchestration one, and builds the dbt project that this chapter publishes.
- [Recipes](recipes.md) has more small tasks, such as the monthly churn of an orders table in SQL.
