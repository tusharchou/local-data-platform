# Daily sales

Orders and revenue of one day, from the gold tables.

**Window** 2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata

> **Stale data.** Some of the tables behind this report are out of date or held back, so check the numbers that come from them.
>
> - `gold.gold_sales` its last run ended blocked\_quality (failing not\_null(city))

- **Revenue** $1,234.50 (+12.2% from $1,100.00)
- **Orders** 87 (-3.3% from 90)
- **Share of the week** 18.8%

## Revenue by city

| city | revenue |
| --- | ---: |
| Pune | $520.25 |
| Delhi | $410.00 |
| Mumbai | $304.25 |

## Revenue by day

| day | Pune | Delhi |
| --- | ---: | ---: |
| 2026-07-30 | $480.00 | $350.50 |
| 2026-07-31 | $520.25 | $410.00 |

## Top cuisines

| cuisine | orders | share |
| --- | ---: | ---: |
| Thai | 31 | 35.6% |
| Italian | 24 | 27.6% |
| Indian | 17 | 19.5% |

*The table shows the first 3 of 5 rows. 2 rows are left out.*

## Note

Orders &amp; revenue of &lt;Pune&gt; and Delhi. There were 87 orders in all.

Numbers are from the gold tables.

## Data health

*One row for each table that this report read, measured when it was built.*

| Table | Last run | Status | Rows written | Finished | Checks | Freshness | Stale |
| --- | --- | --- | ---: | --- | --- | --- | --- |
| `gold.gold_sales` | 2026-08-02 05:30:15 UTC | blocked\_quality | 0 | 2026-08-02 05:30:17 UTC | 4/5, failing not\_null(city) | ok, last success 2026-08-02 03:30:15 UTC | yes |
| `gold.gold_customers` | no pipeline | - | - | - | - | - | no |

## How this report was made

- **Window** 2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata
- **Time zone** Asia/Kolkata
- **Tables read**
  - `gold.gold_sales` release r3, cut 2026-08-02 06:00:00 UTC, snapshot 8420615207115790441
  - `gold.gold_customers` the tables as they are now, because release r3 does not hold gold.gold\_customers, snapshot 3187645520913349012
- **Built** 2026-08-02 06:30:15 UTC
