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

## A Google Sheet that stays in sync

The sales of a small shop live in a Google Sheet that someone types into every day. This recipe loads the sheet into an
Iceberg table every night, so that you can query it with SQL and build a report from the same rows. It needs no Google
API and no credentials. A sheet that is shared by link has a CSV export address, and a CSV source may be an
`https://` address.

First share the sheet. In Google Sheets choose Share, and under General access choose Anyone with the link, as a Viewer.
Anyone who has the link can then read the sheet, so keep the link as private as the data. LDP reads the address with no
credentials, and `ldp plan` and `ldp schedule tick` refuse a config whose address has a token, a key or a password in
it, because a secret does not belong in a config.

The export address of a tab is shown below. `SHEET_ID` is the part of the address of the sheet between `/d/` and
`/edit`, and `TAB_ID` is the number after `gid=` when the tab is open. A tab is one CSV, so each tab has a config of its
own.

```text
https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=TAB_ID
```

The config has the address as the `path` of the source, an Iceberg table as the target, `upsert` as the `write_mode`
with the key columns of the sheet as `join_cols`, and `"schedule": "@daily"` under `metadata`. The first row of the
sheet holds the column names. A row whose key is in the table is updated, and any other row is added.

```json
{
  "identifier": "sheet_sales",
  "what": "The daily sales of the shop, from its Google Sheet",
  "metadata": {
    "source": {"name": "sales", "format": "CSV",
               "path": "https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=0"},
    "target": {"name": "sales", "format": "ICEBERG",
               "catalog": {"identifier": "shop", "warehouse_path": "../warehouse"},
               "write_mode": "upsert", "join_cols": ["day", "dish"]},
    "schedule": "@daily"
  }
}
```

The next block stands in for Google with a small server on this machine, so that the recipe runs with no network. It
writes the config above with the address of that server as the `path`, and ticks on four nights. `ldp schedule tick` is
what cron or launchd calls, and a tick reads the sheet once for the window that is due. Between the nights someone edits
a cell and adds a row.

```python
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from local_data_platform.format.iceberg import Iceberg
from local_data_platform.orchestration.schedule import tick

SHEET = ["day,dish,orders,revenue", "2026-07-01,pad thai,12,143.5", "2026-07-02,green curry,9,117.0"]
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}


class Google(BaseHTTPRequestHandler):
    """Answers every request with the rows of SHEET, as the export address of a sheet does."""

    def do_GET(self):
        body = "\n".join(SHEET).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Google)
threading.Thread(target=server.serve_forever, daemon=True).start()

shop = Path("shop")
shop.mkdir()
address = f"http://127.0.0.1:{server.server_port}/spreadsheets/d/SHEET_ID/export?format=csv&gid=0"
(shop / "sheet_sales.json").write_text(json.dumps({
    "identifier": "sheet_sales",
    "what": "The daily sales of the shop, from its Google Sheet",
    "metadata": {
        "source": {"name": "sales", "format": "CSV", "path": address},
        "target": {"name": "sales", "format": "ICEBERG", "catalog": CATALOG,
                   "write_mode": "upsert", "join_cols": ["day", "dish"]},
        "schedule": "@daily",
    },
}))


def night(day):
    """Tick at 02:05 on a day of August, as cron does, and say what the run of that night wrote."""
    tick(shop, now=f"2026-08-{day:02d}T02:05:00Z")
    events = [json.loads(line) for line in (shop / ".ldp" / "events.jsonl").read_text().splitlines()]
    run = [event["payload"] for event in events if event["type"] == "run.finished"][-1]
    print(f"night {day}: read {run['rows_read']} rows, wrote {run['rows_written']}")


night(5)
night(6)                                      # nobody changed the sheet
SHEET[2] = "2026-07-02,green curry,10,124.0"  # someone edits a cell
night(7)
SHEET.append("2026-07-03,tom yum,5,60.0")     # and adds a row
night(8)

table = Iceberg("sales", catalog=CATALOG, base_dir=shop)
for row in sorted(table.get().to_pylist(), key=lambda row: row["day"]):
    print(row["day"], row["dish"], row["orders"], row["revenue"])
server.shutdown()
```

<!-- docs-runner: output -->
```text
night 5: read 2 rows, wrote 2
night 6: read 2 rows, wrote 0
night 7: read 2 rows, wrote 1
night 8: read 3 rows, wrote 1
2026-07-01 pad thai 12 143.5
2026-07-02 green curry 10 124.0
2026-07-03 tom yum 5 60.0
```

Every night reads the whole tab, and the upsert writes only the rows that changed, so a night with no change writes no
data file at all. To run the sync once by hand, run `ldp run shop/sheet_sales.json`. To run it every night, print the line
for cron, or the plist for launchd on a Mac, and install it as [A scheduler](orchestration.md#part-7-a-scheduler)
shows. It ticks every 5 minutes and does the work when a window is due.

```bash
ldp schedule install --print cron shop
```

<!-- docs-runner: output -->
```text
# ldp schedule tick for .../shop, every 5 minutes
*/5 * * * * mkdir -p .../shop/.ldp && .../ldp schedule tick .../shop >> .../shop/.ldp/schedule.log 2>&1
```

What to know about a sheet.

- A row that you delete in the sheet stays in the table, because an upsert only updates and adds. To make the table
  match the sheet, delete rows included, set `write_mode` to `overwrite`. Every night then writes every row.
- Each key must be there once. An empty key cell or a key that appears twice fails the run with a message that says so,
  and nothing is written. So does a header that someone renames, because a key column is then missing.
- The cells decide the types, as the sheet shows them. A date such as `2026-07-01` is a date, TRUE and FALSE are
  booleans, a whole number is `int64`, another number is `float64`, and anything else is text, so a date that the sheet
  shows as `7/1/2026` and an amount that it shows as `$12.50` are text. Give the date columns the format `yyyy-mm-dd` and
  the number columns a plain format. An empty cell is null. If someone types text into a number column, the column
  becomes text, the write fails with `Cannot change column type`, and nothing is written.
- A run that fails is not lost. The next tick, 5 minutes later, runs the same window again, so the sync works as soon as
  the cell is fixed. With `metadata.retries` a run also tries again within the tick when Google answers 429 or a 5xx
  status, or does not answer in time. Any other answer, such as 403 or 404, is not retried within the tick.
- A sheet that is not shared by link answers with a sign-in page. LDP does not read that page as rows. The run fails and
  says that the answer is a web page, and that the sheet has to be shared with anyone who has the link.
- A download may take 30 seconds and have 50 MiB after it is decompressed. Set `timeout`, in seconds, and `max_bytes`
  in `metadata.source` to change that.
- A private sheet cannot be read, because LDP sends no credentials. Reading one with a service account is not part of
  this release.

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
