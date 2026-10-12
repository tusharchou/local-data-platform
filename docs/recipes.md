# Recipes

Short, runnable examples of the Python API. Each one works on its own from an empty folder, in
the order shown (recipes 4 to 6 read the table recipe 3 writes). The [Quickstart](quickstart.md)
covers configs and the `ldp` command.

---

## Read a local JSON file

`Json` reads any JSON file, such as a dataset config or a settings file. A relative path resolves
against `base_dir`, or the current folder when `base_dir` is not given.

```python
import json

from local_data_platform.store.source.json import Json

with open("settings.json", "w") as f:
    json.dump({"cities": ["NYC", "BKK"]}, f)

settings = Json("settings", "settings.json").get()
print(settings["cities"])   # ['NYC', 'BKK']
```

## Check a batch before you write it

Quality checks run on any `pyarrow.Table`, without a pipeline. A check on a missing column fails;
it doesn't raise.

```python
import pyarrow as pa

from local_data_platform.quality import AcceptedValues, NotNull, Range, Unique, run_checks

batch = pa.table({"ride_id": [1, 2, 2], "city": ["NYC", "BKK", "LDN"], "fare": [12.5, -3.0, 8.0]})
report = run_checks(batch, [
    NotNull(["ride_id"]),
    Unique(["ride_id"]),
    AcceptedValues("city", ["NYC", "BKK"]),
    Range("fare", min=0),
])
print(report.passed)      # False
print(report.summary())   # one line per check, [PASS] or [FAIL]
for result in report.failures:
    print(result.name, result.failing_rows)
```

`report.raise_for_failures()` raises `DataQualityError` when any check failed, which is what a
pipeline with `on_failure="fail"` does before it writes.

## Upsert into an Iceberg table and time-travel

`Iceberg.put` returns a `WriteResult` with the row counts before and after the write.

```python
import pyarrow as pa

from local_data_platform.format.iceberg import Iceberg

rides = Iceberg("rides", {"identifier": "nyc", "warehouse_path": "warehouse"},
                write_mode="upsert", join_cols=["ride_id"])
first = rides.put(pa.table({"ride_id": [1, 2], "fare": [10.0, 20.0]}))
second = rides.put(pa.table({"ride_id": [2, 3], "fare": [25.0, 30.0]}))
print(first.rows_after, second.rows_before, second.rows_after)   # 2 2 3

snapshots = rides.snapshots()
print(rides.get(snapshot_id=snapshots[0]["snapshot_id"]).num_rows)   # 2: the table after the first put
print(rides.get(row_filter="fare >= 25", selected_fields=["ride_id"]).sort_by("ride_id").to_pylist())
# [{'ride_id': 2}, {'ride_id': 3}]
```

`put(df, mode="overwrite")` replaces the rows instead, for a single call.

## Export an Iceberg table to Parquet

`Config.from_dict` builds a config in code. `create_pipeline` picks `IcebergToParquet` from the
source and target formats.

```python
from local_data_platform import Config
from local_data_platform.pipeline.registry import create_pipeline

config = Config.from_dict({
    "identifier": "rides_export",
    "metadata": {
        "source": {"name": "rides", "format": "ICEBERG",
                   "catalog": {"identifier": "nyc", "warehouse_path": "warehouse"}},
        "target": {"name": "rides", "format": "PARQUET", "path": "exports/rides.parquet"},
    },
}, base_dir=".")
result = create_pipeline(config).run()
print(result.rows_written)   # 3
```

## Join an Iceberg table with an Arrow table in DuckDB

Needs the `duckdb` extra: `pip install "local-data-platform[duckdb]"`.

```python
import pyarrow as pa

from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.format.iceberg import Iceberg

rides = Iceberg("rides", {"identifier": "nyc", "warehouse_path": "warehouse"})
cities = pa.table({"ride_id": [1, 2, 3], "city": ["NYC", "BKK", "NYC"]})

with DuckDBEngine() as duck:
    duck.register_iceberg(rides, "rides")
    duck.register_arrow(cities, "cities")
    print(duck.query("SELECT city, sum(fare) AS revenue FROM rides JOIN cities USING (ride_id) "
                     "GROUP BY city ORDER BY city").to_pylist())
# [{'city': 'BKK', 'revenue': 25.0}, {'city': 'NYC', 'revenue': 40.0}]
```

## Add your own pipeline route

A new route is one decorated `Pipeline` subclass that builds its source and target from the
config. [Pipeline registry and factory](design/factory_registry.md) explains the design.

```python
from local_data_platform import Config
from local_data_platform.format.csv import CSV
from local_data_platform.format.parquet import Parquet
from local_data_platform.pipeline import Pipeline
from local_data_platform.pipeline.registry import create_pipeline, register_pipeline


@register_pipeline("PARQUET", "CSV")
class ParquetToCSV(Pipeline):
    """Parquet file to CSV file."""

    def build_source(self, config: Config) -> Parquet:
        block = config.source
        return Parquet(block["name"], block["path"], base_dir=config.base_dir)

    def build_target(self, config: Config) -> CSV:
        block = config.target
        return CSV(block["name"], block["path"], base_dir=config.base_dir)


config = Config.from_dict({
    "identifier": "rides_csv",
    "metadata": {
        "source": {"name": "rides", "format": "PARQUET", "path": "exports/rides.parquet"},
        "target": {"name": "rides", "format": "CSV", "path": "exports/rides.csv"},
    },
}, base_dir=".")
print(create_pipeline(config).run())   # rides_csv: read 3 rows, wrote 3 rows in ...
```

Your module must be imported before `create_pipeline` is called. `ldp pipelines` lists the
registered routes.
