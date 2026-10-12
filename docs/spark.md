# Spark

local-data-platform stores tables as Apache Iceberg in a SQLite catalog on your laptop. Spark can
read and write those same tables, in two ways:

| Way | What you need | Use it for |
|---|---|---|
| **Scala Spark job** (`spark/IcebergJob.scala`), run with scala-cli, or from Python with `ScalaSparkJob` | `scala-cli` only; it fetches the JDK and Spark | A compiled Spark SQL job: count, revenue by city per day, snapshot history, and a new aggregate table written back |
| **PySpark engine** (`SparkEngine`) | `pip install "local-data-platform[spark]"` and a JDK 17+ | Spark SQL from Python, results as `pyarrow.Table`, inserts through Spark |

Both are in `local_data_platform.engine.spark`. They cover the GitHub milestone 0.1.1 item "query the
tables with Spark SQL" (issue #22), inserting through Spark (issue #21) and Spark as a query engine
(issue #12), and `SupportedEngine.PYSPARK` now names `SparkEngine`.

## Versions

Checked against Maven Central and PyPI on 2026-09-30.

| Component | Version | Why |
|---|---|---|
| Spark | 4.1.3 | Newest Spark with an Iceberg runtime. Spark 4.2.0 is released, but there is no `iceberg-spark-runtime-4.2` yet. |
| Scala | 2.13.17 | The Scala version Spark 4.1.3 is built with |
| Java | 17 (Temurin 17.0.20.1, provisioned by scala-cli) | Spark 4 needs Java 17 or later |
| Iceberg Spark runtime | `org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.12.0` | Newest runtime for Spark 4.1 |
| SQLite JDBC | `org.xerial:sqlite-jdbc:3.53.4.0` | Lets Iceberg's `JdbcCatalog` open the pyiceberg SQLite file |
| scala-cli | 1.17.1 | Builds and runs the Scala job |
| PySpark | 4.1.3 | Matches the Spark version; supports Python 3.10 to 3.14 |

The versions live in `spark/project.scala` and in `SPARK_VERSION`, `ICEBERG_VERSION` and
`SQLITE_JDBC_VERSION` in `local_data_platform.engine.spark`; a unit test fails if they drift apart.
`SparkEngine` picks the Iceberg runtime that matches the installed PySpark (`spark_packages()`), so
PySpark 4.0 gets `iceberg-spark-runtime-4.0_2.13`, for example. Iceberg 1.12.0 ships runtimes for
Spark 3.5, 4.0 and 4.1 only. PySpark 4.2, the newest on PyPI, has no Iceberg runtime yet, so
install `pyspark>=4.0,<4.2`: with any other version and no `packages=`, `SparkEngine` raises
`EngineNotFound` instead of failing inside the JVM. PySpark 3.4 works with
`packages=` naming `iceberg-spark-runtime-3.4_2.12:1.11.0`, the last runtime for it. The tested
combination is PySpark 4.1.3 on Python 3.14.

## How the shared catalog works

`LocalIcebergCatalog(name, path)` is a pyiceberg `SqlCatalog` with:

- the catalog database at `sqlite:///<abs path>/<name>_catalog.db`,
- the warehouse at `file://<abs path>`,
- tables named `<namespace>.<table>`. A dataset config's catalog dict
  `{"identifier": "nyc", "warehouse_path": "warehouse"}` uses `nyc` as both the catalog name and
  the namespace.

pyiceberg's `SqlCatalog` and Iceberg's Java `JdbcCatalog` use the same two tables. What pyiceberg
0.12 creates:

```sql
CREATE TABLE iceberg_tables (
  catalog_name VARCHAR(255) NOT NULL, table_namespace VARCHAR(255) NOT NULL,
  table_name VARCHAR(255) NOT NULL, metadata_location VARCHAR(1000),
  previous_metadata_location VARCHAR(1000), iceberg_type VARCHAR(5),
  PRIMARY KEY (catalog_name, table_namespace, table_name));
CREATE TABLE iceberg_namespace_properties (
  catalog_name VARCHAR(255) NOT NULL, namespace VARCHAR(255) NOT NULL,
  property_key VARCHAR(255) NOT NULL, property_value VARCHAR(1000) NOT NULL,
  PRIMARY KEY (catalog_name, namespace, property_key));
```

The `iceberg_type` column is `JdbcCatalog`'s schema version `V1`. Rows look like
`('nyc', 'nyc', 'rides', 'file:///…/metadata/00002-….metadata.json', …, 'TABLE')`, and pyiceberg
marks a namespace with the property `exists=true`.

So Spark registers an Iceberg `SparkCatalog` backed by `JdbcCatalog` on the same file.
`spark_catalog_conf(catalog_name, catalog_db, warehouse)` returns the settings, and the Scala job
builds the same map (`ScalaSparkJob.print_conf()` shows it; the integration test checks the two are
equal):

```properties
spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions
spark.sql.catalog.nyc=org.apache.iceberg.spark.SparkCatalog
spark.sql.catalog.nyc.catalog-impl=org.apache.iceberg.jdbc.JdbcCatalog
spark.sql.catalog.nyc.uri=jdbc:sqlite:/abs/path/warehouse/nyc_catalog.db
spark.sql.catalog.nyc.warehouse=file:///abs/path/warehouse
spark.sql.catalog.nyc.jdbc.schema-version=V1
spark.sql.defaultCatalog=nyc
spark.sql.session.timeZone=UTC
```

Three details matter:

- **The Spark catalog name must equal the pyiceberg catalog name.** `JdbcCatalog` filters every
  query on `catalog_name`, and it uses the Spark catalog name for it. Under any other name Spark sees
  an empty catalog.
- **`jdbc.schema-version=V1`.** With an existing file, `JdbcCatalog` detects the `iceberg_type`
  column by itself. With a fresh file, `V1` makes Spark create the same layout pyiceberg expects.
  Tables Spark creates get `iceberg_type = 'TABLE'`, so pyiceberg lists them.
- **Paths are absolute with symlinks resolved**, as `LocalIcebergCatalog` resolves them (on macOS
  `/tmp` is `/private/tmp`). Table metadata stores absolute `file://` locations, so Python and Spark
  must agree on them.

The session time zone is UTC, so casting a `timestamptz` column to a date gives the same day in
Python and Spark. Plain `timestamp` columns (what pyarrow timestamps without a time zone become)
show up in Spark as `timestamp_ntz` and are not shifted.

### Name tables with three parts in Spark

The catalog and the namespace are both called `nyc`, and Spark reads `nyc.rides` as
"table `rides` at the root of catalog `nyc`", which doesn't exist (`Namespace does not exist`).
Write `nyc.nyc.rides`, or make the namespace current with `USE nyc.nyc` and write `rides`.
`SparkEngine` runs that `USE` for you, and `SparkEngine.qualified("rides")` returns
`` `nyc`.`nyc`.`rides` ``. Metadata tables always need the full name:
`nyc.nyc.rides.snapshots`, not `rides.snapshots`.

## How to run

### Scala Spark job

```bash
brew install Virtuslab/scala-cli/scala-cli      # once
scala-cli run spark -- --catalog-name nyc --catalog-db warehouse/nyc_catalog.db \
  --warehouse warehouse --namespace nyc --table rides --output-table rides_by_city_day
```

or from Python:

```python
from local_data_platform.engine.spark import ScalaSparkJob, parse_result

job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": "warehouse"}, base_dir="path/to/config/folder")
result = job.run("rides", output_table="rides_by_city_day")    # subprocess.CompletedProcess
parse_result(result.stdout)
# {'source': '`nyc`.`nyc`.`rides`', 'source_rows': '1000', 'target': '`nyc`.`nyc`.`rides_by_city_day`',
#  'target_rows': '28', 'mode': 'revenue_by_city_day'}
```

`ScalaSparkJob` finds `scala-cli` on `PATH` or in `~/homebrew/bin`, `/opt/homebrew/bin`,
`/usr/local/bin`, `~/.local/bin` and the Coursier bin folders, and raises `EngineNotFound` with an
install hint when it is missing. A failed job raises `SparkJobError` with the job's stderr. See
[`spark/README.md`](https://github.com/tusharchou/local-data-platform/blob/main/spark/README.md) for
every argument.

### PySpark

```python
from local_data_platform.engine.spark import SparkEngine, scala_cli_java_home

catalog = {"identifier": "nyc", "warehouse_path": "warehouse"}
with SparkEngine(catalog, java_home=scala_cli_java_home()) as spark:   # or rely on JAVA_HOME
    spark.query("SELECT city, count(*) AS rides, sum(fare) AS revenue FROM rides GROUP BY city")
    spark.query(f"SELECT * FROM {spark.qualified('rides')}.snapshots")
    spark.put(new_rides, "rides")           # append a pyarrow.Table through Spark
    spark.query("INSERT INTO rides VALUES (5002, TIMESTAMP_NTZ'2026-09-08 10:00:00', 'BKK', 1.0, 4.0)")
```

- `query(sql)` returns a `pyarrow.Table` (through `DataFrame.toArrow()` on PySpark 4, or pandas on
  older versions). DDL and DML return an empty table.
- `put(df, table)` appends a `pyarrow.Table` with `DataFrameWriterV2`, creating the table if it
  doesn't exist.
- `SparkEngine(catalog=LocalIcebergCatalog(...))` works too.
- The first session downloads the Iceberg runtime and SQLite driver through `spark.jars.packages`
  into `~/.ivy2.5.2`.
- PySpark needs a JDK. `SparkEngine` uses `java_home=`, then `JAVA_HOME`, then
  `/usr/libexec/java_home` on macOS, then `java` on `PATH`, and raises `EngineNotFound` otherwise.
  On macOS `/usr/bin/java` is only a stub. `scala_cli_java_home()` returns the Temurin 17 JDK that
  scala-cli manages, so a machine with scala-cli needs nothing else.
- Importing the module never imports `pyspark`. Without it, `SparkEngine` raises `EngineNotFound`
  with the hint `pip install "local-data-platform[spark]"`.

### Tests

```bash
pytest tests/test_spark_interop.py                        # unit tests; integration tests skip
LDP_RUN_SPARK=1 pytest tests/test_spark_interop.py -m spark
```

The integration tests run only with `LDP_RUN_SPARK=1` and scala-cli installed. They write 1000
synthetic rides with pyiceberg, run the Scala job, read the Spark-written table back with pyiceberg
and compare it with a plain-Python aggregate, then query and append through PySpark (skipped if
`pyspark` isn't installed). The first run downloads about 500 MB; later runs take about 15 seconds.

## What a run looks like

1000 rides over seven days in four cities, written by pyiceberg in two appends (600 and 400):

```text
== Spark 4.1.3 read `nyc`.`nyc`.`rides`: 1000 rows
== Revenue by city per day (fare summed over pickup_ts days)
+----------+----+---------+------------------+
|day       |city|row_count|revenue           |
+----------+----+---------+------------------+
|2026-09-01|BKK |34       |921.3             |
|2026-09-01|LON |36       |1092.66           |
|2026-09-01|NYC |37       |1215.0900000000004|
...
== Snapshot history of `nyc`.`nyc`.`rides`.`snapshots`
|committed_at           |snapshot_id        |parent_id          |operation|added_records|total_records|
|2026-09-30 16:58:45.641|3207766793834732067|NULL               |append   |600          |600          |
|2026-09-30 16:58:45.652|4435750809371935792|3207766793834732067|append   |400          |1000         |
== Wrote `nyc`.`nyc`.`rides_by_city_day`: 28 rows
LDP_SPARK_RESULT source=`nyc`.`nyc`.`rides` source_rows=1000 target=`nyc`.`nyc`.`rides_by_city_day` target_rows=28 mode=revenue_by_city_day
```

pyiceberg then lists `('nyc', 'rides')` and `('nyc', 'rides_by_city_day')`, and all 28 groups
match the plain-Python aggregate (1000 rows, revenue 32767.93). A warm run of the job takes about
4.5 seconds.

## Limitations

- **One writer at a time.** The catalog is a single SQLite file. Run Python loads and Spark jobs one
  after the other, never at the same time, and don't keep a `SparkEngine` open while a Python
  pipeline writes to the same catalog.
- **Local only.** Both engines run Spark in `local[*]` mode on one machine, against a `file://`
  warehouse. `CatalogLocation.from_catalog` rejects non-SQLite catalogs and non-local warehouses.
- **The Scala job needs a source checkout.** The `spark/` folder isn't in the wheel;
  `ScalaSparkJob(project_dir=...)` can point at another copy.
- **First runs download a lot.** scala-cli fetches a JDK, Spark and Iceberg (about 500 MB). PySpark
  is a 455 MB sdist and fetches the Iceberg runtime (48 MB) on its first session.
- **`CREATE OR REPLACE` replaces.** The output table's schema and data are replaced on every run.
  Its history stays readable through time travel, but earlier rows are no longer current.
- **Column detection is by name.** The revenue aggregate needs a string `city` column and an amount
  column with one of the listed names. Other tables get a row count.
- **SQL NULL and floating-point rules apply.** `row_count` counts every row, including rows with a
  NULL amount; `revenue` sums the non-NULL amounts and is NULL when a group has none. Rows with a
  NULL day or city form their own groups. `revenue` is a sum of doubles, so it can differ from a
  Python `sum()` in the last bit or two (Spark adds in a different order); compare with a tolerance.
