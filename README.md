# Local Data Platform

**local-data-platform** (`ldp`) is a Python library for running an Apache Iceberg lakehouse on your
laptop, and for taking the same pipelines to a shared catalog and object storage when you need to.
You describe a dataset in a JSON config, and `ldp` loads it into an Iceberg table, runs data quality
checks before the write, and lets you query the table with DuckDB SQL and read earlier snapshots.
The default setup needs no server and no cloud account.

It's for learning how a lakehouse works, for building and testing pipelines locally before you pay
for cloud infrastructure, and for giving agents and notebooks safe, reproducible access to the
tables you build.

> **Status.** This is version 0.2.0, not yet on PyPI. It carries the 0.1.1 hardening work and
> the 0.2.0 features ("multi-writer, any catalog, object storage, agent-ready", contract in
> [`docs/design/v0_2_0.md`](docs/design/v0_2_0.md)), which work from Python and through the
> [0.2.0 `ldp` commands](#020-commands). Only 0.1.0 is on PyPI, and it has a different module
> layout, so install from source as shown below.

## Quickstart

`ldp` needs Python 3.12 or newer. Install it from a clone of this repo:

```bash
git clone https://github.com/tusharchou/local-data-platform.git
cd local-data-platform
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[duckdb]"

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
| `make demo-spark` | The Scala Spark job reading the demo table and writing an aggregate that pyiceberg reads back (needs scala-cli) |
| `make demo-rest` | The demo config against an Iceberg REST catalog started locally on scala-cli's JVM: `ldp catalog test`, two runs, the snapshots and a query (needs scala-cli) |
| `make demo-all` | All of them; the two JVM demos are skipped when scala-cli isn't installed |

### Extras

| Extra | Adds | You need it for |
|---|---|---|
| `duckdb` | DuckDB | SQL over Iceberg tables (`DuckDBEngine`, `ldp query`, the demo's SQL step) |
| `mcp` | The MCP SDK and DuckDB | The read-only MCP server for agents (`ldp mcp`) |
| `s3` | boto3 | SigV4-signed REST catalogs on AWS, such as S3 Tables. `s3://` IO itself needs nothing extra: pyarrow does it |
| `glue` | `pyiceberg[glue]` (boto3) | The AWS Glue catalog |
| `bigquery` | Google Cloud BigQuery client | BigQuery as a source |
| `spark` | PySpark | The experimental `SparkEngine`, which also needs a JDK 17+ |
| `dev` | pytest, flake8, build, DuckDB, `moto[server]`, the MCP SDK | Working on the library |
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
ldp snapshots rides.json                         # list the target table's snapshots
ldp query rides.json "SELECT count(*) FROM rides"   # SQL over the target, named after target.name
ldp query rides.json "SELECT * FROM rides" --max-rows 20   # print at most 20 rows (default 100)
ldp demo --workdir /tmp/ldp_demo                 # also --rows N (default 1000) and --seed N (default 42)
```

Every command exits with 0 on success and 1 on error. Add `-v` to see a traceback. The commands
that read a table (`snapshots`, `query`, `commits`, `maintain`, `datasets pin`, `catalog test`) fail
without creating anything when the config's catalog is a SQLite file that doesn't exist yet (a
`local` catalog, or `sql` on SQLite). `ldp run`, `ldp snapshots` and `ldp query` also work with the
other [catalog types](#catalogs-and-object-storage); they have been run against `sql` (SQLite) and
`rest` catalogs.

### 0.2.0 commands

`ldp --help` lists them, and `ldp <command> --help` shows each one's options. Each is also a Python
call:

| Command | What it does | From Python |
|---|---|---|
| `ldp catalog test SPEC` | Connect to a catalog (a spec file, or a config's catalog block) and list its namespaces and tables | `catalog.provider.create_catalog(spec).list_namespaces()` |
| `ldp schema` | Print the `ldp/v1` JSON Schema for configs | `spec.json_schema()` |
| `ldp plan CONFIG [--window START/END] [--json]` | Validate a config (or a folder of them) and print its spec hash, route, target, sinks and idempotency key | `spec.validate_spec(data)`, `spec.plan(config, window=...)` |
| `ldp runs CONFIG [--run RUN_ID] [--events]` | List the runs a config's sinks recorded | `events.summarize_runs(events.read_jsonl(path))` or `read_iceberg_events(catalog)` |
| `ldp commits CONFIG [--key KEY] [--branches]` | List the staged publishes on a table, newest first | `format.iceberg.commit.commit_log(iceberg.table())` and `staged_branches(...)` |
| `ldp datasets pin\|list\|export` | Pin, list and export dataset versions | `datasets.pin`, `list_versions`, `export` |
| `ldp maintain CONFIG [--expire DAYS] [--orphans] [--apply]` | Expire snapshots and find orphan files; a dry run unless `--apply` | `maintenance.expire_snapshots`, `find_orphans`, `remove_orphans` |
| `ldp mcp --config CONFIG ...` | Serve tables to an agent over MCP (stdio) | `python -m local_data_platform.mcp_server ...`, same options |
| `ldp spark CONFIG` | Run the Scala Spark job on a config's table (needs scala-cli) | `engine.spark.ScalaSparkJob` |

## Config schema

A config has an `identifier`, optional descriptive fields (`who`, `what`, `where`, `when`, `how`)
and a `metadata` block:

| Key | What it holds |
|---|---|
| `metadata.source` | `name`, `format` (`CSV`, `PARQUET`, `ICEBERG` or `JSON`), and a `path` (local, `s3://` or `gs://`) or, for Iceberg, a `catalog` |
| `metadata.target` | The same, plus for Iceberg `write_mode` (`append`, `overwrite` or `upsert`), `join_cols` for upserts, and `partition_by` |
| `metadata.target.catalog` | `type` (`local` by default, `sql`, `rest` or `glue`) and that type's keys; see [Catalogs](#catalogs-and-object-storage) |
| `metadata.quality` | `on_failure` (`fail` or `warn`) and a list of `checks`: `row_count`, `not_null`, `unique`, `accepted_values`, `range`, `freshness`, `schema`, and the temporal checks `monotonic`, `max_skew`, `rate_below` and `max_gap` |
| `metadata.observability` | Where run events go, for example `{"sinks": ["iceberg", "jsonl"]}`. The default sends them nowhere |

Partition transforms are `identity`, `year`, `month`, `day`, `hour`, `bucket[N]` and
`truncate[W]`. On pyiceberg 0.10 and later, writing with any transform except `identity` needs the
`pyiceberg-core` package, which `ldp` depends on, so a normal install has it. If it's missing, the
demo's step 4 says so and partitions by `identity(pickup_date)` instead. The full schema, with an
example of every check, is in the [v0.1.1 design contract](docs/design/v0_1_1.md#config-schema);
`local_data_platform.spec.json_schema()` returns it as JSON Schema, and `ldp schema` prints it. The configs in
[`examples/`](examples/README.md) are real ones you can run.

## Catalogs and object storage

The catalog block's `type` picks the catalog. It defaults to `local`, so every 0.1.1 config keeps
working unchanged.

| `type` | Keys | What you get |
|---|---|---|
| `local` (alias `LocalIceberg`) | `identifier`, `warehouse_path` | A SQLite catalog file and a warehouse folder, as in 0.1.1 |
| `sql` (alias `sqlite`) | `uri` (a SQLAlchemy URI such as `sqlite:///…` or `postgresql+psycopg://…`), `warehouse`, `name`, `password_env` | pyiceberg's `SqlCatalog`. Its tables are the ones Iceberg's Java `JdbcCatalog` uses, so Spark can share it |
| `rest` | `uri`, `warehouse`, `name`, `token_env`, `credential_env`, and optional `properties` (such as `s3.endpoint`) | pyiceberg's `RestCatalog`, for any Iceberg REST catalog, such as Polaris, Nessie or Lakekeeper |
| `glue` | `name`, `warehouse`, and optional `properties` | pyiceberg's `GlueCatalog` (the `glue` extra). AWS credentials come from boto3's default chain |

What has been tested: `local` and `sql` on SQLite; `rest` against Apache Iceberg's REST catalog
test server (`tools/rest_fixture`, opt-in); `glue` against a local moto server. Postgres and
hosted REST catalogs (Polaris, S3 Tables, Unity and so on) are not tested yet.

The namespace tables live in comes from `identifier` for `local` catalogs (as in 0.1.1), and from
`namespace`, falling back to `identifier`, for every other type.
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

A plain `Iceberg.put(df, mode)` is **direct mode**. It keeps the 0.1.1 semantics (appending twice
gives duplicates, overwriting twice doesn't) with three fixes:

- **One commit per write.** Adding new columns and writing the data happen in one transaction.
- **Counts from metadata.** `rows_before` and `rows_after` come from the snapshots' `total-records`,
  not from a scan.
- **A lock on local catalogs.** `overwrite` and `upsert` take an exclusive file lock,
  `<warehouse>/.ldp/locks/<namespace>.<table>.lock`, so two processes on one laptop can't race.

Direct mode on a shared remote catalog is a single-writer mode. For several writers, pass a
`CommitContext` and `put` switches to the **staged publish protocol**: it writes to a private
branch, checks the staged snapshot, then fast-forwards `main` in one catalog compare-and-swap that
asserts both `main` and the branch. The idempotency key is stored on the snapshot as
`ldp.idempotency-key` (with `ldp.run-id` and `ldp.attempt`), so re-running a key finds the
published snapshot and writes nothing:

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
| `NullSink` | Nothing (the default, so 0.1.1 behaviour is unchanged) |
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
If the extension can't load, it falls back to the 0.1.1 in-memory scan with a warning. It never
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

`attach_rest(name, uri, warehouse, token_env=None)` attaches a whole REST catalog to DuckDB.
`engine.router` estimates how many bytes a scan reads from the manifests, after partition pruning,
and `choose_engine` picks DuckDB below 50 GiB by default, and PySpark above that when it's
installed. No pipeline calls the router yet.

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
make demo-all     # every demo; demo-spark and demo-rest need scala-cli
make spark-test   # opt-in Spark integration tests (LDP_RUN_SPARK=1, scala-cli)
make rest-test    # opt-in REST catalog integration tests (LDP_RUN_REST=1, scala-cli)
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

## Known limitations and roadmap

- **Not on PyPI yet.** PyPI has only 0.1.0, which has a different module layout. Install 0.2.0
  from source.
- **The engine router is unused.** Nothing calls `engine.router` yet.
- **Remote catalogs are lightly tested.** `rest` is tested against Iceberg's REST test server and
  `glue` against moto. Postgres, hosted REST catalogs and GCS have no integration tests yet.
- **Direct writes to a shared catalog are single-writer.** Several writers need the staged protocol
  (`commit=`). The local file lock only covers processes on one machine.
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

Likely next steps, with no dates committed: releasing 0.2.0, then streaming and incremental reads,
compaction, and Spark covered by required CI. The
[SaaS architecture](docs/design/saas_architecture.md) sketches a longer-term roadmap.

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
