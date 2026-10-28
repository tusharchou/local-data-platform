# Local Data Platform

**local-data-platform** (`ldp`) is a Python library for running an Apache Iceberg lakehouse on your
laptop, and for taking the same pipelines to a shared catalog and object storage when you need to.
You describe a dataset in a JSON config, and `ldp` loads it into an Iceberg table, runs data quality
checks before the write, and lets you query the table with DuckDB SQL and read earlier snapshots.
The default setup needs no server and no cloud account.

It's for learning how a lakehouse works, for building and testing pipelines locally before you pay
for cloud infrastructure, and for giving agents and notebooks safe, reproducible access to the
tables you build.

> **Status.** This is version 0.1.4, the Self Serving Gold Layer milestone. It adds gold tables that
> carry an owner, a meaning and a description of every column, `ldp gold build` and one release of all
> the gold tables, `ldp gold list`, `show` and `docs` as a catalog, metrics defined once in
> `metrics.json`, and Excel, CSV and DuckDB files for people (see [Gold layer](#gold-layer)), with a
> [guide](docs/gold.md). Version 0.1.3 added windows and idempotency keys to `ldp run`, workflow files
> with `ldp workflow plan` and `ldp workflow run`, `ldp backfill`, retries, `ldp schedule` for cron and
> launchd, and an Airflow 3 operator (see [Orchestration](#orchestration)), with a
> [guide](docs/orchestration.md). Version 0.1.2 added `ldp dbt`, which runs a dbt project over
> your Iceberg tables and publishes the models you mark back to Iceberg (see
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
pip install "local-data-platform[duckdb]>=0.1.4"

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
| `make demo-orchestration` | The restaurant's nightly drops run as a workflow: a backfill that stops at a bad row and resumes after the fix, a third run that skips every day, and a scheduler tick (runs its dbt models task and its gold task, which cuts a release for each night, when the `dbt` extra is installed) |
| `make demo-gold` | The restaurant data mart: a faulty batch that the gate stops with each failed check named, dbt models whose gold tables have an owner, a meaning and a gate, a release of the gold tables, a metric, an Excel file, a DuckDB file and a catalog page (needs the `dbt` and `excel` extras) |
| `make demo-spark` | The Scala Spark job reading the demo table and writing an aggregate that pyiceberg reads back (needs scala-cli) |
| `make demo-rest` | The demo config against an Iceberg REST catalog started locally on scala-cli's JVM: `ldp catalog test`, two runs, the snapshots and a query (needs scala-cli) |
| `make demo-all` | All of them; `demo-warehouse` and `demo-gold` are skipped when dbt or the Excel reader isn't installed and the two JVM demos when scala-cli isn't |

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
ldp query config/ "SELECT count(*) FROM gold.daily_sales" --release r2   # the gold tables as release r2 names them
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
| 3 | a data gate held the run back, such as a quality check that blocked a write, or a gold table whose contract does not hold |

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
| `ldp backfill FILE --from START [--to END] [--dry-run] [--replace] [--run-backwards] [--max-windows N] [--stop-on-failure] [--json]` | Run a workflow, or a config with a window, for every window of a range, oldest first, each pipeline under the key of its window. A window that is done is skipped without being read, so running it again resumes where it stopped; `--dry-run` lists the windows with their keys and states, and `--replace` writes them again under a replace key; exit status 0, 1, or 3 for a data gate | `orchestration.backfill(path, start, end, ...)` |
| `ldp workflow run FILE --window W [--json]` | Run the tasks of a workflow one at a time for a window. A failed or blocked task stops the tasks that depend on it and the rest still run, and a second run skips what published or was built; exit status 0, 1, or 3 for a data gate | `orchestration.run_workflow(path, window=...)` |
| `ldp schedule tick DIR [--now TIME] [--max-windows N] [--json]` | Run every window that is due and not done, for every workflow and config under DIR that has a schedule, then exit. cron or launchd calls it every 5 minutes. A window that failed is carried on by the next tick as the same run with the next attempt, and a tick that starts while another runs does nothing; exit status 0, 1, or 3 for a data gate | `orchestration.schedule.tick(dir, now=...)` |
| `ldp schedule list DIR [--now TIME] [--max-windows N] [--json]` | List every scheduled workflow and config under DIR with its cron expression, time zone, next fire in UTC and in local time, last window and how it ended, the windows that are due, and the windows it missed with the `ldp backfill` command that fills them | `orchestration.schedule.list_scheduled(dir)` |
| `ldp schedule install --print {cron,launchd} DIR [--ldp PATH]` | Print the crontab line or the launchd plist that ticks DIR every 5 minutes. It never writes to a system location | `orchestration.recipes.recipe(kind, dir)` |
| `ldp plan CONFIG [--window WINDOW] [--json]` | Validate a config (or a folder of files, each as its kind) and print its spec hash, route, target, sinks, schedule, and the source path and idempotency key of a window | `spec.validate_spec(data)`, `spec.plan(config, window=...)` |
| `ldp runs CONFIG [--run RUN_ID] [--events] [--attempts]` | List the runs a config's sinks recorded, the last attempt of a run that was retried, or every attempt with `--attempts` | `events.summarize_runs(events.read_jsonl(path))` or `read_iceberg_events(catalog)` |
| `ldp commits CONFIG [--key KEY] [--branches]` | List the staged publishes on a table, newest first | `format.iceberg.commit.commit_log(iceberg.table())` and `staged_branches(...)` |
| `ldp datasets pin\|list\|export` | Pin, list and export dataset versions, as Parquet, JSONL or CSV | `datasets.pin`, `list_versions`, `export` |
| `ldp maintain CONFIG [--expire DAYS] [--orphans] [--apply]` | Expire snapshots and find orphan files; a dry run unless `--apply` | `maintenance.expire_snapshots`, `find_orphans`, `remove_orphans` |
| `ldp mcp --config CONFIG ...` | Serve tables to an agent over MCP (stdio) | `python -m local_data_platform.mcp_server ...`, same options |
| `ldp dbt init\|build\|test\|docs\|exec PROJECT_DIR` | Run a dbt project over your Iceberg tables; `build` writes the models marked `ldp_publish` back to Iceberg (needs the `dbt` extra) | `dbt_layer.register_sources(...)`, `dbt_layer.publish.plan_publish(...)` and `publish(...)` |
| `ldp gold sync CONFIG [--dbt-manifest FILE] [--dry-run] [--json]` | Write the contract of a config (its `metadata.docs`) into its existing Iceberg table, with no new data. `--dbt-manifest` reads it from a dbt manifest, for a table that dbt built, and `--dry-run` only says what would change. See [Table contracts](#table-contracts) | `gold.sync.sync_config(path)`, `contracts.apply_contract(table, docs)` |
| `ldp gold build DIR [--no-release] [--duckdb FILE] [--json]` | Build the gold tables of a folder in the order that their `inputs` say, and check the ones that are built elsewhere. One line for each table, and a gate that blocks a table stops the build with exit status 3. When every table passed, and the metrics of the folder fit them, it cuts a release, unless `--no-release` says not to. `--duckdb FILE` then writes the DuckDB file of the gold layer. See [Build the gold tables](#build-the-gold-tables) | `gold.build.build_gold(folder, release=True)`, `gold.serve.refresh_after_build(folder, out, result)` |
| `ldp gold releases DIR [--show RELEASE] [--json]` | List the releases of the gold tables of a folder, newest first, or the tables of one release. See [Release the gold tables](#release-the-gold-tables) | `gold.release.list_releases(folder)`, `current_release(folder)`, `snapshot_for(table, release)` |
| `ldp gold list DIR [--all-layers] [--json] [--strict]` | One row for each gold table of a folder, with what it means, who owns it, what one row is, its rows, when its data last changed, the status of its last run, the verdict of its last quality check and the release it is in. A table with no contract shows `no description`. `--all-layers` adds the other tables, and `--strict` exits with 1 when a gold table lacks part of its contract, for CI. See [Browse the gold tables](#browse-the-gold-tables) | `gold.catalog.collect(folder)` |
| `ldp gold export DIR TABLE DEST [--format csv\|parquet\|jsonl\|xlsx] [--release RN\|--live] [--json]` | Write one gold table, as the current release holds it, as a CSV, Parquet, JSONL or Excel file, which opens with no Python and no Iceberg. An Excel file has a second sheet, `about`, with the contract of the table, and needs the `excel` extra. See [Open the gold tables in a spreadsheet or a BI tool](#open-the-gold-tables-in-a-spreadsheet-or-a-bi-tool) | `gold.serve.export_table(folder, table, dest)` |
| `ldp gold duckdb DIR --out FILE [--release RN\|--live] [--json]` | Write the whole gold layer as one DuckDB file, with a copy of each table of the current release, a comment on each table and column, a `_catalog` table and a `_metrics` table. It opens in the DuckDB command line, the DuckDB UI and the BI tools that read DuckDB. See [Open the gold tables in a spreadsheet or a BI tool](#open-the-gold-tables-in-a-spreadsheet-or-a-bi-tool) | `gold.serve.write_duckdb_file(folder, out)` |
| `ldp gold show DIR TABLE [--runs N] [--no-samples] [--json]` | Describe one table: its contract, every column with its type and description, its last runs, the releases that include it, a few sample rows and an `ldp query` line that reads it | `gold.catalog.collect(folder, only=table, detail=True)` |
| `ldp gold docs DIR --out FILE [--format html\|md] [--all-layers] [--no-samples]` | Write the catalog as one self-contained page with a search box, or as Markdown for a MkDocs site | `gold.render.html_page(catalog)`, `gold.render.markdown_page(catalog)` |
| `ldp metrics list DIR [--json]` | One row for each metric of the `metrics.json` files of a folder, with its description, owner, format, table, dimensions and grains. See [Define metrics](#define-metrics) | `metrics.load_metrics(folder)` |
| `ldp metrics validate DIR [--json]` | Check every name and expression of the metrics against the schema of the gold tables, list every problem, and exit with 1 if there is one. No row is read, and `ldp gold build` runs the same check | `metrics.validate_folder(folder)` |
| `ldp metrics query DIR METRICS [--by DIMS] [--grain GRAIN] [--filter DIM=VALUE] [--from DAY] [--to DAY] [--release RN\|--live] [--sql] [--out FILE] [--raw] [--json]` | Compute metrics from the current release of the gold tables, grouped and filtered, and print them in their format. `--sql` prints the SQL and runs nothing, and `--out` writes CSV, Parquet or JSONL | `metrics.query(folder, metrics, by=..., grain=..., filters=[...])` returns a `pyarrow.Table` |
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
| `metadata.retries` | How a workflow tries the run again after a failure: `max_attempts` (1 by default, which is no retry), `delay_s` (30) and `backoff` (2.0); see [Workflow files](#workflow-files). Editing it does not change the spec hash |
| `metadata.lineage` | The Iceberg tables that a source was made from, as `ICEBERG` blocks, for the run events only. `ldp dbt build` writes it for each model that it publishes, from the manifest. A `DUCKDB` source with `inputs` needs none. Editing it does not change the spec hash |
| `metadata.docs` | The contract of the table: `layer` (`bronze`, `silver` or `gold`), `description`, `owner` (the config's `who` by default), `grain`, `primary_key`, `columns` (column name to description), `tags` and `refresh` (the config's `when` by default); see [Table contracts](#table-contracts). Editing it does not change the spec hash |
| `metadata.sla` | How fresh, how big and how stable the table should be, in up to three sections. `freshness` has `warn_after` and `error_after`, each a `count` of `minute`, `hour` or `day` periods such as `{"count": 26, "period": "hour"}`, and an optional `column`. `volume` has `min_rows`, `max_rows`, `max_change_pct` and `lookback_runs` (7 by default). `schema` has `on_change` (`ignore`, `warn` or `error`, `warn` by default) and `allow`, a list of `added_column` and `widened_type`. A malformed value is an error that names its path, `ldp plan` prints the SLA and `ldp schema` describes it. Nothing checks a table against it yet, because the monitors are the rest of the 0.1.5 milestone. Editing it does not change the spec hash |

A config may also say `"apiVersion": "ldp/v1"` and `"kind": "Pipeline"`. `kind` says what a JSON file is. `Pipeline`
is the default, so every config here is one, and a config has the same spec hash with and without it. `Workflow` is
the second kind and `Metrics` the third, see [Define metrics](#define-metrics). A kind is written exactly as shown, so
`workflow` fails with a message that names `Workflow`, and `Report` and `Profiles` are reserved for later milestones.
`ldp plan` validates each file of a folder as its kind, and `ldp query`, `ldp mcp --config` and `ldp dbt` read the
tables of the `Pipeline` files only, so a folder can hold workflow and metrics files next to its configs.
`ldp schema --kind Metrics` prints the schema of another kind.

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
  be redone and the other days stay. A published window is skipped when it runs again, so to redo a day that changed at
  the source, replace it with `ldp backfill daily_orders.json --from 2026-08-02 --to 2026-08-03 --replace`, or run it with a
  key of your own: `ldp run daily_orders.json --window 2026-08-02/2026-08-03 --idempotency-key orders-2026-08-02-v2`.
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
  "metadata": {"schedule": "5 2 * * *", "window": {"size": "day"}, "retries": {"max_attempts": 3, "delay_s": 30}},
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
  relative to the workflow file), `dbt` (a dbt project, built for the window, see below), `maintain` (a config's Iceberg
  table and the options of `ldp maintain`, a dry run unless `apply` is true) or `gold` (the gold tables of a folder, built
  for the window, with a release cut at the end, see [Release the gold tables](#release-the-gold-tables)). Later
  milestones add kinds without changing the file format.
- Tasks run one at a time, in dependency order. A pipeline task runs with the window of the workflow, so its idempotency key
  is the one `ldp plan CONFIG --window` prints.
- A task that fails, or that a quality check blocks, makes the tasks that depend on it `upstream_failed`, and they do not run.
  The tasks that do not depend on it still run. The exit status is 1 when a task failed and 3 when the only problem is a
  data gate.
- Run the same window again after fixing the cause: the pipeline tasks that published are skipped by their keys, without
  reading their sources, and only the tasks that failed or did not run do their work.
- A task that fails with an error that another attempt may fix runs again by itself, as `retries` says, in the workflow
  or in the task, and a key of the task overrides the same key of the workflow. These are retried: a dropped connection,
  a timeout, a file that has not landed yet (any `OSError`) and the commit errors of pyiceberg. A data gate, a wrong
  config and a fenced attempt never are. A retry is the next attempt of the same run, with the same run id and the same
  idempotency key, so the new attempt fences the old one and a load that already landed is found and not written
  twice. The wait is a random time from 0 to `delay_s * backoff ** (n - 1)` seconds after attempt n. `ldp runs` lists the
  last attempt of each run, and `ldp runs LEDGER --attempts` lists them all.
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

### Backfill a range of windows

`ldp backfill` runs a workflow, or a config that has a window, for every window of a range. It is how a new pipeline loads
its history, and how a load that stopped halfway finishes:

```bash
ldp backfill daily_orders.json --from 2026-06-01 --to 2026-09-01 --dry-run   # every window, with its key and its state
ldp backfill daily_orders.json --from 2026-06-01 --to 2026-09-01             # load them, oldest first
ldp backfill daily_orders.json --from 2026-06-01 --to 2026-09-01             # again: it skips every window that is done
ldp backfill daily_orders.json --from 2026-08-02 --to 2026-08-03 --replace   # redo a day that changed at the source
```

- The windows are the windows of `metadata.window.size` (`day`, `hour` or `15m`), or of the size the schedule means, a day for
  `@daily` and an hour for `@hourly`. `--from` and `--to` are dates or datetimes in the time zone of the workflow. Each of them
  has to start a window, and the window that starts at `--to` is not part of the range, so `--from 2026-06-01 --to
  2026-06-04` is the three days of June 1st, 2nd and 3rd. Without `--to` the range ends with the last complete window. A
  range of more than `--max-windows` windows, 1000 by default, is refused, to catch a typo.
- Each window runs as `ldp workflow run` runs it, so each pipeline publishes under the key that `ldp plan CONFIG --window W`
  prints, and a task with `retries` is retried. A window whose keys are on the table is skipped without being read, planned
  or written to the ledger, so a finished backfill that runs again does nothing, and one that stopped at window 40 starts
  again at window 40. A workflow with a task that a key cannot vouch for, such as a dbt task or a pipeline that writes a file,
  also needs the last run of the window in the ledger to have succeeded for every task.
- A window that fails, or that a quality check blocks, does not stop the others, and it is listed with what went wrong,
  such as the check that failed. The summary counts the windows that published, were skipped, were blocked and failed.
  The exit status is 0 when no window failed or was blocked, 3 when a data gate is the only problem, and 1 otherwise.
  `--stop-on-failure` stops at the first window that failed instead, and a window that a quality check blocked does not
  stop it.
- `--dry-run` prints every window with its key and its state, which is `published` (done, so it is skipped), `partial`,
  `missing`, `failed` or `blocked` (the last run of it in the ledger failed or was held back by a data gate), and runs
  nothing, not even the creation of a table. `--run-backwards` runs the newest window first, and `--json` prints the result.
- `--replace` writes each window again under a replace key, the sha256 of its key, the word `replace` and the spec hash of
  its config, with a window overwrite. The rows that a window has are replaced by the rows the source has now, a window that
  has none is loaded, a window that was replaced under the same spec is skipped, and one that was replaced under an older
  spec is replaced again. It needs a `metadata.window.column` in every pipeline that writes to Iceberg, and without one it
  exits with 1 and changes nothing. A window that is replaced has to have rows in the source. An empty one is not written,
  because the run fails with "No data to write", or is blocked by a `row_count` check, and the table keeps its rows.
- From Python, `from local_data_platform.orchestration import backfill` runs the same, and `backfill(path, start, end,
  dry_run=True)` returns the windows with their keys and states. The [contract](docs/design/v0_1_1.md#backfills-013) has the
  rest.

### Run on a schedule

`ldp schedule tick` is a scheduler with no server and no daemon. cron or launchd calls it every 5 minutes, it runs every
window that is due and not done, and it exits, so a laptop that was asleep catches up at the first tick after it wakes:

```bash
ldp schedule list ldp_demo/restaurant                                    # what is scheduled, when it fires, what it missed
ldp schedule tick ldp_demo/restaurant --now 2026-08-05T02:05:00Z         # run what is due at that time, then exit
ldp schedule install --print launchd ldp_demo/restaurant > ~/Library/LaunchAgents/restaurant.plist   # macOS
(crontab -l 2>/dev/null; ldp schedule install --print cron ldp_demo/restaurant) | crontab -         # Linux
```

- A tick looks at every `.json` file under the folder that is a workflow file, or a dataset config, and has a schedule in
  `metadata.schedule` or in a `when` such as `daily`. A config with a schedule is a workflow of one task. A file that has a
  schedule and is not valid is printed with its problems and makes the tick exit with 1, and the other files still run.
- The schedule is a cron expression read in `metadata.window.timezone`, UTC without one. The window a fire owns is the
  interval between the previous fire and this fire. When the workflow has a window size, in `metadata.window.size` or implied
  by `@daily` and `@hourly`, it is the last complete window of that size at or before the fire, so the fire of `5 2 * * *` on
  5 August owns 4 August. A fixed time that a clock change skips fires right after the gap, and one that happens twice fires
  once.
- Without `catchup`, a tick runs the window of the latest fire if it is not done, and `ldp schedule list` prints the
  `ldp backfill` command for any windows before it that were missed. With `"catchup": true` and a `"start"` in the metadata of
  the workflow, a tick runs every window since `start` that is not done, oldest first, up to `--max-windows` (1000) a tick.
- A window that failed, was blocked by a data gate, or never finished is run again by the next tick, as the next attempt of
  the same run. The workflow and each task keep their run ids and the attempts go up by one, so the first commit of the new
  attempt fences every older one and the window is loaded once however many ticks it takes. `retries` are the retries within
  one tick.
- A tick takes the lock `DIR/.ldp/locks/schedule.lock` without waiting for it. A tick that starts while another runs does
  nothing and exits with 0, and a crash leaves no lock. The exit status is 0 when nothing failed or was blocked, 3 when a data
  gate is the only problem, and 1 otherwise.
- The cron line and the plist call `ldp` by its absolute path and write a log, `DIR/.ldp/schedule.log` or a file in
  `~/Library/Logs`. cron does not run while a Mac sleeps, and launchd runs an interval that passed during sleep once when the
  Mac wakes, so launchd is the recipe for macOS and cron the recipe for Linux.
- From Python, `from local_data_platform.orchestration.schedule import tick, list_scheduled` does the same. The
  [contract](docs/design/v0_1_1.md#schedules-013) has the rest.

### Run from Airflow

A team that already runs Apache Airflow 3 can run a config as a task. `LdpRunOperator` takes the data interval of the DAG run
as the window, `ti.try_number` as the attempt, and a run id made of the DAG id, the run id of the DAG run and the task id. So
a retry by Airflow is the next attempt of the same run, a backfill over a day that is in the table finds the key and writes
nothing, and a day is loaded once however Airflow got to it. The [guide](docs/airflow.md) installs Airflow, runs the daily
drops of the restaurant example from `examples/restaurant/airflow_dag.py`, and retries a late drop. Its commands, in short, are these.

```bash
export LDP_AIRFLOW_DEMO="$PWD/ldp_demo/airflow/data"
python examples/restaurant/airflow_dag.py --prepare "$LDP_AIRFLOW_DEMO"   # the seven daily drops and a windowed orders config
airflow standalone                                                        # terminal 1, with the DAG file in its dags folder
airflow dags unpause restaurant_orders                                    # terminal 2, catches up seven days one after the other
ldp commits "$LDP_AIRFLOW_DEMO/config/orders.json"                        # terminal 2, one publish for each day
```

- The window is `(data_interval_start, data_interval_end)`, so the key of an interval is the one
  `ldp plan CONFIG --window START/END` prints for it.
- A data gate that holds a run back, and a config that cannot run, fail the task with `AirflowFailException`, so Airflow does
  not retry them. A skipped duplicate succeeds, and the `PipelineResult` as a dict is the XCom of the task. Any other error is
  left to the `retries` of the task.
- Give the DAG a `CronDataIntervalTimetable("@daily", timezone="UTC")` as its schedule and not the string `"@daily"`. Airflow 3
  makes a trigger timetable for a cron string, whose runs have an empty data interval, and the operator fails them and says why.
- The mapping is plain Python and Airflow is imported when the operator is used, so `local_data_platform.integrations.airflow`
  imports without it. There is no extra for it, because Airflow has to be installed with its own constraints file. Airflow 3.1.7
  and later install next to LDP, and a team that cannot do that runs `ldp run CONFIG --window ... --run-id ... --attempt ...`
  from a BashOperator, which the guide shows. The [contract](docs/design/v0_1_1.md#airflow-013) has the rest.

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

Use `relation` (`table`, `schema.table` or `catalog.schema.table`), `sql` or `query_file`, one of them. The file is
opened read-only and closed before the Iceberg write starts, and a query must be exactly one `SELECT` or `WITH` query.
A `query_file` is a file that holds the query, in place of `sql`. It needs the `duckdb` extra. `ldp plan` reports a
missing file or a source with more than one or none of the three.

A source can also have `inputs` in place of `path`, which is a list of dataset configs. It then reads no file. It
opens an in-memory DuckDB, registers the Iceberg table that each input config writes under that table's name, with the
session time zone set to UTC, and runs the query on them. This is how a gold table is one SQL file over silver tables,
and [`ldp gold build`](#build-the-gold-tables) builds them in order.

```json
"source": {"name": "revenue_by_restaurant", "format": "DUCKDB", "query_file": "models/revenue_by_restaurant.sql",
           "inputs": ["silver_orders.json"]}
```

A query that reads a table that is not in `inputs` fails with DuckDB's own unknown table error, so `inputs` is the whole
lineage of the table, and every input is an input dataset in the run's OpenLineage events.

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

### Table contracts

A table says what it means, who owns it, what one row is and what each column holds, in a `metadata.docs` block next to
the pipeline that builds it. After a run publishes, LDP writes the block into the Iceberg table, as the properties
`comment`, `ldp.owner`, `ldp.layer`, `ldp.grain` and `ldp.primary-key` and the doc of each column. Anyone who finds the
table reads it there, in `ldp mcp`, in DuckDB or in any other client.

```json
"docs": {
  "layer": "gold",
  "description": "Orders and revenue for each restaurant on each day it had an order.",
  "owner": "restaurant operations",
  "grain": "one row for each restaurant on each day it had an order",
  "primary_key": ["restaurant_id", "day"],
  "columns": {"restaurant_id": "The restaurant.", "day": "The day.", "orders": "How many orders.",
              "revenue": "What they cost."}
}
```

- A `gold` table has to keep its promise. It needs a `description`, a `grain` and an owner (`owner`, or the `who` of the
  config). `ldp run` stops it before anything is written when a column has no description, or when `columns` describes
  a column that the table does not have, and the exit status is 3, as for a failed quality check. A table of another
  layer may document as much or as little as it likes.
- The contract is one metadata-only commit, so it adds no snapshot, and a table that already has it gets no commit. It
  only adds and changes text, and a run that wrote nothing does not touch the table.
- `ldp gold sync CONFIG` writes the contract of a config into a table that exists, with no new data. With
  `--dbt-manifest target/manifest.json` it reads the contract from the manifest, for a table that dbt built.
- `ldp dbt build` fills the block from the dbt manifest. A model that is published to the `gold` namespace is a gold
  table, so it needs `owner` and `grain` in its meta, a description, and a description of each column.
- Editing the block does not change the spec hash. The design is in [the v0.1.4 contract](docs/design/v0_1_4.md).

### Build the gold tables

A gold table is one SQL file over other tables. Its config has a `DUCKDB` source with `inputs` and a `query_file`, and
a `metadata.docs` block with `"layer": "gold"`. `ldp gold build DIR` finds every gold config of a folder, orders them by
their `inputs`, since a gold table may read another, and builds them one at a time.

```bash
ldp gold build examples/robot_episodes/data
```

```text
gold_episode_stats  built          96 rows  snapshot 3844274472678295321  4 of 4 checks passed
gold_robot_daily    built          16 rows  snapshot 1985638678580193697  2 of 2 checks passed
gold build examples/robot_episodes/data: 2 built, 0 checked, 0 blocked, 0 failed, 0 not run in 0.6s
metrics: 1 semantic model and 5 metrics fit the gold tables
release r1 cut with 2 tables and 112 rows in robots
```

The last line is the release that the build cuts, see [Release the gold tables](#release-the-gold-tables).

- A cycle between configs is refused before anything runs, and the message names it.
- The build stops at the first table that a gate blocks or that fails, and the tables after it are `not run`, because
  they could read what it did not write. The exit status is 3 when a gate held a table back, and 1 on any other
  failure. A table whose contract does not hold is held back by a gate too.
- A gold config with no `inputs`, such as a dbt model that `ldp dbt build` published or a table that Spark or a script
  wrote, is built elsewhere. The build does not run it. It checks that the table exists and runs its quality checks on
  the current snapshot, and it writes nothing. A failing check blocks the build, unless the config says
  `on_failure` `warn`.
- The runs are recorded in `_ldp.runs` and `_ldp.quality_results`, through the `iceberg` sink, unless the config names
  sinks of its own, so the freshness and the gate status of a gold table show in `describe_table` of `ldp mcp`.
- A folder with a `metrics.json` has its metrics checked, see [Define metrics](#define-metrics). A file that is not
  valid stops the build before anything is built. When every table was built or checked, the names and expressions are
  checked against the tables, and the build says so with a `metrics:` line. Metrics that do not fit cut no release and
  the exit status is 1, and the readers still see the last release.

### Release the gold tables

A gold table is rebuilt on its own, so two tables that are read at the same moment can come from different builds. A
release fixes that. When every gold table of a build was built or checked, `ldp gold build` cuts release `r1`, and then
`r2` and so on. A release names the snapshot that each gold table ended the build with, so a reader that reads the
current release sees tables that agree. A build that a gate blocks cuts no release, and the last release stays current.
The build says which release readers still see.

```bash
ldp gold build examples/robot_episodes/data       # builds, and then cuts a release
ldp gold releases examples/robot_episodes/data    # the releases, newest first
ldp gold releases examples/robot_episodes/data --show r1
ldp query examples/robot_episodes/data "SELECT count(*) FROM robots.gold_episode_stats" --release r1
```

```text
gold_episode_stats  built          96 rows  snapshot 7902942047508908069  4 of 4 checks passed
gold_robot_daily    built          16 rows  snapshot 7453607785734378588  2 of 2 checks passed
gold build examples/robot_episodes/data: 2 built, 0 checked, 0 blocked, 0 failed, 0 not run in 0.6s
metrics: 1 semantic model and 5 metrics fit the gold tables
release r1 cut with 2 tables and 112 rows in robots
```

- A release is a file in the warehouse, `<warehouse>/.ldp/releases/<namespace>/r1.json`. It lists each table with its
  snapshot, the dataset version that pins it, the run that built it and its rows. The file is written to a temporary
  path and renamed, so a reader sees all of it or none of it.
- Each table is pinned with `datasets.pin` as the dataset `<namespace>.<table>`, with the property `release=r1`. A pin
  tags its snapshot, and snapshot expiry never removes a tagged snapshot, so `ldp maintain` keeps every released
  snapshot. That keeps its files too, so a table that is rebuilt in full for each release keeps a copy for each of them.
- A release covers one namespace of one warehouse, because that is where its file is. A folder whose gold tables are in
  more than one is refused before anything is built. Give each its own folder, or build with `--no-release`.
- A build that finds the tables at the snapshots of the current release, as a workflow does when it runs a window that
  it already ran, cuts no new release.
- `ldp query DIR SQL --release r1` reads the gold tables as r1 names them. A table that the release does not hold, and
  a table in a namespace that has no releases, is read at its current snapshot. Without `--release`, `ldp query` reads the
  tables as they are now.
- Code that reads the gold layer asks for the release with `local_data_platform.gold.release`. `current_release(dir)`
  gives the release readers see, `snapshot_for(table, release)` gives the snapshot to read, and `register_release(engine,
  dir)` registers every gold table in a `DuckDBEngine` at its snapshot in the release. Pass `release="r1"` for an older
  one and `live=True` to read the tables as they are.
- `ldp runs` shows a release in the history of each table, as a run with the status `released`.

A workflow can build and release the gold layer as one of its tasks, after the pipelines and the dbt project that
feed it. The task is one line.

```json
{"id": "gold", "depends_on": ["orders", "dbt"], "gold": {"dir": "config"}}
```

The task builds the gold tables of `config` for the window of the run, as a pipeline task runs a config, so running the
same window again finds the tables already published and cuts no second release. It ends `blocked_quality` when a gate
holds a table back, and then no release is cut.

### Browse the gold tables

A person who is not an engineer should find the right gold table without reading pipeline code, and so should an agent
that works for them. `ldp gold list` prints one row for each gold table of a folder, and `ldp gold show` describes one.

```bash
ldp gold list examples/robot_episodes/data
```

```text
2 gold tables in examples/robot_episodes/data
table                      description                                       owner                       grain                      rows  refreshed_utc        run        quality        release
-------------------------  ------------------------------------------------  --------------------------  -------------------------  ----  -------------------  ---------  -------------  -------
robots.gold_episode_stats  One row for each promoted episode, with its o...  robot learning researchers  one row per episode        96    2026-10-16 09:17:03  published  4 of 4 passed  r1
robots.gold_robot_daily    Fleet health for each robot on each day: how ...  fleet operations            one row per robot per day  16    2026-10-16 09:17:03  published  2 of 2 passed  r1
(2 rows)
```

```bash
ldp gold show examples/robot_episodes/data gold_robot_daily
```

```text
robots.gold_robot_daily  (gold)
  Fleet health for each robot on each day: how many episodes it recorded, how many succeeded, and how healthy its frames were.
  Owner        fleet operations
  Grain        one row per robot per day
  Primary key  robot_id, recorded_on
  Refresh      after each promotion
  Tags         robotics, fleet
  Config       gold_robot_daily.json
  Rows         16
  Refreshed    2026-10-16 09:17:03 UTC
  Last run     published, run 01a14400-2856-7074-a459-21ae4afcae71, 2026-10-16 09:17:03 UTC
  Quality      2 of 2 passed
  Release      r1

Columns (9)
  ...
Metrics
  ...
Releases that include it (1)
  ...
Read it
  ldp query examples/robot_episodes/data "SELECT * FROM robots.gold_robot_daily LIMIT 10"
```

`show` also prints every column with its type and description, the metrics defined on the table, the last 5 runs, the
releases that include the table and 5 sample rows. The last line is an `ldp query` line that reads the table, ready to copy. `TABLE` is a table with its
namespace, or its bare name when only one table has it, and it can be a table of any layer.

- The tables are the Iceberg tables that the dataset configs of the folder write. A gold table is one whose
  `metadata.docs.layer` is `gold`, or whose Iceberg table says so, or that is in a namespace called `gold`.
  `--all-layers` adds the bronze and silver tables and the tables that have no layer, and `--json` prints the same as
  data.
- The contract is the one in the `metadata.docs` of the config and the one that the Iceberg table carries. Where both
  have a value the config wins, because it is what the next run writes, and `show` says so when the table does not carry
  it yet. A table with no contract shows `no description` and does not fail the listing, and its description is `null`
  in JSON.
- `--strict` exits with 1 when a gold table has no description, no owner or no grain, or has a column without a
  description, and it names the table and what it lacks. A table of another layer is never held to it. Run it in CI, so
  that a gold table that nobody documented does not get in. A table that does not exist yet is listed as `not built`.
- The last run and the last quality check are what `describe_table` of `ldp mcp` reports, from the same code. A release is
  not a run, so a table that was released still shows the status of its last build.
- `release` is the release that readers see now. `r1 (behind live)` says that the table was built again after r1, so
  readers of the release do not see the newer data yet. The rows and the time are the table as it is now, and the time
  leaves out the snapshots that `ldp maintain` makes.
- Nothing is written, not even a catalog or a namespace that does not exist yet.

`ldp gold docs` writes the same content as one page, or as Markdown for a MkDocs site.

```bash
ldp gold docs examples/robot_episodes/data --out catalog.html
ldp gold docs examples/robot_episodes/data --out docs/catalog.md --format md
```

```text
wrote catalog.html, 2 tables, 18.8 KB. Open it as a page. It needs no network.
wrote docs/catalog.md, 2 tables, 7.4 KB. Open it as a Markdown file.
```

- The page is one file with a search box, a light and a dark theme, and its CSS and its one script inline. It asks for
  nothing from the network, and its Content-Security-Policy makes the browser refuse to try, so it opens from a folder or
  from an e-mail. It is built with `local_data_platform.html`, which later pages share.
- Every value that a person wrote, such as the description of a table, is escaped. It can never become markup in the page,
  and the Markdown escapes it too.
- The sample rows of each table are in the file. Leave them out with `--no-samples` when the file will be shared and the
  rows must not be.
- In code, `local_data_platform.gold.catalog.collect(folder)` returns one entry for each table, and
  `entry.document()` is the text of an entry for a search to index.

`ldp mcp` says the same things to an agent. `list_tables` returns the layer, description, owner and grain of each table, and
`describe_table` returns the contract. `ldp mcp --config DIR --layer gold` serves only the gold tables, so no tool and no
SQL can reach the tables upstream of them.

### Define metrics

Revenue, orders and average ticket should mean one thing in every report, export and answer of an agent. A
`metrics.json` file next to the configs of the gold tables defines each number once, with what it means, who answers
for it and how a person reads it. The robot example has one, `examples/robot_episodes/configs/metrics.json`, and so has the
restaurant example, `examples/restaurant/config/metrics.json`, which defines `churn_rate` and `active_customers` on the
churn model of the warehouse demo.

```json
{
  "apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "robot_fleet_metrics", "who": "fleet operations",
  "semantic_models": [{
    "name": "robot_daily", "table": "robots.gold_robot_daily",
    "time_dimension": {"column": "recorded_on", "grains": ["day", "week", "month", "quarter", "year"]},
    "dimensions": [{"name": "robot", "column": "robot_id"}],
    "measures": [{"name": "episode_sum", "agg": "sum", "expr": "episodes"},
                 {"name": "success_sum", "agg": "sum", "expr": "successes"}]}],
  "metrics": [
    {"name": "episodes", "type": "simple", "measure": "episode_sum",
     "description": "How many episodes the robots recorded."},
    {"name": "success_rate", "type": "ratio", "numerator": "success_sum", "denominator": "episode_sum",
     "format": "percent", "description": "The share of episodes that succeeded."}]
}
```

A semantic model names one gold table, its `time_dimension` (a date column and the grains that its numbers may be
reported at), its `dimensions` and its `measures`. A measure is an aggregate, `sum`, `count`, `count_distinct`, `avg`,
`min` or `max`, over a SQL expression of the columns of the table. A metric is `simple`, which is one measure, or a
`ratio`, which is one measure over another. It has a `description`, an `owner` (its own, or the `who` of the file) and
a `format` of `number`, `percent` or `currency`. The terms follow MetricFlow, so an exporter can follow later.
`ldp plan DIR` validates the file as `Metrics`, and `ldp schema --kind Metrics` prints its schema.

```bash
ldp metrics list examples/robot_episodes/data
ldp metrics query examples/robot_episodes/data episodes,success_rate,frame_drop_rate --by robot --grain week
```

```text
metric             description                                       owner                       format   table                    dimensions  grains
-----------------  ------------------------------------------------  --------------------------  -------  -----------------------  ----------  -------------------------------
episodes           How many episodes the robots recorded.            fleet operations            number   robots.gold_robot_daily  robot       day, week, month, quarter, year
success_rate       The share of episodes that succeeded.             fleet operations            percent  robots.gold_robot_daily  robot       day, week, month, quarter, year
frame_drop_rate    The share of frames that were dropped.            fleet operations            percent  robots.gold_robot_daily  robot       day, week, month, quarter, year
worst_skew_ms      The largest RGB to depth gap of any frame, in...  fleet operations            number   robots.gold_robot_daily  robot       day, week, month, quarter, year
active_robot_days  Days with a recording, added up over robots.      robot learning researchers  number   robots.gold_robot_daily  robot       day, week, month, quarter, year
(5 rows)

week        robot  episodes  success_rate  frame_drop_rate
----------  -----  --------  ------------  ---------------
2026-08-31  h01          12         83.3%             0.3%
2026-08-31  h02          12         75.0%             0.5%
2026-08-31  h03          12         83.3%             0.3%
2026-08-31  h04          12         91.7%             0.4%
2026-08-31  h05          12         41.7%             0.4%
2026-08-31  h06          12         66.7%             0.3%
2026-08-31  h07          12         75.0%             0.4%
2026-08-31  h08          12         66.7%             0.0%
(8 rows)
read release r1, cut 2026-10-20 08:34:32 UTC
```

`--filter dimension=value` keeps the rows of a dimension, `--from` and `--to` take days that are both included, and
`--sql` prints what would run, with the value of each parameter, and runs nothing.

```bash
ldp metrics query examples/robot_episodes/data success_rate --filter robot=h05 --grain month --from 2026-09-01 --sql
```

```text
SELECT
  CAST(date_trunc('month', "recorded_on") AS DATE) AS "month",
  sum(successes) / NULLIF(sum(episodes), 0) AS "success_rate"
FROM "robots"."gold_robot_daily"
WHERE "robot_id" = $1 AND "recorded_on" >= $2
GROUP BY 1
ORDER BY 1
-- $1 = "h05"
-- $2 = DATE '2026-09-01'
```

- **A ratio is a ratio of sums.** `success_rate` is the sum of the successes over the sum of the episodes of the group,
  and never the average of the daily rates, which is a different number when the days have different counts. A group
  with no episodes has no value and not infinity.
- **A value is never SQL.** A filter value, a date and every other value that a person gives is bound as a parameter of
  the type of its column, so `--filter robot="h05' OR '1'='1"` matches no robot. Names go into the SQL only after they
  were checked against the definitions, and an unknown metric, dimension or grain is refused with the valid names and the
  closest one.
- **A query reads one table.** Metrics of two semantic models are refused with the table of each, because that would need
  a join, which this version does not have.
- **`grains` is where the numbers are right.** A number that counts customers at the start of a month is wrong at any
  other grain, so its model allows only `month`, and another grain is refused. A query with no grain adds the whole
  period into one row, which is the same step as adding up years, so it needs `year` to be allowed. The churn of the
  restaurant example is such a number. After `make demo-warehouse`,
  `ldp metrics query ldp_demo/restaurant/config churn_rate,active_customers --grain month` prints it for every month,
  and the [recipe](docs/recipes.md) for monthly churn from an orders table shows the definition in SQL.
- **The format is for people.** `percent` shows `0.5` as `50.0%` and `currency` has two decimals, on a terminal. `--raw`
  shows what was computed, and `--out FILE`, `--json` and the Python function always give the values as computed.
  `--out` writes CSV, Parquet or JSONL by the ending of the name.
- **The tables are read as the current release holds them.** `--release r1` reads an older one and `--live` reads the
  tables as they are now, as `ldp query` does. The line under the table says which was read. A release pins the data and
  not the definitions, so an old release that is read with a definition that needs a column that it lacks fails with a
  message that says so.
- **The definitions are checked against the tables.** `ldp metrics validate DIR` checks that every table is a gold table
  that exists, that every dimension and the time column are columns, that the time column is a `DATE` or a `TIMESTAMP`,
  that each expression is one plain SQL expression that works on the table, and that every metric is a number. It lists
  every problem and exits with 1. `ldp gold build` runs the same check and cuts no release when metrics do not fit.
- **In code**, `local_data_platform.metrics.query(folder, "episodes,success_rate", by="robot", grain="week")` returns a
  `pyarrow.Table`, and `metrics.compile_for(...)` returns the SQL and its parameters without running anything.

`ldp mcp` has two more tools for the same numbers, `list_metrics` and `query_metric`, see
[MCP server for agents](#mcp-server-for-agents). The gold catalog shows the metrics of each table, in `ldp gold show`,
on the page of `ldp gold docs`, and in its search.

### Open the gold tables in a spreadsheet or a BI tool

A person who works in a spreadsheet or a desktop SQL tool should be able to use a gold table without Python, Iceberg or an
engineer. `ldp gold export` writes one table as a file, and `ldp gold duckdb` writes the whole gold layer as one DuckDB file.
Both read the gold layer as every reader does, so by default it is the current release, `--release r1` reads an older one
and `--live` reads the tables as they are now.

```bash
ldp gold export examples/robot_episodes/data gold_robot_daily daily.csv
ldp gold export examples/robot_episodes/data gold_robot_daily daily.xlsx
```

```text
wrote daily.csv, 16 rows of robots.gold_robot_daily, read from release r1, cut 2026-10-23 11:26:36 UTC, snapshot 1317331787740633770
wrote daily.xlsx, 16 rows of robots.gold_robot_daily, read from release r1, cut 2026-10-23 11:26:36 UTC, snapshot 1317331787740633770
```

The format comes from the end of the name, or from `--format`.

- **CSV, Parquet and JSONL.** The three are written by `datasets.write_rows`, the same function as `ldp datasets export`,
  which has `--format csv` too. A CSV has a header and a line for each row. A list, a struct or a map is written as JSON text,
  bytes as hexadecimal and a number that is not finite as no value. A Parquet file has the table, release and snapshot in its
  metadata, under `ldp.gold`. A name that starts with `s3://` or `gs://` is written there.
- **Excel.** The file needs the `excel` extra, `pip install "local-data-platform[excel]"`, and says which if it is missing.
  The first sheet has the rows, with a bold header that stays in view and a filter. The second sheet is called `about` and
  has the description, owner, grain and key of the table, its release, snapshot id and build time, its rows, and every
  column with a word for its type and its description. A text is always a text and never a formula, even when it starts
  with `=`. A time with a zone is written in UTC, because a sheet has no zones. An integer beyond 2^53 is written as text,
  because a sheet would round it. A table with more than 1,048,575 rows does not fit a sheet, with its header row, and is
  refused before it is read, with a hint to use CSV. A table with more than 16,384 columns and a value of more than 32,767
  characters are refused too, with the same hint, when the file is written, and no file is left.
- **A CSV and a formula.** Excel runs a CSV value that starts with `=`, `+`, `-` or `@` as a formula when a person opens
  the file. LDP writes the values as they are, so use Excel for text that people typed and that you do not trust.
- **A table that the release does not hold.** When the current release was cut before the table existed, the table is read
  as it is, and the line says why. `--release r1` for a table that r1 does not hold is refused.

```bash
ldp gold duckdb examples/robot_episodes/data --out gold.duckdb
```

```text
wrote gold.duckdb, 2 tables and 5 metrics, from release r1, cut 2026-10-23 11:26:36 UTC, 1292.0 KB
```

The file has a copy of every gold table of the release, in the `main` schema under its own name, so `SELECT * FROM
gold_robot_daily` works. It has no schema called `gold`, because DuckDB names a file's database after the file, and
`gold.daily_sales` in a file called `gold.duckdb` would be ambiguous. Three more things are in it.

- **Comments.** `COMMENT ON TABLE` has the description of each table and `COMMENT ON COLUMN` the description of each column,
  from the contract, so a tool that shows comments shows what the data means.
- **`_catalog`.** One row for each table with `table_name`, `description`, `owner`, `grain`, `release`, `snapshot_id` and
  `built_at`, which is when that snapshot was written, in UTC.
- **`_metrics`.** One row for each metric of `metrics.json` with its `metric`, `description`, `owner`, `type`, `format`,
  `table_name`, `definition`, `dimensions`, `time_column` and `grains`. The `definition` is the SQL that computes it from
  its table, such as `sum(dropped_frames) / NULLIF(sum(frames), 0)`. The table is empty when the folder has no metrics file.

Open it in the DuckDB command line, with `duckdb gold.duckdb`, or in the DuckDB UI, with `duckdb -ui gold.duckdb`. The UI
fetches its web pages from ui.duckdb.org, so it needs the network.

```sql
SELECT table_name, owner, grain, release FROM _catalog;
SELECT metric, definition FROM _metrics;
SELECT robot_id, round(avg(success_rate), 2) AS success_rate FROM gold_robot_daily GROUP BY 1 ORDER BY 1;
```

- **It needs no extension.** The file has plain tables, so a DuckDB with no extension reads it, and no `iceberg_scan` is in
  it. It is written in the storage version of DuckDB 1.0, so a DuckDB 1.x opens it as well as a DuckDB 2.0. DuckDB 2.0
  writes a newer version by default, which a DuckDB 1.x cannot open.
- **A reader never sees half a file.** It is written to a temporary path next to the file and renamed. A failure leaves the
  file that was there, and no temporary file.
- **It holds one release.** A gold table that the release does not hold is left out, and the output names it, so that the
  file is as consistent as the release is. With `--live` it holds every table as it is now and `_catalog.release` has no
  value.
- **`ldp gold build DIR --duckdb gold.duckdb`** writes the file after the build cuts a release. When the build cuts none,
  because the tables are at the snapshots of the current release, the file is written only if it is missing or holds
  another release. A build that a gate blocks or that fails leaves the file alone, so the file that readers have is the
  one of the last good release. A file that cannot be written after the release was cut is an error with exit status 1,
  and the release stays cut. `--duckdb` cannot be used with `--no-release`.
- **Windows.** Replacing a file that a program has open fails on Windows. `{release}` in the name is replaced by the release,
  so `--out "gold_{release}.duckdb"` writes a new file for each release and never replaces one.
- **Memory.** A table is read whole before it is written, as `ldp datasets export` does, so a gold table has to fit in memory.

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

A published model also carries what the project says about it. Its description, the description of each column, its
tags, its `owner` and `grain` meta and its layer become the table's [contract](docs/design/v0_1_4.md) and go into the
Iceberg table. A model that is published to the `gold` namespace is a gold table, so it needs an owner, a grain, a
description and a description of every column, and `ldp dbt build` stops before it publishes anything if one is
missing.

- The [warehousing guide](docs/warehousing.md) walks through it on a restaurant chain's Excel and CSV exports, from
  the raw tables to tested models, churn, an incremental load and time travel. `make demo-warehouse` runs the same
  example offline in about 25 seconds.
- [How `ldp dbt` works](docs/design/warehousing.md) has the contract, including the meta keys and the schema rules.

## Orchestration

A workflow file declares the pipelines, dbt models and maintenance of a night once. LDP runs it for the right window, backfills a range of windows, retries what failed, and runs it on a schedule from cron or launchd, with no service. Every run goes by the idempotency key of its window, so a window is never loaded twice, whoever starts it.

```bash
make demo-orchestration     # the restaurant's nightly drops, a bad row, a backfill, a fix and a scheduler tick
```

- The [orchestration guide](docs/orchestration.md) walks through it on the restaurant example, command by command, from a window and its key to a workflow file, a backfill that stops at a bad row, a replaced day, a retried task, launchd and cron, and Airflow, Dagster and Prefect.
- `make demo-orchestration` runs `examples/restaurant/run.py --layer orchestration`. It drops three nights of orders into a folder, with a bad row in the third. `ldp backfill` publishes two days and a data gate holds the third back, with exit status 3. After the fix, the same command runs only the third, and a third run skips every day. `ldp schedule tick` then loads the fourth night, and `ldp runs`, `ldp commits`, `ldp schedule list` and `ldp schedule install` show what happened. It needs no extra, runs the `models` and `gold` tasks when the `dbt` extra is installed, and prints a skip line when it is not.
- The pieces are described above, under [Windows](#windows), [Workflow files](#workflow-files), [Backfill a range of windows](#backfill-a-range-of-windows), [Run on a schedule](#run-on-a-schedule) and [Run from Airflow](#run-from-airflow), and the exit statuses are in the [table](#use-it-from-the-command-line) of the command line section.

## Gold layer

A gold table is a table that people read. It says who owns it, what it means, what one row is and what each column holds, it is checked before it is written, and all the gold tables are published together as one release, so every reader sees the same build. A metric such as the average ticket is defined once. People who do not write code get the tables as a catalog page, an Excel file and one DuckDB file, and an AI agent can be limited to the gold tables.

```bash
make demo-gold     # the restaurant's data mart, a faulty batch that the gate stops, a release, a metric and files for people
```

- The [gold layer guide](docs/gold.md) walks through it on the restaurant example, command by command, from the three layers and a gate that stops a bad batch to the contract of a gold table, a release that a gate can hold back, the catalog, metrics, files for people and `ldp mcp --layer gold`. It has a section for people who are not engineers, and every command in it runs in the tests.
- `make demo-gold` runs `examples/restaurant/run.py --layer gold`. It loads the raw tables, stops a faulty week of orders at the gate with each failed check named, loads the week without the faults, builds the gold tables with dbt, cuts a release with `ldp gold build`, asks for a metric with `ldp metrics query`, and writes an Excel file, a DuckDB file and a catalog page. It needs the `dbt` and `excel` extras, and takes about 10 seconds.
- The commands are described above, under [Table contracts](#table-contracts), [Build the gold tables](#build-the-gold-tables), [Release the gold tables](#release-the-gold-tables), [Browse the gold tables](#browse-the-gold-tables), [Define metrics](#define-metrics) and [Open the gold tables in a spreadsheet or a BI tool](#open-the-gold-tables-in-a-spreadsheet-or-a-bi-tool).

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
| `list_metrics()` | The metrics that the `metrics.json` files of the `--config` folders define, each with what it means, its owner, format, table, dimensions and grains |
| `query_metric(metrics, by, grain, filters, start, end, max_rows)` | Some metrics of one table, computed from the current release of the gold tables as the definitions say, with every value bound as a parameter |

The guardrails are on by default. A query must be exactly one `SELECT` or `WITH` statement, as
DuckDB's parser sees it, so `COPY`, `ATTACH`, `INSTALL`, `SET`, `read_csv('/etc/passwd')` and
multi-statement SQL are rejected. DuckDB runs with external access off and file access limited to
the served tables' folders, and its configuration is locked once the tables are registered.
Results are capped in rows and time, `--allow` limits the tables, and every call is audited: the
tool, the SQL, row counts, duration and any error, never the result data. Records go to a JSONL file
as each call ends, and to the `_ldp.audit` Iceberg table in the first served catalog when the
server stops (`--no-iceberg-audit` turns that off).
`query_metric` runs in the same sandbox as `query`, with the same row cap, timeout and audit, and its audit record
has the arguments and the compiled SQL. It reads the current release of the gold tables, and `query` reads the tables
as they are now. `python examples/agent_client.py` builds a small lake with a gold table, three metrics and a release,
and calls both tools. `make demo-agent` runs a scripted client against the demo table. See
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
datasets.export(train, "exports/humanoid_train_v1.parquet")    # or format="jsonl" or "csv"
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
make demo-all     # every demo; demo-warehouse and demo-gold need the dbt and excel extras, demo-spark and demo-rest need scala-cli
make demo-orchestration   # the restaurant's nightly workflow, a backfill and a scheduler tick
make demo-gold    # the restaurant data mart: a blocked batch, gold tables, a release, a metric and files for people
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
  `examples/`, so the warehousing and orchestration guides and `make demo-warehouse`, `make demo-orchestration` and
  `make demo-gold` need a clone.
- **Tasks run one at a time.** A workflow runs its tasks in order and a backfill its windows in order, which also
  respects DuckDB's single writer. There are no parallel tasks.
- **The scheduler is a tick, not a daemon.** `ldp schedule tick` has to be called by cron or launchd, and there is no
  foreground loop and no systemd or Windows Task Scheduler recipe. A window that failed is run again by every tick, every
  5 minutes under the recipes, with no wait between ticks. `retries` are the retries inside one tick.
- **A dbt task builds its whole selection for every window.** dbt gets `window_start` and `window_end`, but only a
  model that reads them is limited to the window, and the models of the restaurant example are not.
- **Airflow runs one config for each task.** DAGs built from workflow files, an `LdpTaskOperator` and managed Airflow
  recipes are planned for 0.2.0.
- **A release covers one namespace of one warehouse.** `ldp gold build` refuses a folder whose gold tables are in more
  than one place, and says to give each place its own folder of configs.
- **A metrics query reads one table.** `ldp metrics query` and the `query_metric` tool refuse metrics of two tables in one
  question, so ask for them in separate queries.

## Roadmap

From 0.1.1 on, the package version equals the milestone number. Each milestone adds the next layer
of a data platform, and every layer runs on a laptop first. 0.1.4 Self Serving Gold Layer is released, and these
are the milestones after it:

| Version | Milestone | What it adds |
|---|---|---|
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
