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

## Excel to CSV in a Google Drive folder

Needs the `excel` extra: `pip install "local-data-platform[excel]"`. A spreadsheet that you download from a
website becomes a CSV in a folder you choose. To share it through Google Drive, make the target the folder that
Google Drive for desktop syncs, such as `~/Google Drive/My Drive/sales`. LDP only writes a file there, so it
needs no Drive API and no credentials, and the file is replaced atomically, so Drive never uploads a half-written
one. `header_row` skips the title above the column names, and `sheet` picks the sheet by name or by position.

```python
import datetime as dt

import openpyxl

from local_data_platform import Config
from local_data_platform.pipeline.registry import create_pipeline

# A stand-in for the file you downloaded, with a title above the header row.
workbook = openpyxl.Workbook()
sheet = workbook.active
sheet.title = "Sales"
sheet.append(["Sales for July"])
sheet.append(["day", "dish", "orders", "revenue"])
sheet.append([dt.date(2026, 7, 1), "pad thai", 12, 143.5])
sheet.append([dt.date(2026, 7, 2), "green curry", 9, 117.0])
workbook.save("sales.xlsx")

drive = "drive"   # on your machine, the folder that Google Drive for desktop syncs
config = Config.from_dict({
    "identifier": "sales_to_drive",
    "metadata": {
        "source": {"name": "sales", "format": "XLSX", "path": "sales.xlsx", "sheet": "Sales", "header_row": 2},
        "target": {"name": "sales", "format": "CSV", "path": f"{drive}/sales.csv"},
    },
}, base_dir=".")
print(create_pipeline(config).run().rows_written)   # 2
print(open(f"{drive}/sales.csv").read())
# "day","dish","orders","revenue"
# 2026-07-01 00:00:00.000000,"pad thai",12,143.5
# 2026-07-02 00:00:00.000000,"green curry",9,117
```

Dates become timestamps, whole numbers `int64` and other numbers `float64`. To load the sheet into an Iceberg table
instead, make the target an Iceberg block, as in [the quickstart](quickstart.md).

## Monthly churn from an orders table

Needs the `duckdb` extra, which `pip install "local-data-platform[duckdb]"` installs. A customer churns in the month they
stop ordering. For a month M, `customers_at_start` is the number of customers with an order in the month before, `churned` is how many of
them have no order in month M, and `churn_rate` is the second divided by the first, with no value when nobody was there to
lose. The first month has no row, because there is no month before it, and the months run from the first order to the
last, so a month with no order has a row. This is the definition of `churn_by_month` in the restaurant example, which
computes it with dbt, and the one that its metrics `churn_rate` and `active_customers` report.

```python
import datetime as dt

import pyarrow as pa

from local_data_platform.engine.duckdb import DuckDBEngine

# C1, C2 and C3 order in January, C4 in February, C4 and C1 in March, C1 in April and in June.
placed = [("C1", dt.date(2023, 1, 5)), ("C2", dt.date(2023, 1, 12)), ("C3", dt.date(2023, 1, 20)),
          ("C4", dt.date(2023, 2, 3)), ("C4", dt.date(2023, 3, 2)), ("C1", dt.date(2023, 3, 15)),
          ("C1", dt.date(2023, 4, 10)), ("C1", dt.date(2023, 6, 5))]
orders = pa.table({"order_id": [f"O{n}" for n in range(1, len(placed) + 1)],
                   "customer_id": [customer for customer, _ in placed],
                   "ordered_on": [day for _, day in placed]})

ACTIVITY = """
WITH activity AS (   -- one row for each customer and each month in which they ordered
    SELECT DISTINCT customer_id, CAST(date_trunc('month', ordered_on) AS DATE) AS month FROM orders
),
months AS (          -- every month from the first order to the last
    SELECT CAST(month AS DATE) AS month
    FROM (SELECT min(month) AS first_month, max(month) AS last_month FROM activity),
         generate_series(first_month, last_month, INTERVAL 1 MONTH) AS series(month)
)"""

CHURN = ACTIVITY + """,
counts AS (
    SELECT months.month,
           count(previous.customer_id) AS customers_at_start,
           count(previous.customer_id) FILTER (WHERE kept.customer_id IS NULL) AS churned
    FROM months
    LEFT JOIN activity AS previous ON previous.month = CAST(months.month - INTERVAL 1 MONTH AS DATE)
    LEFT JOIN activity AS kept ON kept.customer_id = previous.customer_id AND kept.month = months.month
    WHERE months.month > (SELECT min(month) FROM activity)   -- the first month has no month before it
    GROUP BY months.month
)
SELECT month, customers_at_start, churned, CAST(churned AS DOUBLE) / NULLIF(customers_at_start, 0) AS churn_rate
FROM counts ORDER BY month
"""

# What the snippet in issue #47 computes. It needs a status for each customer in each month, so make that table.
INACTIVE = ACTIVITY + """,
status AS (
    SELECT customers.customer_id, months.month,
           CASE WHEN activity.customer_id IS NULL THEN 'inactive' ELSE 'active' END AS status
    FROM (SELECT DISTINCT customer_id FROM orders) AS customers
    CROSS JOIN months
    LEFT JOIN activity ON activity.customer_id = customers.customer_id AND activity.month = months.month
)
SELECT month, 100.0 * count(*) FILTER (WHERE status = 'inactive') / count(*) AS churn_rate
FROM status GROUP BY month ORDER BY month
"""

with DuckDBEngine() as duck:
    duck.register_arrow(orders, "orders")
    for row in duck.query(CHURN).to_pylist():
        print(row["month"], row["customers_at_start"], row["churned"], row["churn_rate"])
    print([round(row["churn_rate"], 1) for row in duck.query(INACTIVE).to_pylist()])
```

<!-- docs-runner: output -->
```text
2023-02-01 3 3 1.0
2023-03-01 1 0 0.0
2023-04-01 2 1 0.5
2023-05-01 1 1 1.0
2023-06-01 0 0 None
[25.0, 75.0, 50.0, 75.0, 100.0, 75.0]
```

All three customers of January are gone in February, so February is 3 of 3. C4 and C1 order in March, so nobody of
February is lost and March is 0 of 1. Nobody orders in May, so June has no customer at the start and no churn rate. The
last line has other numbers for the same orders.

The pandas snippet in [issue #47](https://github.com/tusharchou/local-data-platform/issues/47) gives those other numbers,
because it counts inactive rows and not lost customers. Its churn rate for a month is the share of the customers in the
month whose status is inactive, and it needs a status for each customer in each month, which an orders table does not
have, so `INACTIVE` makes the table first. Run on that table, the snippet itself gives the same six numbers as the last
line. They differ from the churn above in three ways.

- A customer who stays away is counted again in every month. C2 and C3 ordered only in January, so the snippet counts them
  in each of the five months after it, and the churn counts them once, in February, the month they stopped.
- A customer who has not ordered yet is counted. C4 is inactive in January, before the first order, and the snippet calls
  that churn in a month that has no churn.
- It divides by all four customers in every month. The churn rate divides by the customers who ordered in the month
  before, who are 1 in March.

A customer who stops ordering leaves no row in an orders table, so a count of inactive rows never sees the lost customers
unless someone makes the rows. The restaurant example reports the churn above as the metric `churn_rate`, and the
customers with an order in the month as `active_customers`. Both are only by month, because adding up the months would
count a customer once for each month. After `make demo-warehouse`, this prints them.

```bash
ldp metrics query ldp_demo/restaurant/config churn_rate,active_customers --grain month
```
