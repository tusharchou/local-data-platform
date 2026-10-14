# Local Data Platform

**local-data-platform** (`ldp`) is a Python library for running an Apache Iceberg lakehouse on your
laptop, and for taking the same pipelines to a shared catalog and object storage when you need to.
You describe a dataset in a JSON config, and `ldp` loads it into an Iceberg table, runs data quality
checks before the write, and lets you query the table with DuckDB SQL and read earlier snapshots.
The default setup needs no server and no cloud account.

It's for learning how a lakehouse works, for building and testing pipelines locally before you pay
for cloud infrastructure, and for giving agents and notebooks safe, reproducible access to the
tables you build.

> **Status.** This is version 0.1.2, the Warehousing milestone. It adds `ldp dbt`, which runs a dbt
> project over your Iceberg tables and publishes the models you mark back to Iceberg (see
> [Transform with dbt](#transform-with-dbt)), the `DUCKDB` and `XLSX` source formats, time travel and
> folders of tables in `ldp query`, retries for concurrent appends, and a restaurant example with a
> [guide](docs/warehousing.md). Version 0.1.1 was the first release on PyPI since 0.1.0. It carries
> the hardening work (contract in [`docs/design/v0_1_1.md`](docs/design/v0_1_1.md)) and the platform
> work ("multi-writer, any catalog, object storage, agent-ready", contract in
> [`docs/design/v0_1_1_platform.md`](docs/design/v0_1_1_platform.md)), which works from Python and
> through the [platform `ldp` commands](#platform-commands). The version number follows the
> milestones in the [roadmap](docs/roadmap.md).

## Quickstart

`ldp` needs Python 3.12 or newer. Install it from PyPI, preferably in a virtual environment:

```bash
pip install "local-data-platform[duckdb]>=0.1.2"

ldp demo --workdir ldp_demo
```

To work on the library itself, install it from a clone of this repo. `make install` creates
`.venv` and installs the package in editable mode with the `dev` and `docs` extras:

```bash
git clone https://github.com/tusharchou/local-data-platform.git
cd local-data-platform
make install
source .venv/bin/activate

ldp demo --workdir ldp_demo
```

`ldp demo` generates 1,000 synthetic taxi rides (always the same ones), then shows each feature
using only the public API:

1. It loads a config from JSON.
2. It appends twice, so you can see the duplicates, then overwrites twice to show that the load is
   idempotent.
3. It upserts on a key column.
4. It writes a day-partitioned table.
5. It runs quality checks that pass, then shows a bad batch being blocked before the write.
6. It queries the table with DuckDB SQL.
7. It reads an earlier snapshot (time travel).
8. It exports the table back to CSV.

Everything it writes goes into the `--workdir` folder, so you can delete it afterwards. The
[quickstart guide](docs/quickstart.md) explains each step and how to write your own config.

More demos, each one a `make` target (see [Development](#development)):

| Target | What it shows |
|---|---|
| `make demo-robotics` | Humanoid-robot episodes and sensor frames in bronze, silver and gold Iceberg tables, temporal quality checks, a pinned training split and DuckDB research queries |
| `make demo-agent` | The read-only MCP server on the demo table, driven by a scripted agent client |
| `make demo-warehouse` | A restaurant chain from Excel and CSV exports to dbt-tested Iceberg tables with monthly customer churn, a late batch and time travel (needs the `dbt` and `excel` extras) |
| `make demo-spark` | The Scala Spark job reading the demo table and writing an aggregate that pyiceberg reads back (needs scala-cli) |
| `make demo-rest` | The demo config against an Iceberg REST catalog started locally on scala-cli's JVM: `ldp catalog test`, two runs, the snapshots and a query (needs scala-cli) |
| `make demo-all` | All of them; `demo-warehouse` is skipped when dbt isn't installed and the two JVM demos when scala-cli isn't |

### Extras

| Extra | Adds | You need it for |
|---|---|---|
| `duckdb` | DuckDB | SQL over Iceberg tables (`DuckDBEngine`, `ldp query`, the demo's SQL step) |
| `mcp` | The MCP SDK and DuckDB | The read-only MCP server for agents (`ldp mcp`) |
| `s3` | boto3 | SigV4-signed REST catalogs on AWS, such as S3 Tables. `s3://` IO itself needs nothing extra: pyarrow does it |
| `glue` | `pyiceberg[glue]` (boto3) | The AWS Glue catalog |
| `bigquery` | Google Cloud BigQuery client | BigQuery as a source |
| `spark` | PySpark | The experimental `SparkEngine`, which also needs a JDK 17+ |
| `excel` | openpyxl and defusedxml | Excel workbooks (`.xlsx`) as a source, for `ExcelToCSV` and `ExcelToIceberg` |
| `dbt` | dbt-core, the dbt-duckdb adapter and DuckDB | `ldp dbt`, which runs a dbt project over your Iceberg tables and publishes the models you mark back to Iceberg ([how it works](docs/design/warehousing.md)), and the tests marked `dbt` |
| `dev` | pytest, flake8, build, DuckDB, `moto[server]`, the MCP SDK, openpyxl | Working on the library |
| `docs` | MkDocs and plugins | Building the docs |

## Use it from Python

A config names a source, a target and optional quality checks. Paths are relative to the config
file's folder. Save this as `rides.json`, next to a `data/rides.csv` with `ride_id`, `pickup_ts`
and `fare` columns:

```json
{
  "identifier": "rides",
  "metadata": {
    "source": {"name": "rides", "format": "CSV", "path": "data/rides.csv"},
    "target": {
      "name": "rides",
      "format": "ICEBERG",
      "catalog": {"identifier": "nyc", "warehouse_path": "warehouse"},
      "write_mode": "upsert",
      "join_cols": ["ride_id"],
      "partition_by": [{"column": "pickup_ts", "transform": "day"}]
    },
    "quality": {
      "on_failure": "fail",
      "checks": [
        {"check": "not_null", "columns": ["ride_id", "pickup_ts"]},
        {"check": "unique", "columns": ["ride_id"]},
        {"check": "range", "column": "fare", "min": 0}
      ]
    }
  }
}
```

Run it, query it and time-travel:

```python
from local_data_platform import Config
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.registry import create_pipeline

config = Config.from_json("rides.json")
result = create_pipeline(config).run()      # picks CSVToIceberg from the config's formats
print(result.rows_read, result.rows_written, result.quality.passed)

table = Iceberg("rides", config.target["catalog"], base_dir=config.base_dir)

with DuckDBEngine() as duck:
    duck.register_iceberg(table, "rides")
    print(duck.query("SELECT count(*) AS n, avg(fare) AS avg_fare FROM rides").to_pylist())

first = table.snapshots()[0]["snapshot_id"]
print(table.get(snapshot_id=first).num_rows)   # the table as of its first snapshot
```

If a check fails and `on_failure` is `"fail"`, `run()` raises `DataQualityError` and writes
nothing; the failing checks are in `error.report`. With `"warn"` it logs them and writes anyway.

You can also build a pipeline in code, with your own transforms:

```python
import pyarrow.compute as pc

from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline import Pipeline
from local_data_platform.quality import NotNull, Unique

pipeline = Pipeline(
    source=CSV("rides", "data/rides.csv"),
    target=Iceberg("rides", {"identifier": "nyc", "warehouse_path": "warehouse"}, write_mode="overwrite"),
    transforms=[lambda df: df.filter(pc.field("fare") >= 0)],
    checks=[NotNull(["ride_id"]), Unique(["ride_id"])],
)
print(pipeline.run())
```

## Use it from the command line

```bash
ldp --version
ldp pipelines                                    # list the registered source/target routes
ldp run rides.json                               # run the config's pipeline
ldp run rides.json --mode overwrite              # override the config's write_mode
ldp run rides.json --window 2026-08-01/2026-08-02   # staged and exactly once: a second run for the window skips
ldp run orders.json --window yesterday              # a window by name: also last-week and last-month, in the config's time zone
ldp run rides.json --window 2026-08-01/2026-08-02 --run-id RUN_ID --attempt 2   # a retry of that run
ldp run rides.json --idempotency-key sales-2026-08-01   # a key of your own, for an orchestrator
ldp run rides.json --window 2026-08-01/2026-08-02 --json   # the result as JSON on stdout, for scripts
ldp snapshots rides.json                         # list the target table's snapshots
ldp query rides.json "SELECT count(*) FROM rides"   # SQL over the target, named after target.name
ldp query rides.json "SELECT * FROM rides" --max-rows 20   # print at most 20 rows (default 100)
ldp query rides.json "SELECT count(*) FROM rides" --snapshot 4437   # an older snapshot, from ldp snapshots
ldp query rides.json "SELECT count(*) FROM rides" --as-of "2026-07-31 23:59:59"   # the newest snapshot at or before that time (UTC)
ldp query config/ "SELECT o.id FROM raw.orders o JOIN raw.customers c ON c.id = o.id"   # a folder of configs: every table, joinable
ldp demo --workdir /tmp/ldp_demo                 # also --rows N (default 1000) and --seed N (default 42)
```

With `--window` or `--idempotency-key` a run is staged and exactly once. It prints its window, its
idempotency key, its run id and attempt, its status (`published` or `skipped_duplicate`) and the snapshot that holds
its rows, and running it again for the same window finds that snapshot and writes nothing. `ldp plan CONFIG --window W`
prints the same key, and `--idempotency-key auto` asks for it. The key is looked for in the whole history that the table
still holds, before the source is read, so a window that ran weeks ago is skipped without being read too. A retry
reuses the run id with the next attempt number,
and the new attempt fences the older ones, so a staged branch that an earlier attempt left behind never reaches `main`.
`--attempt` needs `--run-id`, and both need a window or a key. Without these options `ldp run` writes directly, as it
always did. A window is `START/END`, or `yesterday`, `last-week` (the last full Monday-to-Monday week) or `last-month`,
counted in the time zone of the config, and `ldp run` prints the window it stands for. What a window reads, and how a
config says so, is in [Windows](#windows).

Every command follows one exit-code table.

| Exit | Meaning |
|---|---|
| 0 | success, including a run that published and a run that skipped a duplicate |
| 1 | an error: a bad config, a usage error, a failed write |
| 2 | reserved for an SLA breach that `ldp monitor` reports in 0.1.5 |
| 3 | a data gate held the run back, such as a quality check that blocked a write |

A scheduler retries a run that exits with 1, because the cause may pass, and never retries one that exits with 3,
because the data has to change first. Add `-v` to see a traceback. The commands
that read a table (`snapshots`, `query`, `commits`, `maintain`, `datasets pin`, `catalog test`) fail
without creating anything when the config's catalog is a SQLite file that doesn't exist yet (a
`local` catalog, or `sql` on SQLite). `ldp run`, `ldp snapshots` and `ldp query` also work with the
other [catalog types](#catalogs-and-object-storage); they have been run against `sql` (SQLite) and
`rest` catalogs.

### Platform commands

`ldp --help` lists them, and `ldp <command> --help` shows each one's options. Each is also a Python
call:

| Command | What it does | From Python |
|---|---|---|
| `ldp catalog test SPEC` | Connect to a catalog (a spec file, or a config's catalog block) and list its namespaces and tables | `catalog.provider.create_catalog(spec).list_namespaces()` |
| `ldp schema [--kind KIND]` | Print the `ldp/v1` JSON Schema of a kind of file, the dataset config by default | `spec.json_schema()`, `spec.kind_schema(kind)` |
| `ldp workflow plan FILE [--window W] [--json]` | Check a workflow file, or a dataset config with a schedule, and every config it names, and print its tasks in the order they run with each pipeline task's idempotency key for the window | `orchestration.plan_workflow(path, window=...)` |
| `ldp workflow run FILE --window W [--json]` | Run the tasks of a workflow one at a time for a window. A failed or blocked task stops the tasks that depend on it and the rest still run, and a second run skips what published or was built; exit status 0, 1, or 3 for a data gate | `orchestration.run_workflow(path, window=...)` |
| `ldp plan CONFIG [--window WINDOW] [--json]` | Validate a config (or a folder of files, each as its kind) and print its spec hash, route, target, sinks, schedule, and the source path and idempotency key of a window | `spec.validate_spec(data)`, `spec.plan(config, window=...)` |
| `ldp runs CONFIG [--run RUN_ID] [--events]` | List the runs a config's sinks recorded | `events.summarize_runs(events.read_jsonl(path))` or `read_iceberg_events(catalog)` |
| `ldp commits CONFIG [--key KEY] [--branches]` | List the staged publishes on a table, newest first | `format.iceberg.commit.commit_log(iceberg.table())` and `staged_branches(...)` |
| `ldp datasets pin\|list\|export` | Pin, list and export dataset versions | `datasets.pin`, `list_versions`, `export` |
| `ldp maintain CONFIG [--expire DAYS] [--orphans] [--apply]` | Expire snapshots and find orphan files; a dry run unless `--apply` | `maintenance.expire_snapshots`, `find_orphans`, `remove_orphans` |
| `ldp mcp --config CONFIG ...` | Serve tables to an agent over MCP (stdio) | `python -m local_data_platform.mcp_server ...`, same options |
| `ldp dbt init\|build\|test\|docs\|exec PROJECT_DIR` | Run a dbt project over your Iceberg tables; `build` writes the models marked `ldp_publish` back to Iceberg (needs the `dbt` extra) | `dbt_layer.register_sources(...)`, `dbt_layer.publish.plan_publish(...)` and `publish(...)` |
| `ldp spark CONFIG` | Run the Scala Spark job on a config's table (needs scala-cli) | `engine.spark.ScalaSparkJob` |

## Config schema

A config has an `identifier`, optional descriptive fields (`who`, `what`, `where`, `when`, `how`)
and a `metadata` block:

| Key | What it holds |
|---|---|
| `metadata.source` | `name`, `format` (`CSV`, `PARQUET`, `ICEBERG`, `JSON`, `DUCKDB` or `XLSX`), and a `path` (local, `s3://` or `gs://`, and it may have `{window_start:FORMAT}` placeholders, see [Windows](#windows)) or, for Iceberg, a `catalog`. A `DUCKDB` source also has one of `relation` (a table or view) and `sql` (one `SELECT` query), see [A DuckDB file as a source](#a-duckdb-file-as-a-source). An `XLSX` source may have `sheet` and `header_row`, see [An Excel sheet as a source](#an-excel-sheet-as-a-source) |
| `metadata.target` | The same, plus for Iceberg `write_mode` (`append`, `overwrite` or `upsert`), `join_cols` for upserts, and `partition_by` |
| `metadata.target.catalog` | `type` (`local` by default, `sql`, `rest` or `glue`) and that type's keys; see [Catalogs](#catalogs-and-object-storage) |
| `metadata.quality` | `on_failure` (`fail` or `warn`) and a list of `checks`: `row_count`, `not_null`, `unique`, `accepted_values`, `range`, `freshness`, `schema`, and the temporal checks `monotonic`, `max_skew`, `rate_below` and `max_gap` |
| `metadata.observability` | Where run events go, for example `{"sinks": ["iceberg", "jsonl"]}`. The default sends them nowhere |
| `metadata.schedule` | When the config runs: a 5-field cron expression, or `@hourly`, `@daily`, `@weekly` or `@monthly`. Without it, a `when` of `hourly`, `daily`, `weekly` or `monthly` means the alias. Editing it does not change the spec hash |
| `metadata.window` | `"none"`, or `size` (`day`, `hour` or `15m`), an optional `column` that selects the rows of a window, and an optional `timezone` (UTC by default); see [Windows](#windows) |

A config may also say `"apiVersion": "ldp/v1"` and `"kind": "Pipeline"`. `kind` says what a JSON file is. `Pipeline`
is the default, so every config here is one, and a config has the same spec hash with and without it. `Workflow` is
the second kind. A kind is written exactly as shown, so `workflow` fails with a message that names `Workflow`, and
`Metrics`, `Report` and `Profiles` are reserved for later milestones. `ldp plan` validates each file of a folder as its
kind, and `ldp query`, `ldp mcp --config` and `ldp dbt` read the tables of the `Pipeline` files only, so a folder can
hold workflow files next to its configs. `ldp schema --kind Workflow` prints the schema of another kind.

Partition transforms are `identity`, `year`, `month`, `day`, `hour`, `bucket[N]` and
`truncate[W]`. On pyiceberg 0.10 and later, writing with any transform except `identity` needs the
`pyiceberg-core` package, which `ldp` depends on, so a normal install has it. If it's missing, the
demo's step 4 says so and partitions by `identity(pickup_date)` instead. The full schema, with an
example of every check, is in the [v0.1.1 hardening contract](docs/design/v0_1_1.md#config-schema).
`local_data_platform.spec.json_schema()` returns it as JSON Schema, and `ldp schema` prints it. The configs in
[`examples/`](examples/README.md) are real ones you can run.

### Windows

A run can be responsible for one window of time, `[start, end)`, which is what `--window` gives it. A config says how a
window picks what the run reads, and when the config runs:

```json
{
  "identifier": "daily_orders",
  "when": "daily",
  "metadata": {
    "schedule": "5 2 * * *",
    "window": {"size": "day", "column": "ordered_at", "timezone": "Europe/Paris"},
    "source": {"name": "orders", "format": "CSV", "path": "drops/orders_{window_start:%Y-%m-%d}.csv"},
    "target": {"name": "orders", "format": "ICEBERG", "write_mode": "overwrite",
               "catalog": {"identifier": "shop", "warehouse_path": "warehouse"}}
  }
}
```

- `{window_start:FORMAT}` and `{window_end:FORMAT}` in `metadata.source.path` name the file of the window, with a Python
  `strftime` FORMAT shown in the time zone of the window. A run without a window on such a path fails and names the
  placeholder.
- `window.column` keeps only the rows at or after the start of the window and before its end, before the transforms and
  the quality checks. `run.extracted` reports the rows read and the rows kept.
- With `window.column` and the write mode `overwrite`, a run replaces only the rows of its window, so one changed day can
  be redone and the other days stay. A published window is skipped when it runs again, so redo a day with a key of your
  own: `ldp run daily_orders.json --window 2026-08-02/2026-08-03 --idempotency-key orders-2026-08-02-v2`.
- A `freshness` check in a windowed run measures the age of the data at the end of the window.
- A config with a schedule that appends, and that has neither a placeholder nor a `window.column`, would load the same
  rows in every window. `ldp plan` reports that as an error and `ldp run --window` logs a warning.

The [contract](docs/design/v0_1_1.md#windows-and-schedules-013) has the full rules, including how each column type is
compared.

### Workflow files

A workflow file lists the pipelines, dbt models and maintenance that run for each window, and in what order:

```json
{
  "apiVersion": "ldp/v1",
  "kind": "Workflow",
  "identifier": "daily_orders",
  "metadata": {"schedule": "5 2 * * *", "window": {"size": "day"}},
  "tasks": [
    {"id": "orders", "pipeline": "config/orders.json"},
    {"id": "customers", "pipeline": "config/customers.json"},
    {"id": "gold", "dbt": {"project": "dbt", "select": "+daily_revenue"}, "depends_on": ["orders", "customers"]},
    {"id": "export", "pipeline": "config/export.json", "depends_on": ["gold"]},
    {"id": "tidy", "maintain": {"config": "config/orders.json", "expire": 7}, "depends_on": ["export"]}
  ]
}
```

```bash
ldp workflow plan daily_orders.json --window 2026-08-01/2026-08-02   # the tasks in run order, with each key
ldp workflow run daily_orders.json --window 2026-08-01/2026-08-02    # run them for that day
ldp runs .ldp/events.jsonl                                           # the workflow run and the run of each task
```

- Each task has an `id`, an optional `depends_on`, and exactly one kind: `pipeline` (the path of a dataset config,
  relative to the workflow file), `dbt` (a dbt project, built for the window, see below) or `maintain` (a config's Iceberg
  table and the options of `ldp maintain`, a dry run unless `apply` is true). Later milestones add kinds without changing the
  file format.
- Tasks run one at a time, in dependency order. A pipeline task runs with the window of the workflow, so its idempotency key
  is the one `ldp plan CONFIG --window` prints.
- A task that fails, or that a quality check blocks, makes the tasks that depend on it `upstream_failed`, and they do not run.
  The tasks that do not depend on it still run. The exit status is 1 when a task failed and 3 when the only problem is a
  data gate.
- Run the same window again after fixing the cause: the pipeline tasks that published are skipped by their keys, without
  reading their sources, and only the tasks that failed or did not run do their work.
- A `dbt` task does what [`ldp dbt build`](#transform-with-dbt) does, for the window: dbt gets `window_start` and
  `window_end` as variables (`'{{ var("window_start") }}'::timestamp` in a model), and the models marked `ldp_publish` are
  written to Iceberg. Failing data tests are a data gate, so the task is `blocked_quality` and the exit status is 3. Run the
  same window again and the task is skipped when its last build succeeded with the same project and no task before it may
  have written data. List the pipelines that load the tables a dbt project reads in its `depends_on`, and name the dbt task
  in the `depends_on` of every task that reads the tables it publishes.
- Every event of a run goes to `.ldp/events.jsonl` next to the workflow file, with the `workflow`, `workflow_run_id` and
  `task_id` of the task in its payload. A dataset config that has a `metadata.schedule` runs as a workflow of one task.

The [contract](docs/design/v0_1_1.md#workflow-files-013) has the full file format. `ldp schema --kind Workflow` prints its JSON
Schema.

### A DuckDB file as a source

A `DUCKDB` source reads one table, or the result of one query, from a DuckDB database file and
publishes it to Iceberg like any other source, with the same quality checks, run events and
snapshots. It is how tables built with SQL (or with dbt, through `ldp dbt`) reach the lakehouse:

```json
{
  "identifier": "churn_by_month",
  "metadata": {
    "source": {"name": "churn_by_month", "format": "DUCKDB", "path": "target/build.duckdb",
               "relation": "gold.churn_by_month"},
    "target": {"name": "churn_by_month", "format": "ICEBERG", "write_mode": "overwrite",
               "catalog": {"identifier": "gold", "warehouse_path": "warehouse"}}
  }
}
```

Use `relation` (`table`, `schema.table` or `catalog.schema.table`) or `sql`, not both. The file is
opened read-only and closed before the Iceberg write starts, and a `sql` source must be exactly one
`SELECT` or `WITH` query. It needs the `duckdb` extra. `ldp plan` reports a missing file or a
source with both or neither of `relation` and `sql`.

### An Excel sheet as a source

An `XLSX` source reads one sheet of an `.xlsx` workbook and writes it to a CSV file (`ExcelToCSV`) or an
Iceberg table (`ExcelToIceberg`), so a spreadsheet that someone downloads from a website stops being converted
by hand:

```json
{
  "identifier": "sales",
  "metadata": {
    "source": {"name": "sales", "format": "XLSX", "path": "downloads/sales.xlsx", "sheet": "Sales",
               "header_row": 1},
    "target": {"name": "sales", "format": "CSV", "path": "drive/sales.csv"}
  }
}
```

`sheet` is the sheet's name or its position from 0, and the first sheet by default. `header_row` is the row
that holds the column names, counted from 1, and the rows above it are skipped. Dates and datetimes become
timestamps, whole numbers `int64`, other numbers `float64`, TRUE and FALSE `bool`, and text `string`. A column
that mixes kinds becomes `string`, and a warning names it. A formula cell gives the value Excel saved with it,
and empty rows at the end of the sheet and empty columns at its right edge are dropped. Formatting, charts and
macros are ignored, and a merged range gives its value to its first cell and nulls to the rest. A `.xls` file, a
sheet that does not exist, and an empty or repeated header cell fail with a message that names the file and the
sheet. It needs the `excel` extra, and `ldp plan` reports a missing file or a `.xls` path. The CSV is replaced
atomically, so a folder that Google Drive syncs never holds a half-written file; see the recipe
[Excel to CSV in a Google Drive folder](docs/recipes.md#excel-to-csv-in-a-google-drive-folder).

## Catalogs and object storage

The catalog block's `type` picks the catalog. It defaults to `local`, so a catalog block without
`type` keeps working unchanged.

| `type` | Keys | What you get |
|---|---|---|
| `local` (alias `LocalIceberg`) | `identifier`, `warehouse_path` | A SQLite catalog file and a warehouse folder |
| `sql` (alias `sqlite`) | `uri` (a SQLAlchemy URI such as `sqlite:///…` or `postgresql+psycopg://…`), `warehouse`, `name`, `password_env` | pyiceberg's `SqlCatalog`. Its tables are the ones Iceberg's Java `JdbcCatalog` uses, so Spark can share it |
| `rest` | `uri`, `warehouse`, `name`, `token_env`, `credential_env`, and optional `properties` (such as `s3.endpoint`) | pyiceberg's `RestCatalog`, for any Iceberg REST catalog, such as Polaris, Nessie or Lakekeeper |
| `glue` | `name`, `warehouse`, and optional `properties` | pyiceberg's `GlueCatalog` (the `glue` extra). AWS credentials come from boto3's default chain |

What has been tested: `local` and `sql` on SQLite; `rest` against Apache Iceberg's REST catalog
test server (`tools/rest_fixture`, opt-in); `glue` against a local moto server. Postgres and
hosted REST catalogs (Polaris, S3 Tables, Unity and so on) are not tested yet.

The namespace tables live in comes from `identifier` for `local` catalogs, and from `namespace`,
falling back to `identifier`, for every other type.
`properties` are passed through to pyiceberg, and `properties_env` maps a property to the
environment variable holding its value. A REST target looks like this:

```json
"catalog": {
  "type": "rest",
  "name": "lake",
  "uri": "https://catalog.example.com/api/catalog",
  "warehouse": "analytics",
  "namespace": "rides",
  "token_env": "LAKE_TOKEN",
  "properties": {"s3.endpoint": "http://127.0.0.1:9000"}
}
```

**Secrets never go in configs.** A config names the environment variable that holds a token,
credential or password (`token_env`, `credential_env`, `password_env`, `properties_env`). A key or
property whose name contains `token`, `secret`, `password` or `credential` and holds a literal value
is rejected, and so is a password inside a `uri`; log lines mask them. A `postgresql+psycopg://`
URI also needs a SQLAlchemy driver, such as `pip install "psycopg[binary]"`.

`create_catalog` builds the same catalog a config would. For a read-only check before running
anything, use `ldp catalog test SPEC`; calling `create_catalog` on an `sql` spec creates the SQLite
file if it does not exist yet:

```python
from local_data_platform.catalog.provider import catalog_namespace, create_catalog

spec = {"type": "sql", "name": "lake", "uri": "sqlite:////tmp/lake/catalog.db", "warehouse": "file:///tmp/lake",
        "namespace": "rides"}
catalog = create_catalog(spec)
print(catalog_namespace(spec), catalog.list_namespaces())
```

`register_catalog_type(name)` is a decorator that adds your own type. `Iceberg(...,
catalog_obj=catalog)` writes through a catalog object you already have.

**Object storage.** CSV, Parquet and JSON read and write through `local_data_platform.fs`, so their
paths can be local, `file://`, `s3://` or `gs://`, all through `pyarrow.fs`. Settings come from the
environment, never from a config: S3 credentials from the AWS default chain, `AWS_ENDPOINT_URL_S3`
or `AWS_ENDPOINT_URL` for MinIO, LocalStack or moto, and `AWS_REGION`; `STORAGE_EMULATOR_HOST` for
a GCS emulator. A URI with credentials in it (`s3://key:secret@bucket/...`) is rejected. Writes are
atomic: locally a temp file is renamed into place, and on an object store the data is uploaded only
once it is complete, so readers see the old object or the new one. The S3 tests run against a local
moto server; `gs://` reads and writes have no test against a GCS emulator yet.

## Exactly-once writes

A plain `Iceberg.put(df, mode)` is **direct mode**. It keeps the semantics of the
[hardening contract](docs/design/v0_1_1.md) (appending twice gives duplicates, overwriting twice
doesn't) with three fixes:

- **One commit per write.** Adding new columns and writing the data happen in one transaction.
- **Counts from metadata.** `rows_before` and `rows_after` come from the snapshots' `total-records`,
  not from a scan.
- **A lock on local catalogs.** `overwrite` and `upsert` take an exclusive file lock,
  `<warehouse>/.ldp/locks/<namespace>.<table>.lock`, so two overwrites or upserts on one laptop
  can't interleave. Appends take no lock. An append that loses a commit race reloads the table
  and runs again with a backoff (`CommitPolicy` sets the limits), so several processes can append
  to one table at once.

Direct `overwrite` and `upsert` on a shared remote catalog are single-writer. For several
writers, pass a `CommitContext` and `put` switches to the **staged publish protocol**: it writes
to a private branch, checks the staged snapshot, then fast-forwards `main` in one catalog
compare-and-swap that asserts both `main` and the branch. The idempotency key is stored on the
snapshot as `ldp.idempotency-key` (with `ldp.run-id` and `ldp.attempt`), so re-running a key finds
the published snapshot and writes nothing:

```python
import pyarrow as pa

from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import CommitContext

table = Iceberg("fares", {"identifier": "nyc", "warehouse_path": "warehouse"}, write_mode="upsert",
                join_cols=["ride_id"])
batch = pa.table({"ride_id": [1, 2, 3], "fare": [12.5, 8.0, 30.25]})
context = CommitContext.create("fares/2024-01-01")   # a new run id; looks for the key 7 days back

first = table.put(batch, commit=context)
again = table.put(batch, commit=context)     # the same key: finds the published snapshot
print(first.attempts, again.skipped_duplicate)   # 1 True
```

If another writer moves `main` first, the attempt rebases and retries within its `CommitPolicy`; an
upsert is recomputed against the new rows, never blindly rebased. A superseded attempt is fenced by
removing its branch, so it can't publish afterwards. `WriteResult` gains `branch`,
`idempotency_key`, `attempts` and `skipped_duplicate`. What this guarantees is at most one effect
on `main` per key, which with retries means effectively once. It doesn't remove duplicates a
producer sent twice upstream. `ldp commits
CONFIG` lists the publishes on a table with their keys.
[Exactly-once writes](docs/exactly_once.md) has the protocol and its tests, including the
multi-process upsert race.

## Run events and the `_ldp` namespace

`Pipeline.run(mode=None, *, commit=None, sink=None)` emits a `RunEvent` at each step:
`run.started`, `run.extracted`, `quality.evaluated`, then one of `run.published`,
`run.skipped_duplicate`, `run.blocked_quality` or `run.failed`, and finally `run.finished`.
`PipelineResult` gains `run_id` (a uuid7), `idempotency_key` and `published_snapshot_id`.

| Sink | Writes |
|---|---|
| `NullSink` | Nothing (the default, so a run emits no events unless you choose a sink) |
| `JsonlSink(path)` | One JSON event per line |
| `OpenLineageSink(path_or_url)` | OpenLineage 1.x `RunEvent`s with schema, data-quality and output-statistics facets |
| `IcebergSink(catalog_spec, base_dir)` | Batched appends to `_ldp.runs` and `_ldp.quality_results`, both day-partitioned, in your own catalog (the MCP server also uses it for `_ldp.audit`) |
| `MultiSink` | Several of the above |

```python
from local_data_platform import Config
from local_data_platform.events import JsonlSink
from local_data_platform.pipeline.registry import create_pipeline

result = create_pipeline(Config.from_json("rides.json")).run(sink=JsonlSink("runs.jsonl"))
print(result.run_id, result.published_snapshot_id)
```

Or set the default sinks in the config with `metadata.observability`, for example
`{"sinks": ["iceberg", "jsonl"]}`; by default the `jsonl` sink writes `.ldp/events.jsonl` in the
config's folder, and `ldp run` uses these sinks too. Because `_ldp.runs` and
`_ldp.quality_results` are ordinary Iceberg tables in your catalog, you can query your run history
with any engine; `ldp runs CONFIG` lists it, and `events.read_iceberg_events` and `summarize_runs`
read it from Python. `local_data_platform.spec` gives each config a stable `spec_hash` and an
`idempotency_key(config, window)`, and validates configs against the `ldp/v1` JSON Schema
(`ldp plan CONFIG` shows all three). See
[Run events and the `_ldp` namespace](docs/observability.md).

## SQL with DuckDB

`DuckDBEngine.register_iceberg(table, alias, snapshot_id=None, row_filter=None, native=None)`
uses DuckDB's `iceberg` extension when it can: the table becomes a view over `iceberg_scan` at the
chosen snapshot, which streams and pushes filters down instead of loading the table into memory.
If the extension can't load, it falls back to the in-memory scan with a warning. It never
downloads the extension on its own, so it works offline; `DuckDBEngine(install_extensions=True)` allows
that. `native=True` or `native=False` forces one path.

```python
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.format.iceberg import Iceberg

table = Iceberg("rides", {"identifier": "nyc", "warehouse_path": "warehouse"})
with DuckDBEngine() as duck:
    duck.register_iceberg(table, "rides")                      # native iceberg_scan when available
    duck.register_iceberg(table, "rides_in_memory", native=False)
    print(duck.query("SELECT count(*) FROM rides").to_pylist())
    print(duck.snapshots("rides"))                             # from iceberg_snapshots
```

`open_lake(path)` registers every Iceberg table that some dataset configs name, so SQL can join them. `path` is a
config file, a folder of configs, or a list of them, and `ldp query` uses it when it is given a folder or more than
one config. Each table is a view named `namespace.table` and, when no other table has the same name, a view with the
bare name. A namespace of several parts, such as `a.b`, is one schema named `a.b`, so SQL quotes it: `"a.b".events`.
`snapshot_id=` or `as_of=` read one table as it was, and refuse a lake of several. Nothing is written and
nothing is downloaded, and the MCP SDK is not needed.

```python
from local_data_platform.lake import open_lake

with open_lake("config") as lake:
    lake.query("SELECT o.id, c.label FROM raw.orders o JOIN raw.customers c ON c.id = o.id")
with open_lake("config/raw.orders.json", as_of="2026-07-31 23:59:59") as lake:   # UTC unless it has an offset
    lake.query("SELECT count(*) FROM orders")
```

`attach_rest(name, uri, warehouse, token_env=None)` attaches a whole REST catalog to DuckDB.
`engine.router` estimates how many bytes a scan reads from the manifests, after partition pruning,
and `choose_engine` picks DuckDB below 50 GiB by default, and PySpark above that when it's
installed. No pipeline calls the router yet.

## Transform with dbt

`ldp dbt` runs a [dbt](https://www.getdbt.com/) project over your Iceberg tables and writes the models you mark back
to Iceberg, so what dbt builds is versioned, can be read as it was, and is there for `ldp query`, `ldp runs` and
`ldp mcp` like any other table. It needs the `dbt` extra, and the `excel` extra when a source is an Excel sheet.

```bash
pip install "local-data-platform[dbt,excel]"
ldp dbt init my_project --sources config --warehouse warehouse   # a project that builds as soon as its sources exist
ldp dbt build my_project             # register the sources, run dbt, then publish the models marked ldp_publish
ldp dbt test my_project              # register the sources and run the data tests, and publish nothing
ldp dbt docs my_project --serve      # generate the dbt docs and serve them on http://127.0.0.1:8080
ldp dbt exec my_project compile      # any other dbt command, with the sources registered
```

Before dbt runs, every Iceberg table that the configs in `--sources` describe becomes a source in a DuckDB file inside
the project, so a model reads it with `{{ source('raw', 'orders') }}`. After a build in which every model and every
data test passed, each model whose config has `meta={'ldp_publish': true}` is written to Iceberg through the
[`DUCKDB` source](#a-duckdb-file-as-a-source), with the same quality checks, run events and snapshots as any other load.
A table or view model overwrites its table, and an incremental model with a `unique_key` upserts on it. Nothing is
published when dbt fails. Everything after the project folder goes to dbt unchanged, and the options of `ldp dbt`
itself go before it.

- The [warehousing guide](docs/warehousing.md) walks through it on a restaurant chain's Excel and CSV exports, from
  the raw tables to tested models, churn, an incremental load and time travel. `make demo-warehouse` runs the same
  example offline in about 25 seconds.
- [How `ldp dbt` works](docs/design/warehousing.md) has the contract, including the meta keys and the schema rules.

## MCP server for agents

`local_data_platform.mcp_server` serves your tables to an AI agent over the Model Context Protocol,
on stdio and read-only. Install the `mcp` extra, then point your MCP client at `ldp mcp`
(`python -m local_data_platform.mcp_server` takes the same options):

```json
{
  "mcpServers": {
    "ldp": {
      "command": "/abs/path/to/.venv/bin/ldp",
      "args": ["mcp", "--config", "/abs/path/to/rides.json", "--max-rows", "500"]
    }
  }
}
```

| Tool | Returns |
|---|---|
| `list_tables()` | Every table |
| `describe_table(table)` | Schema, partition spec, row count, snapshots, the latest `_ldp` run and quality status, and freshness |
| `query(sql, max_rows)` | The result of one read-only SQL statement |
| `sample_rows(table, n)` | A sample of rows |
| `table_history(table)` | The table's snapshot history |
| `get_dataset(name)` | A pinned dataset version |

The guardrails are on by default. A query must be exactly one `SELECT` or `WITH` statement, as
DuckDB's parser sees it, so `COPY`, `ATTACH`, `INSTALL`, `SET`, `read_csv('/etc/passwd')` and
multi-statement SQL are rejected. DuckDB runs with external access off and file access limited to
the served tables' folders, and its configuration is locked once the tables are registered.
Results are capped in rows and time, `--allow` limits the tables, and every call is audited: the
tool, the SQL, row counts, duration and any error, never the result data. Records go to a JSONL file
as each call ends, and to the `_ldp.audit` Iceberg table in the first served catalog when the
server stops (`--no-iceberg-audit` turns that off).
`make demo-agent` runs a scripted client against the demo table. See
[MCP server for agents](docs/agents.md).

## Reproducible datasets and robotics

A dataset version pins a table to one snapshot, with an optional row filter and column list, and
is saved as a JSON manifest under `<warehouse>/.ldp/datasets/`. Loading it later returns exactly
the same rows, however the table has changed since:

```python
from local_data_platform import datasets
from local_data_platform.format.iceberg import Iceberg

episodes = Iceberg("gold_episode_stats", {"identifier": "robots", "warehouse_path": "warehouse"})
train = datasets.pin(episodes, "humanoid_train", row_filter="success = true",
                     selected_fields=["episode_id", "robot_id", "task", "frame_count"])
rows = datasets.load(train)                                    # the same rows at any later time
datasets.export(train, "exports/humanoid_train_v1.parquet")    # or format="jsonl"
```

Each pin also tags the snapshot (`ldp_ds_<name>_v<N>`), so snapshot expiry keeps it.
`datasets.list_versions(name, warehouse="warehouse")` and
`datasets.get_version("name@vN", warehouse="warehouse")` find versions again. From the command
line, `ldp datasets pin CONFIG NAME`, `ldp datasets list` and `ldp datasets export` do the same.

The temporal quality checks suit sensor and event data:

| `check` | Keys | Passes when |
|---|---|---|
| `monotonic` | `column`, `group_by`, `strict`, `order_by` | The column never decreases (or, with `strict`, always increases) within each group |
| `max_skew` | `column_a`, `column_b`, `max_ms` | Two timestamps on each row are at most `max_ms` apart |
| `rate_below` | `predicate_column`, `max_rate`, `group_by` | The share of rows where the boolean column is true stays below `max_rate` |
| `max_gap` | `column`, `max_ms`, `group_by` | Consecutive timestamps within each group are at most `max_ms` apart |

[`examples/robot_episodes/`](examples/robot_episodes/) puts it together: deterministic
humanoid-robot episodes and sensor frames, bronze, silver and gold tables (episodes partitioned by
`day(start_ts)` and `bucket(8, robot_id)`), quality configs for the frame-drop rate, RGB/depth sync
skew, monotonic frame timestamps and accepted tasks, a pinned training split, DuckDB research
queries and an optional Scala Spark per-robot aggregate. It runs offline in about a second with
`make demo-robotics`. See [Robot-episode datasets](docs/robotics.md).

## Table maintenance

```python
import datetime as dt

from local_data_platform import maintenance
from local_data_platform.format.iceberg import Iceberg

table = Iceberg("rides", {"identifier": "nyc", "warehouse_path": "warehouse"})
plan = maintenance.expire_snapshots(table, older_than=dt.timedelta(days=7), dry_run=True)
print(plan["expired_snapshot_ids"], plan["protected_snapshot_ids"]["idempotency_key"])
print(maintenance.find_orphans(table))          # files no metadata references; deletes nothing
report = maintenance.remove_orphans(table)      # a dry run unless dry_run=False
```

Expiry always keeps the current snapshot, branch and tag heads and `main`'s last 20 snapshots
(`retain_last`), and it never expires a snapshot that carries an idempotency key newer than the
table's idempotency horizon (7 days by default), because re-runs rely on finding it. Orphans are
files under the table's location that no metadata references and that are older than 72 hours
(`older_than_hours`), so the files of a write still in progress are left alone. `ldp maintain
CONFIG` runs both from the command line, as a dry run unless `--apply` is given. There is no
compaction yet.

## Spark (experimental)

`engine.spark` reads and writes the same tables from Spark, in two ways: `ScalaSparkJob`, which runs
the Scala job in [`spark/`](spark/README.md) with scala-cli (it fetches its own JDK, Spark and
Iceberg), and `SparkEngine`, a PySpark engine (the `spark` extra plus a JDK 17+). Both open the same
SQLite catalog as pyiceberg, and pyiceberg reads back what Spark writes. `spark_catalog_conf_for`
turns a catalog block into Spark settings for the `local`, `sql` and `rest` types:

```python
from local_data_platform.engine.spark import spark_catalog_conf_for

conf = spark_catalog_conf_for({"identifier": "nyc", "warehouse_path": "warehouse"})
print(conf["spark.sql.catalog.nyc.catalog-impl"])   # org.apache.iceberg.jdbc.JdbcCatalog
```

`make demo-spark` runs the Scala job on the demo table, and `ldp spark CONFIG` on any config's
table. The integration tests are opt-in
(`make spark-test`, which sets `LDP_RUN_SPARK=1`). No pipeline uses Spark, and DuckDB is the
supported SQL engine. [Spark](docs/spark.md) covers versions, the shared catalog and the limits.

## Architecture

```text
 rides.json ──► Config.from_json ──► registry.create_pipeline ──► Pipeline.run(mode, commit=, sink=)
                                                                     │
          ┌──────────────────────────────────────────────────────────┘
          ▼
   source.get()  ──►  transforms  ──►  quality checks  ──►  target.put(mode, commit=)
   CSV · Parquet ·    your pyarrow      fail: raise           direct: one transaction, file lock
   JSON (local, s3,   callables         DataQualityError,     staged: branch ─► checks ─► publish (CAS)
   gs via fs.py) ·                      write nothing         CSV · Parquet targets
   Iceberg · BigQuery                                                │
                                                                     ▼
            catalog provider: local (SQLite + folder) · sql · rest · glue ── warehouse: file · s3 · gs
                                                                     │
   RunEvents ──► JSONL · OpenLineage · Iceberg (_ldp.runs, _ldp.quality_results)
                                                                     │
   read by: DuckDB (iceberg_scan) · Spark (experimental) · MCP server (read-only, audited to _ldp.audit) · datasets.pin
```

Data moves between steps as `pyarrow.Table`s. The package lives in `src/local_data_platform/`:

| Module | Purpose |
|---|---|
| `config`, `paths` | `Config` and path resolution relative to the config file |
| `spec` | The `ldp/v1` JSON Schema, config validation, `spec_hash` and `idempotency_key` |
| `fs` | Local and object-storage IO (`file://`, `s3://`, `gs://`) with atomic writes |
| `format` | `CSV`, `Parquet` and `Iceberg` tables (`get` / `put`), and `WriteResult` |
| `format.iceberg.commit` | The staged publish protocol: `CommitContext`, `write_once`, `stage`, `publish`, `fence` |
| `catalog` | `create_catalog` and the `local`, `sql`, `rest` and `glue` types; `LocalIcebergCatalog` |
| `store` | Sources: BigQuery (`[bigquery]` extra), JSON files, and older import paths kept for compatibility |
| `quality` | Checks, including the temporal ones in `quality.temporal`, `run_checks` and `QualityReport` |
| `events` | `RunEvent` and the event sinks |
| `engine.duckdb` | `DuckDBEngine` for SQL over Iceberg and Arrow tables (`[duckdb]` extra) |
| `engine.router` | Scan-size estimates and engine choice |
| `engine.spark` | Experimental: `SparkEngine`, `ScalaSparkJob` and `spark_catalog_conf_for`; see [Spark](docs/spark.md) |
| `mcp_server` | The read-only MCP server (`[mcp]` extra) |
| `datasets` | Snapshot-pinned dataset versions |
| `maintenance` | Snapshot expiry with the idempotency floor, and orphan-file detection |
| `pipeline` | `Pipeline`, the built-in pipelines and the registry (`create_pipeline`) |
| `cli`, `demo` | The `ldp` command and the demo |
| `github`, `issue` | GitHub REST helpers used by the project's docs tooling |

[Why a registry instead of if/else](docs/design/factory_registry.md) explains how pipelines are
chosen. The [SaaS architecture](docs/design/saas_architecture.md) is a proposal for where this
could go; none of its control plane exists.

## Development

```bash
make install      # create .venv and install the package with the dev and docs extras
make lint         # flake8
make test         # pytest, offline: the Spark and REST integration tests skip
make demo         # run the demo into ./ldp_demo
make demo-all     # every demo; demo-warehouse needs the dbt and excel extras, demo-spark and demo-rest need scala-cli
make spark-test   # opt-in Spark integration tests (LDP_RUN_SPARK=1, scala-cli)
make rest-test    # opt-in REST catalog integration tests (LDP_RUN_REST=1, scala-cli)
make dbt-test     # the tests that need the dbt extra (pip install -e ".[dev,dbt]" first)
make docs         # mkdocs build --strict
make build        # build the sdist and wheel into dist/
make smoke        # install the wheel in a fresh venv and run ldp --version and ldp demo
```

The JVM demos and tests use [scala-cli](https://scala-cli.virtuslab.org), which provisions its own
Temurin 17 JDK; the first run downloads it with Spark, Iceberg and their jars (about 500 MB).
`tests/test_rest_catalog.py` and `make demo-rest` start the Iceberg REST fixture in
`tools/rest_fixture` (`make demo-rest` on port 8181; `REST_PORT=` changes it) and stop it
afterwards, also when a step fails.

CI runs lint, tests on Python 3.12 and 3.13 against both pyiceberg 0.11.0 and the newest supported
release, the wheel smoke test and a strict docs build. The Spark and REST jobs are in a separate,
opt-in workflow (`.github/workflows/jvm.yml`): they run when started by hand, weekly, or on a pull
request labelled `jvm`, and never block a merge. See the [changelog](CHANGELOG.md) for what changed
in each release, and the [contributing guide](docs/contributing.md) to get involved.

## Known limitations

- **The engine router is unused.** Nothing calls `engine.router` yet.
- **Remote catalogs are lightly tested.** `rest` is tested against Iceberg's REST test server and
  `glue` against moto. Postgres, hosted REST catalogs and GCS have no integration tests yet.
- **Direct overwrite and upsert on a shared catalog are single-writer.** Several writers need the
  staged protocol (`commit=`). The local file lock only covers processes on one machine.
- **Exactly once means at most one effect per key.** Duplicates a producer sent upstream are not
  removed.
- **An upsert batch needs every column of the table.** An upsert replaces whole rows, so a batch
  that lacks one of the table's columns is rejected with a `ConfigError` that names them (appends
  accept it).
- **Mostly in memory.** Reads return a whole `pyarrow.Table`. DuckDB's native `iceberg_scan`
  streams, but pipelines don't; there are no streaming or incremental reads.
- **Spark is experimental.** It needs a JDK (or scala-cli) and downloads jars on first use, no
  pipeline uses it, and its tests are opt-in.
- **The MCP server is read-only and local.** It speaks MCP over stdio only. Its `_ldp.audit` rows
  are appended when it stops, so a server that is killed leaves them only in the JSONL audit file.
- **No compaction.** Maintenance expires snapshots and finds orphans; it doesn't rewrite data files.
- **BigQuery is read-only.** It can be a source, not a target.
- **`ldp dbt` publishes one model at a time.** Nothing is published when dbt fails, but the writes of a
  successful build are separate commits, so a write that fails, such as a blocked quality check or a model
  with no rows, leaves the models before it published. A model published with an upsert that gains or loses
  a column needs one `ldp run CONFIG --mode overwrite`.
- **The restaurant example is in the repo, not in the package.** `pip install` does not include
  `examples/`, so the warehousing guide and `make demo-warehouse` need a clone.

## Roadmap

From 0.1.1 on, the package version equals the milestone number. Each milestone adds the next layer
of a data platform, and every layer runs on a laptop first. 0.1.2 Warehousing is released, and these
are the milestones after it:

| Version | Milestone | What it adds |
|---|---|---|
| 0.1.3 | Orchestration | Windows, backfills, retries and a laptop scheduler, plus an Airflow operator |
| 0.1.4 | Self Serving Gold Layer | Gold tables with owners and docs, releases, metrics, a catalog and exports |
| 0.1.5 | Monitoring | SLAs, freshness, volume, schema and metric monitors, alerts and `ldp status` |
| 0.1.6 | Business Intelligence Reporting Dashboarding | Offline HTML reports, Slack delivery, a live dashboard and BI tool recipes |
| 0.1.7 | Data Science Insights | Churn, retention, funnels, sessions, training sets and a notebook |
| 0.1.8 | LLM | Catalog search and `ldp ask`, text to SQL that corrects itself |
| 0.1.9 | Launch Documentation | The Learn path, generated reference pages, onboarding and contributor setup |
| 0.2.0 | Cloud Integration | Profiles, cloud catalogs and storage, Snowflake and one container image |

The [roadmap](docs/roadmap.md) has the goal, issues and demo of each milestone.

## References

- [PyIceberg](https://py.iceberg.apache.org)
- [DuckDB Iceberg extension](https://duckdb.org/docs/extensions/iceberg.html)
- [Model Context Protocol](https://modelcontextprotocol.io)
- [OpenLineage](https://openlineage.io)
- [NEAR Lake Framework](https://docs.near.org/concepts/advanced/near-lake-framework)
- [Reliable change data capture using Iceberg](https://medium.com/@tushar.choudhary.de/reliable-cdc-apache-spark-ingestion-pipeline-using-iceberg-5d8f0fee6fd6)
- [Internals of Apache PyIceberg](https://medium.com/@tushar.choudhary.de/internals-of-apache-pyiceberg-10c2302a5c8b)

## License

MIT. See [LICENSE](LICENSE).
