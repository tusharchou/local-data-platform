# Quickstart

This guide gets a local Iceberg lakehouse running in a few minutes, then shows how to load your
own data. You need Python 3.12 or newer and nothing else: no server, no cloud account.

## 1. Install

Install `local-data-platform` from PyPI, preferably in a virtual environment:

```bash
pip install "local-data-platform[duckdb]>=0.1.1"
ldp --version
```

To work on the library itself, install it from a clone of the repo. `make install` creates `.venv`
and installs the package in editable mode with the `dev` and `docs` extras:

```bash
git clone https://github.com/tusharchou/local-data-platform.git
cd local-data-platform
make install
source .venv/bin/activate
ldp --version
```

The extras are optional:

| Extra | Adds | You need it for |
|---|---|---|
| `duckdb` | DuckDB | SQL over Iceberg tables (`DuckDBEngine`, `ldp query`, the demo's SQL step) |
| `mcp` | The MCP SDK and DuckDB | The read-only MCP server for agents, `ldp mcp` ([MCP server for agents](agents.md)) |
| `s3` | boto3 | SigV4-signed REST catalogs on AWS, such as S3 Tables; `s3://` IO itself needs nothing extra ([Catalogs and object storage](catalogs.md)) |
| `glue` | `pyiceberg[glue]` (boto3) | The AWS Glue catalog ([Catalogs and object storage](catalogs.md)) |
| `bigquery` | Google Cloud BigQuery client | BigQuery as a source |
| `dev` | pytest, flake8, build, DuckDB, `moto[server]`, the MCP SDK | Working on the library |
| `docs` | MkDocs and plugins | Building these docs |
| `spark` | PySpark | The experimental `SparkEngine`, which also needs a JDK 17+ ([Spark](spark.md)) |

## 2. Run the demo

```bash
ldp demo --workdir ldp_demo
```

The demo generates 1,000 synthetic taxi rides from a fixed seed, so every run produces the same
data. It then goes through these steps, using only the public API:

| Step | What it shows |
|---|---|
| 1. Config from JSON | Loads the dataset config with `Config.from_json`; its paths resolve against the config's folder |
| 2. Append, then overwrite | Appending the same batch twice doubles the rows. Overwriting twice leaves the count unchanged: the load is idempotent |
| 3. Upsert | Changed rows are updated and new rows inserted, keyed on `join_cols` |
| 4. Partitioning | A table partitioned by `day(pickup_ts)`, and a one-day filter that reads one data file of seven |
| 5. Quality checks | A clean batch passes. A bad batch fails its checks and is blocked before anything is written |
| 6. DuckDB SQL | SQL over the Iceberg table |
| 7. Time travel | Reads the table as of an earlier snapshot |
| 8. Export | Writes the table back to CSV |

Everything it writes (data, the SQLite catalog and the Iceberg warehouse) goes into the
`--workdir` folder. Running it again into the same folder replaces what the demo wrote there; it
refuses a non-empty folder it didn't create. `--rows N` (default 1000, at least 20) and `--seed N`
(default 42) change the synthetic data.

Step 4 needs the `pyiceberg-core` package to write `day()` partitions on pyiceberg 0.10 and later.
It's a dependency of `ldp`, so a normal install has it; if it's missing, the demo says so and
partitions by `identity(pickup_date)` instead.

## 3. Load your own data

Put a CSV next to a config file:

```text
my_project/
├── rides.json
└── data/
    └── rides.csv      # ride_id, pickup_ts, fare, city
```

`rides.json`:

```json
{
  "identifier": "rides",
  "metadata": {
    "source": {"name": "rides", "format": "CSV", "path": "data/rides.csv"},
    "target": {
      "name": "rides",
      "format": "ICEBERG",
      "catalog": {"identifier": "my_project", "warehouse_path": "warehouse"},
      "write_mode": "overwrite"
    },
    "quality": {
      "on_failure": "fail",
      "checks": [
        {"check": "row_count", "min": 1},
        {"check": "not_null", "columns": ["ride_id"]},
        {"check": "unique", "columns": ["ride_id"]},
        {"check": "accepted_values", "column": "city", "values": ["NYC", "BKK"]}
      ]
    }
  }
}
```

Relative paths resolve against the folder holding `rides.json`, so the command works from any
directory:

```bash
ldp run my_project/rides.json
ldp run my_project/rides.json            # again: overwrite keeps the row count the same
ldp snapshots my_project/rides.json
ldp query my_project/rides.json "SELECT city, count(*) AS rides FROM rides GROUP BY city"
```

`ldp query` registers the target table under its `target.name`, here `rides`, and prints at most
100 rows (`--max-rows N` changes that). `ldp snapshots` and `ldp query` only read: on a `local`
catalog, before the first `ldp run`, they fail with "does not exist" and create nothing.

### Choose a write mode

| `write_mode` | Behaviour | Use it when |
|---|---|---|
| `append` (default) | Adds the rows. Running twice duplicates them | Each run brings only new rows |
| `overwrite` | Replaces the table's rows | Each run brings the full dataset |
| `upsert` | Updates rows whose `join_cols` match and inserts the rest. It needs `join_cols`, and every batch must have all of the table's columns | Each run brings new and changed rows |

`ldp run CONFIG --mode MODE` overrides the config for one run.

### Partition the table

Add `partition_by` to an Iceberg target. Each item names a column and a transform: `identity`,
`year`, `month`, `day`, `hour`, `bucket[N]` or `truncate[W]`.

```json
"partition_by": [{"column": "pickup_ts", "transform": "day"}]
```

### Quality checks

Checks run after any transforms and before the write. With `"on_failure": "fail"` a failing check
raises `DataQualityError` and nothing is written. With `"warn"` the failures are logged and the
write goes ahead.

| `check` | Keys | Passes when |
|---|---|---|
| `row_count` | `min`, `max`, `equals` | The row count is in range |
| `not_null` | `columns` | None of the columns has a null |
| `unique` | `columns` | The combination of columns is unique |
| `accepted_values` | `column`, `values` | Every value is in the list |
| `range` | `column`, `min`, `max` | Every value is within the bounds |
| `freshness` | `column`, `max_age_hours` | The newest timestamp is recent enough |
| `schema` | `columns` (a list of names, or names mapped to pyarrow types) | The columns exist, with those types |

A check on a missing column fails; it doesn't raise an exception. The full schema is in the
[v0.1.1 hardening contract](design/v0_1_1.md#config-schema).

## 4. Use the Python API

The same config, from Python:

```python
from local_data_platform import Config
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.exceptions import DataQualityError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.registry import create_pipeline

config = Config.from_json("my_project/rides.json")

try:
    result = create_pipeline(config).run()
except DataQualityError as error:
    print(error.report.summary())
    raise

print(f"read {result.rows_read}, wrote {result.rows_written} in {result.duration_s:.2f}s")

table = Iceberg("rides", config.target["catalog"], base_dir=config.base_dir)
for snapshot in table.snapshots():
    print(snapshot["snapshot_id"], snapshot["operation"])

with DuckDBEngine() as duck:
    duck.register_iceberg(table, "rides")
    print(duck.query("SELECT city, avg(fare) AS avg_fare FROM rides GROUP BY city").to_pylist())
```

`create_pipeline` picks the pipeline class from the config's source format, target format and
engine. [Pipeline registry and factory](design/factory_registry.md) explains why, and how to add
your own.

The built-in routes are:

| Source | Target | Pipeline |
|---|---|---|
| CSV | Iceberg | `CSVToIceberg` |
| Parquet | Iceberg | `ParquetToIceberg` |
| Iceberg | CSV | `IcebergToCSV` |
| Iceberg | Parquet | `IcebergToParquet` |
| BigQuery (a `JSON` query file with the `BIGQUERY` engine) | CSV | `BigQueryToCSV` |

`ldp pipelines` lists them.

## 5. Beyond the laptop

The same config runs against other catalogs and storage by changing only its `catalog` block and
paths. These come from the [v0.1.1 platform contract](design/v0_1_1_platform.md), each with its
own guide. They work from Python, through `ldp run`, `ldp snapshots` and `ldp query`, and through
their own `ldp` subcommands (`ldp --help` lists them).

- **Another catalog.** Set `"type"` in the catalog block to `sql` (any SQLAlchemy URI), `rest` (any
  Iceberg REST catalog) or `glue`. Tokens come from environment variables that the config names,
  never from the config itself. `ldp catalog test my_project/rides.json` (or
  `create_catalog(spec).list_namespaces()`) checks the connection. See
  [Catalogs and object storage](catalogs.md).
- **Object storage.** CSV, Parquet and JSON paths can be `s3://` or `gs://`.
- **Several writers.** Pass a `CommitContext` to `Iceberg.put(..., commit=)`, or to
  `Pipeline.run(commit=)`, for the staged publish protocol: a re-run with the same idempotency key
  writes nothing. See [Exactly-once writes](exactly_once.md).
- **Run history.** Add `"observability": {"sinks": ["iceberg", "jsonl"]}` to `metadata` and every
  run records its events in the `_ldp.runs` and `_ldp.quality_results` tables, which any engine can
  query; `ldp runs my_project/rides.json` lists the runs. See
  [Run events and the `_ldp` namespace](observability.md).
- **Agents.** `ldp mcp --config my_project/rides.json` serves the tables to an MCP client,
  read-only and audited. See [MCP server for agents](agents.md).
- **Reproducible training data.** `datasets.pin` (or `ldp datasets pin my_project/rides.json NAME`)
  pins a table to a snapshot, so the same rows come back later. See
  [Robot-episode datasets](robotics.md).
- **Housekeeping.** `ldp maintain my_project/rides.json` (or
  `maintenance.expire_snapshots(table, older_than=..., dry_run=True)` and
  `maintenance.find_orphans(table)`) shows what snapshot expiry and orphan cleanup would do, without
  changing anything until you add `--apply`.

## 6. Next steps

- From a clone of the repo, try the other demos: `make demo-robotics`, `make demo-agent`, and, with
  [scala-cli](https://scala-cli.virtuslab.org) installed, `make demo-spark` and `make demo-rest`.
- Run the real-world examples in
  [`examples/`](https://github.com/tusharchou/local-data-platform/tree/main/examples): NEAR
  blockchain transactions and NYC yellow-taxi trips.
- Read the [changelog](https://github.com/tusharchou/local-data-platform/blob/main/CHANGELOG.md)
  for what changed in 0.1.1, and the [roadmap](roadmap.md) for what each later version adds.
- Browse the [API docs](api.md).
