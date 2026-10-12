# Scala Spark job

`IcebergJob.scala` queries a local-data-platform Iceberg table with Spark SQL and writes an
aggregate back as a new Iceberg table. It opens the same SQLite catalog file that the Python
library (`LocalIcebergCatalog`, a pyiceberg `SqlCatalog`) writes, so a table loaded from Python is
visible to Spark, and the table Spark writes can be read again from Python.

The project is built and run with [scala-cli](https://scala-cli.virtuslab.org). scala-cli downloads
a JDK (Temurin 17), Scala 2.13.17, Spark 4.1.3, the Iceberg 1.12.0 Spark runtime and the SQLite
JDBC driver the first time you run it (about 500 MB, cached under `~/Library/Caches/Coursier` on
macOS or `~/.cache/coursier` on Linux). No system JDK is needed.

## Install scala-cli

```bash
brew install Virtuslab/scala-cli/scala-cli   # prebuilt binary, no dependencies
# or: brew install scala-cli, or see https://scala-cli.virtuslab.org/install
scala-cli version
```

## Run it

Write a table from Python first, for example with `ldp demo` or any pipeline whose target is
`{"identifier": "nyc", "warehouse_path": "warehouse"}`. Then, from the repository root:

```bash
scala-cli run spark -- \
  --catalog-name nyc \
  --catalog-db  warehouse/nyc_catalog.db \
  --warehouse   warehouse \
  --namespace   nyc \
  --table       rides \
  --output-table rides_by_city_day
```

| Argument | Meaning |
|---|---|
| `--catalog-name` | pyiceberg catalog name. For `LocalIcebergCatalog` this is the config `identifier`. |
| `--catalog-db` | The SQLite catalog file, `<warehouse_path>/<identifier>_catalog.db`. |
| `--warehouse` | The config `warehouse_path` (a folder or a `file://` URI). |
| `--namespace` | The Iceberg namespace, also the config `identifier`. |
| `--table` | Source table. |
| `--output-table` | Table to write the aggregate to. Default `<table>_spark_summary`. Replaced if it exists. |
| `--show-rows` | Rows to print per result. Default 10. |
| `--conf key=value` | Extra Spark setting. Repeatable. |
| `--print-conf` | Print the Spark settings and exit, without starting Spark. |

The job:

1. counts the rows of `<catalog>.<namespace>.<table>`;
2. when the table has a timestamp or date column (`pickup_ts`, or the first temporal column), a
   string `city` column and a numeric amount column (`fare`, `fare_amount`, `total_amount`,
   `revenue`, `amount` or `price`), computes rows and revenue by city per day; otherwise prints the
   first rows and counts them;
3. prints the snapshot history from the table's `snapshots` metadata table;
4. writes the aggregate with `CREATE OR REPLACE TABLE <catalog>.<namespace>.<output-table> USING
   iceberg AS SELECT ...`;
5. prints `LDP_SPARK_RESULT source=... source_rows=... target=... target_rows=... mode=...` as its
   last line, for scripts.

It exits with 0 on success, 1 when the job fails (for example a missing table, with the tables that
do exist listed) and 2 on bad arguments.

From Python, `local_data_platform.engine.spark.ScalaSparkJob` builds and runs this command from a
dataset config's catalog dict:

```python
from local_data_platform.engine.spark import ScalaSparkJob, parse_result

job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": "warehouse"})
result = job.run("rides", output_table="rides_by_city_day")
print(parse_result(result.stdout))
```

## Files

| File | Purpose |
|---|---|
| `project.scala` | scala-cli directives: Scala, JVM, dependencies, the `--add-opens` flags Spark needs on Java 17, main class |
| `IcebergJob.scala` | The job: `JobConfig` (argument parsing), `CatalogConf` (Spark settings), `RevenueColumns` (column detection), `IcebergJob` |
| `resources/log4j2.properties` | Logs Spark, Hadoop and Iceberg at WARN to stderr, so stdout holds only results |

`scala-cli compile spark` type-checks the job; `scala-cli run spark -- --help` prints the usage.
Build output goes to `spark/.scala-build/`, which is git-ignored.

See [docs/spark.md](../docs/spark.md) for how the shared catalog works and its limits.
