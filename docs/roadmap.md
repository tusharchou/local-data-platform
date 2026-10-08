# Roadmap

Local Data Platform aims to be a full data platform in a box. Each milestone adds the next layer that data passes through inside a company, in the order a company usually builds them. First you store and model the data, then you run it on a schedule, serve trusted gold tables, watch them, report on them, analyse them and ask them questions in plain English. After that come documentation for newcomers and, last, the move to the cloud. Every layer runs on a laptop first, with no services to install.

One dataset makes the whole journey. `examples/restaurant` starts in 0.1.2 as the sales, customers, menus and reviews of a small restaurant group. Every later milestone extends the same example, so you can follow the same rows from a raw file to a dashboard and a plain-English answer.

**Version numbers.** From 0.1.1 on, the package version equals the milestone number. 0.1.1 is the current release, made from PR #117. Milestone 0.1.2 ships as package 0.1.2, and so on up to 0.2.0 Cloud Integration. APIs deprecated in 0.1.1 are removed in 0.2.0.

**Sizes.** Sizes are estimates in engineer-days for one engineer working with an AI assistant, before review cycles. A small issue (S) is 1.5 days, a medium one (M) is 5 days and a large one (L) is 11 days. The whole plan comes to about 288 engineer-days.

## Summary

| Milestone | What it adds | Demo | Size (days) | Status |
|---|---|---|---|---|
| 0.1.1 | Today's platform on PyPI, renumbered from 0.2.0 | `ldp demo` | 1.5 | Releasing |
| 0.1.2 Warehousing: DuckDB, Iceberg, DBT | dbt models over Iceberg tables, published back to Iceberg, plus Excel input | `make demo-warehouse` | 33.5 | Planned |
| 0.1.3 Orchestration | Windows, backfills, retries and a laptop scheduler, plus an Airflow operator | `make demo-orchestration` | 34 | Planned |
| 0.1.4 Self Serving Gold Layer | Gold tables with owners and docs, releases, metrics, a catalog and exports | `make demo-gold` | 31 | Planned |
| 0.1.5 Monitoring | SLAs, freshness, volume, schema and metric monitors, alerts and `ldp status` | `make demo-monitor` | 25.5 | Planned |
| 0.1.6 Business Intelligence Reporting Dashboarding | Offline HTML reports, Slack delivery, a live dashboard and BI tool recipes | `make demo-bi` | 27 | Planned |
| 0.1.7 Data Science Insights | Churn, retention, funnels, sessions, training sets and a notebook | `make demo-insights` | 29.5 | Planned |
| 0.1.8 LLM | Catalog search and `ldp ask`, text to SQL that corrects itself | `make demo-ask` | 34 | Planned |
| 0.1.9 Launch Documentation | The Learn path, generated reference pages, onboarding and contributor setup | `make docs-test` | 34 | Planned |
| 0.2.0 Cloud Integration | Profiles, cloud catalogs and storage, Snowflake and one container image | `make demo-cloud` | 38 | Planned |
| **Total** | | | **288** | |

## Shared building blocks

Each of these is built once, in the milestone shown, and reused by every milestone after it.

- **One example.** `examples/restaurant` (0.1.2). `python examples/restaurant/run.py --layer NAME` runs one layer, and each milestone adds a layer.
- **One way to write SQL results to Iceberg.** The `DUCKDB` source format (0.1.2). 0.1.4 adds `inputs` and `query_file` to it, and the 0.1.7 insights run through it.
- **One `kind` key and one exit-code table** (0.1.3). JSON files declare `Pipeline` (the default), `Workflow`, `Metrics`, `Report` or `Profiles`. Every command exits with 0 on success, 1 on an error, 2 when `ldp monitor` finds an SLA breach and 3 when a data gate holds a run or a report back.
- **One page shell for HTML output.** `src/local_data_platform/html.py` (0.1.4), used by the gold catalog page, `ldp status --html` and the reports.
- **One metric layer.** `metrics.json` and `ldp metrics` (0.1.4), read by the monitors (0.1.5), the reports (0.1.6), the insights (0.1.7) and `ldp ask` (0.1.8).
- **One notifier.** `src/local_data_platform/notify.py` (0.1.5), used for alerts and for report delivery.
- **One `excel` extra** (0.1.2) and **one `notebook` extra** (0.1.6).
- **One dbt CI job** (0.1.2). dbt stays out of the `dev` extra. A later test that needs dbt carries the `dbt` marker and runs in that job, and every demo prints a skip line when dbt is not installed.
- **One guide format.** Each milestone writes its guide as a Learn chapter on `examples/restaurant`, so 0.1.9 moves the guides into `docs/learn/` instead of rewriting them.

## 0.1.1, the current release

PR #117 carries the platform built so far. That includes Iceberg tables in a local catalog with append, overwrite and upsert, partitioning, quality gates, exactly-once staged publishing, run events and OpenLineage, DuckDB queries, pinned datasets, maintenance, the read-only MCP server for agents and `ldp demo`. It ships as 0.1.1, the closed milestone whose four items it completes, so that PyPI version order follows this roadmap. The release issue renumbers the code, tightens the packaging tests and the tag check in `publish.yml`, and corrects the docs about concurrent appends (fixed in 0.1.2, #88). It closes #15.

```bash
pip install "local-data-platform[duckdb]==0.1.1"
ldp demo
```

## 0.1.2 Warehousing: DuckDB, Iceberg, DBT

**Goal.** Make the laptop lakehouse a warehouse people can model in. dbt models run over LDP's Iceberg tables in DuckDB and get tested and documented, and the models a user marks are published back to Iceberg as versioned tables with time travel. Excel files become a source, monthly customer churn is the worked example and concurrent appends become safe.

**What already exists**

- Iceberg tables in a local SQLite catalog, created on first write, with append, overwrite and upsert from a config (`src/local_data_platform/format/iceberg/__init__.py`, route `CSVToIceberg` in `src/local_data_platform/pipeline/builtin.py`).
- Partitioning with identity, time, `bucket[N]` and `truncate[W]` transforms, set when a table is created (#23).
- Snapshots and time travel from Python, `ldp snapshots` from the command line, and `ldp query` over one table through DuckDB (#3, `src/local_data_platform/engine/duckdb/__init__.py`).
- Row counts read from snapshot metadata in LDP's own `Iceberg.row_count` (#1). It does not depend on upstream pyiceberg work. The merged upstream PR #1388 is ResidualVisitor, and the metadata-only count PR #1480 was closed unmerged.
- Quality checks that follow dbt's generic tests and block bad writes (`src/local_data_platform/quality/checks.py`).
- Run events, the run ledger, OpenLineage (`src/local_data_platform/events.py`) and a commit retry policy with backoff (`CommitPolicy` in `src/local_data_platform/format/iceberg/commit.py`).
- DuckDB and pyiceberg as dependencies (#57). dbt is not installed anywhere yet.

**What this milestone adds**

- Retry direct-mode Iceberg appends that lose a commit race on pyiceberg 0.11 (S, continues #88)
- Add the dbt extra and a CI job that runs the dbt tests offline (S, continues #57)
- DUCKDB source format, the one way to publish a DuckDB table or query to Iceberg (M)
- Register LDP Iceberg tables as dbt sources in a DuckDB build file (S)
- `ldp dbt` runs a dbt project over LDP tables and publishes marked models to Iceberg (L, continues #45)
- `ldp query` gets time travel flags, folders of tables and `open_lake` (S)
- Excel source with XLSX to CSV and XLSX to ICEBERG routes (M, continues #48)
- Restaurant example with monthly customer churn in dbt, and `make demo-warehouse` (M, continues #47)
- Warehousing guide as a Learn chapter (S)

**The demo**

```bash
make install
.venv/bin/python -m pip install -e ".[dbt,excel]"
make demo-warehouse
.venv/bin/ldp snapshots ldp_demo/restaurant/config/churn_by_month.json
.venv/bin/ldp query ldp_demo/restaurant/config/churn_by_month.json "SELECT month, customers_at_start, churned, churn_rate FROM churn_by_month ORDER BY month"
.venv/bin/ldp query ldp_demo/restaurant/config/churn_by_month.json "SELECT * FROM churn_by_month WHERE month = DATE '2026-06-01'" --snapshot SNAPSHOT_ID
.venv/bin/ldp query ldp_demo/restaurant/config "SELECT c.month, c.churn_rate, count(o.order_id) AS orders FROM churn_by_month c JOIN fct_orders o ON date_trunc('month', o.order_ts) = c.month GROUP BY ALL ORDER BY c.month"
.venv/bin/ldp dbt docs ldp_demo/restaurant/dbt --serve
.venv/bin/ldp mcp --config ldp_demo/restaurant/config
```

- `make demo-warehouse` runs `examples/restaurant/run.py --workdir ldp_demo/restaurant --layer warehouse` offline. It turns `restaurants.xlsx` into a CSV and an Iceberg table, loads customers and the first batch of orders, and runs `ldp dbt build`, which passes every data test and publishes `silver.fct_orders`, `gold.churn_by_month` and `gold.revenue_by_restaurant_day`.
- It prints churn for February to June 2026, loads the second batch and rebuilds. `fct_orders` upserts only the July rows and the late June rows.
- The `--snapshot` query shows June's churn before the late orders arrived, and the folder query joins two published tables.
- `make test` includes the 8-process append race for #88, and `pytest -m dbt` runs the dbt layer and the example offline.

**Key decisions**

- **Which dbt.** dbt-core 1.12 with dbt-duckdb 1.11 in a `dbt` extra, run as a subprocess, so dbt v2 can be added later without a redesign.
- **How models reach Iceberg.** dbt builds in a DuckDB file with its stock materializations, then LDP publishes the marked models through the `DUCKDB` source, so the quality gate, run events and staged commits all apply.
- **How dbt reads Iceberg.** Persistent DuckDB views over `iceberg_scan`, pinned to the current snapshot, with copies as the fallback when the iceberg extension is missing. LDP never downloads an extension.
- **Which models publish.** Models with dbt meta `ldp_publish: true`. Table models overwrite, and incremental models with a `unique_key` upsert on it.
- **When a dbt node fails.** Nothing is published. Publishing is all or nothing.
- **How #88 is fixed.** LDP retries direct appends with `CommitPolicy` backoff, and the minimum stays pyiceberg 0.11.
- **Excel reader.** openpyxl plus defusedxml in an `excel` extra. A Google Drive copy uses the folder that Google Drive for desktop syncs, with no API or credentials.
- **Where churn lives.** Here, in the dbt model of the restaurant example. Churn is counted in the month a customer stops ordering, and 0.1.4 and 0.1.7 reuse that convention.
- **Package version.** 0.1.2.

**Out of scope or moved later**

- Scheduling dbt runs (Story 3, step 7) moves to 0.1.3.
- OpenLineage inputs from the dbt manifest move to the 0.1.4 gold build.
- Partial publishing after a failed dbt node, partition evolution after a table exists and dbt snapshots are not planned.
- dbt v2 (the Rust engine) waits until it supports local docs and has stable artifacts.
- Cut line. If the milestone runs past 6 weeks, #48 moves to 0.1.4 together with the `excel` extra, and the example reads `restaurants.csv` instead of `restaurants.xlsx`.

## 0.1.3 Orchestration

**Goal.** Make the lakehouse run itself on a laptop. Pipelines, dbt models and maintenance are declared once in a workflow file and run for the right time window on a schedule, from a built-in `ldp schedule` with no services or from Airflow 3. Backfills and retries reuse the idempotency keys, so no window is ever loaded twice. Today `ldp run` rejects `--window`, a config cannot hold a schedule, three daily windows over one file write 9 rows instead of 3, and the 501st daily window fails its key search. This milestone fixes all four.

**What already exists**

- Idempotent windowed runs from Python. `run_config(config, window=...)` writes through the staged protocol, and a second run over the same window writes nothing (`src/local_data_platform/etl.py`).
- Idempotency keys and window parsing, and `ldp plan CONFIG --window START/END` prints the key (`src/local_data_platform/spec.py`).
- Staged exactly-once publishing that fences older attempts (`CommitContext` and `CommitPolicy` in `src/local_data_platform/format/iceberg/commit.py`).
- Run events that carry the window and the key, and `ldp commits CONFIG --key K` (`src/local_data_platform/events.py`).
- Snapshot expiry that keeps keyed snapshots for 7 days (`src/local_data_platform/maintenance/snapshots.py`).
- A file lock for POSIX and Windows that a scheduler can reuse (`src/local_data_platform/format/iceberg/__init__.py`).

**What this milestone adds**

- Spec kinds and apiVersion (S)
- `ldp run` takes a window and a retry identity, with one exit-code table (S)
- Window-aware configs with `metadata.schedule`, `metadata.window`, path placeholders and window overwrite (M)
- Idempotency keys that hold for long backfills and late re-runs (S)
- Workflow files with `ldp workflow plan` and `ldp workflow run` (M)
- dbt models as workflow tasks (S)
- Task retries that reuse the run id and the idempotency key (S)
- `ldp backfill` over a range of windows, with resume and replace (M)
- `ldp schedule`, a local scheduler with no services, plus cron and launchd recipes (M, continues #44)
- Airflow 3 `LdpRunOperator` and a BashOperator recipe (S, continues #42)
- Orchestration guide and `make demo-orchestration` (M)

**The demo**

```bash
make install
make demo-orchestration
ldp workflow plan ldp_demo/restaurant/workflows/daily_orders.json --window 2026-08-01/2026-08-02
ldp backfill ldp_demo/restaurant/workflows/daily_orders.json --from 2026-08-01 --to 2026-08-04
ldp schedule tick ldp_demo/restaurant --now 2026-08-05T02:05:00Z
ldp schedule list ldp_demo/restaurant
ldp runs ldp_demo/restaurant/.ldp/events.jsonl
ldp commits ldp_demo/restaurant/config/orders.json
ldp schedule install --print launchd ldp_demo/restaurant
```

- The demo writes daily order drops for 1 to 3 August 2026 from `examples/restaurant`, with a bad row on day 3.
- The first backfill publishes days 1 and 2. Day 3 is blocked by its quality gate, its dbt and maintenance tasks show `upstream_failed`, and the command exits with 3.
- After the demo fixes the row, the same backfill runs only day 3, and a third run skips every window.
- The tick loads 4 August, `ldp schedule list` shows the next fire time in UTC and local time, and `ldp commits` shows exactly one publish per day.
- The dbt step runs when the `dbt` extra is installed, and the demo prints a skip line otherwise.

**Key decisions**

- **Local scheduler.** A built-in `ldp schedule tick` that cron or launchd calls every 5 minutes. Airflow is optional, and Dagster and Prefect are covered in the docs only.
- **How a DAG is declared.** A JSON workflow file with `"kind": "Workflow"`. A config with a schedule counts as a one-task workflow.
- **Where schedule and window live.** Under `metadata`. `metadata.schedule` stays out of the spec hash, and `metadata.window` stays in it because its column changes which rows a run reads.
- **Cron library.** cronsim as a core dependency, with no dependencies of its own.
- **Time zone.** UTC by default, with an optional `window.timezone`.
- **Catch-up.** Off by default, as in Airflow 3. `ldp schedule list` prints the `ldp backfill` command for any longer gap.
- **Airflow packaging.** An in-package module that imports Airflow lazily, with no pip extra.
- **Exit codes.** 0 ok, 1 error, 2 reserved for SLA breaches from `ldp monitor`, 3 blocked by a data gate. Schedulers retry 1 and never retry 3.

**Out of scope or moved later**

- `ldp schedule run` (a foreground loop), the systemd and Windows Task Scheduler recipes and a Colab notebook are stretch items. `ldp schedule tick` with cron or launchd covers a laptop.
- DAGs built from workflow files, the opt-in Airflow CI workflow and managed Airflow move to 0.2.0.
- The Google Sheet sync for #106 moves to 0.1.6.
- Parallel task execution is not in scope. Tasks run one at a time, which also respects DuckDB's single writer.
- Unattended Colab Enterprise notebook schedules are not planned.

## 0.1.4 Self Serving Gold Layer

**Goal.** Make the gold layer self-serving. Every gold table carries an owner, a meaning, a grain and column docs, all gold tables publish together as one consistent release, and metrics are defined once. People who are not engineers reach the data without asking one, through a catalog page, CSV and Excel exports, a DuckDB file and the MCP server. The restaurant data mart (#97) shows all of it on a laptop.

**What already exists**

- Who, what, where, when and how fields in every config (`src/local_data_platform/config.py`).
- Gold tables built with DuckDB SQL in the robot example, which only its own script can run today (`examples/robot_episodes/run.py`).
- Pinned, tagged dataset versions with export to Parquet and JSONL (`src/local_data_platform/datasets.py`).
- A read-only MCP server whose `describe_table` already returns column docs, the latest run and the quality status, all empty for gold tables today (`src/local_data_platform/mcp_server/tools.py`).
- Metadata-only Iceberg commits for column docs and table properties, checked on pyiceberg 0.12.0.
- After 0.1.2, the `DUCKDB` source and the restaurant dbt project. After 0.1.3, `kind` dispatch and workflow files.

**What this milestone adds**

- Gold table contracts with owner, description, grain and column docs (M)
- SQL gold models on the DUCKDB source, and `ldp gold build` (S)
- Gold releases that publish every gold table as one consistent version (M)
- Gold catalog with `ldp gold list`, `ldp gold show`, a static catalog page and MCP fields (M)
- Metric definitions in `metrics.json` and `ldp metrics` (M)
- Self-serve access through CSV and Excel exports and a DuckDB file of the gold layer (M)
- Churn rate and active customers as gold metrics on the 0.1.2 churn model (S)
- Restaurant data mart gold tables on `examples/restaurant`, and `make demo-gold` (S, continues #97)
- Gold layer guide, README section and changelog (S)

**The demo**

```bash
pip install -e ".[duckdb,dbt,excel,mcp]"
make demo-gold
ldp gold list ldp_demo/restaurant/config
ldp gold show ldp_demo/restaurant/config gold_daily_sales
ldp metrics query ldp_demo/restaurant/config revenue,avg_ticket --by cuisine --grain month
ldp metrics query ldp_demo/restaurant/config churn_rate --grain month --sql
ldp gold releases ldp_demo/restaurant/config
ldp gold export ldp_demo/restaurant/config gold_restaurant_scorecard scorecard.xlsx
ldp gold duckdb ldp_demo/restaurant/config --out gold.duckdb
ldp gold docs ldp_demo/restaurant/config --out catalog.html
python examples/agent_client.py --config ldp_demo/restaurant/config
```

- The silver gate blocks a faulty batch (a negative amount, an unknown restaurant and a duplicated order) and prints each failed check.
- `ldp dbt build` and `ldp gold build` produce the gold tables and cut a release. The workflow file runs ingest, dbt and the gold build as one scheduled unit.
- `ldp gold list` shows each gold table with its owner, description, grain, freshness, gate status and release.
- The Excel export has a second sheet named `about`. `gold.duckdb` opens in any DuckDB client with no extension. `catalog.html` opens with the network off.
- The agent client calls `list_tables`, which now shows owners and descriptions, then `list_metrics` and `query_metric`.

**Key decisions**

- **How gold tables are built.** Any builder. dbt from 0.1.2 is the main path, and the `DUCKDB` source with `inputs` covers SQL without dbt. `ldp gold build` checks tables built elsewhere and never builds them.
- **Where docs live.** `metadata.docs` in the config, written to Iceberg table properties (`comment`, `ldp.owner`, `ldp.layer`, `ldp.grain`, `ldp.primary-key`) and column docs. For dbt models the publish step fills it from `target/manifest.json`.
- **Semantic layer.** An LDP-native `metrics.json` with `"kind": "Metrics"`, one table per query, using MetricFlow's terms so an exporter can follow.
- **Releases.** Built now on `datasets.pin` plus one release manifest. Readers use the current release by default.
- **DuckDB serving file.** Copies of the gold tables in the current release, not views, so any client opens it with no extension.
- **Excel export.** The `excel` extra from 0.1.2.
- **Command names.** `ldp gold` and `ldp metrics`, because `ldp catalog` already means the Iceberg catalog connection.
- **#97 scope.** The data mart. Anomaly alerts and Slack reports get their own issues in 0.1.5 and 0.1.6 that link #97.

**Out of scope or moved later**

- Access control and masking of PII columns. The MCP allowlist is the only boundary on a single-user laptop.
- Joins across semantic models, derived metrics and metric caching.
- Referential checks across tables. The example uses `accepted_values`.

## 0.1.5 Monitoring

**Goal.** Know when a table is late, too small, has changed shape or shows a business anomaly, without watching a dashboard. Each config carries its SLA, `ldp monitor` checks it between runs, alerts go to Slack, a webhook or a file when something breaks and again when it recovers, and `ldp status` shows every pipeline's health on one screen. Nothing needs an extra service.

**What already exists**

- Quality checks that judge the batch being written, never the table between runs (`src/local_data_platform/quality/checks.py`).
- Run events in `_ldp.runs`, `_ldp.quality_results` and `_ldp.audit`, and `ldp runs` with rows written per run (`src/local_data_platform/events.py`).
- An HTTP sender that reads its secret from an environment variable, in the OpenLineage sink (`OpenLineageSink` in `events.py`).
- Snapshot summaries and file statistics that answer freshness and volume without a data scan.
- A schema fingerprint helper (`datasets.schema_fingerprint`) and a read-only SQL guard (`src/local_data_platform/mcp_server/guard.py`).
- After 0.1.3, the scheduler and the exit-code table. After 0.1.4, metrics, the HTML shell and the gold catalog.

**What this milestone adds**

- SLA settings in the dataset config (`metadata.sla`) (S)
- `ldp monitor` with a freshness monitor and a monitor history (M)
- Volume monitor against the run history (S)
- Schema drift monitor (S)
- Metric monitors on 0.1.4 metrics for business anomalies (S, links #97)
- Alerts to Slack, a webhook or a local file through one notifier (M, links #106 and #97)
- `ldp status`, one screen for every pipeline's health (M)
- SLA status for agents in the MCP server (S)
- Keep the `_ldp` history tables small (S)
- Monitoring guide, `make demo-monitor` and a scheduler recipe (S)

**The demo**

```bash
make install
make demo-monitor
ldp monitor ldp_demo/restaurant/config --now NOW_PLUS_50_HOURS
ldp status ldp_demo/restaurant/config
ldp status ldp_demo/restaurant/config --html ldp_demo/restaurant/status.html
ldp alert test ldp_demo/restaurant/config/orders.json
```

- The demo loads restaurant orders, then runs `ldp monitor` 50 hours later. It reports a freshness error, exits with 2 and writes one alert to `ldp_demo/restaurant/.ldp/alerts.jsonl`.
- A short batch raises a volume error, a batch with a new column raises a schema warning and a day with a revenue drop raises an error on the 0.1.4 `revenue` metric.
- A fresh batch sends one resolved alert per monitor, and a second `ldp monitor` sends nothing.
- With `LDP_SLACK_WEBHOOK` set, the same alerts reach Slack. Without it, the demo is fully offline.
- The revenue step needs the 0.1.4 gold tables, so it runs when the `dbt` extra is installed and prints a skip line otherwise.

**Key decisions**

- **Where SLAs live.** `metadata.sla`, kept out of the spec hash.
- **Freshness shape.** dbt style `warn_after` and `error_after`, so learners meet one shape twice.
- **Where results go.** A new `_ldp.monitor_results` table, or `.ldp/monitor.jsonl` when a config has no Iceberg sink. Alert state comes from the previous result, so there is no state file.
- **Status view.** A command-line table plus one static HTML file on the 0.1.4 page shell, with no server and no port.
- **Alert channels.** Slack incoming webhook, a generic JSON webhook and a local file, all in `src/local_data_platform/notify.py`. Email and Microsoft Teams can follow later.
- **Anomaly rule.** Fixed bounds plus percent change against the median of recent runs, with at least 3 runs of history.
- **Metric monitors.** They name a 0.1.4 metric. Raw SQL is only a fallback for tables without `metrics.json`.
- **What runs the monitor.** The 0.1.3 scheduler or cron. There is no second scheduler inside monitoring.

**Out of scope or moved later**

- Email and Microsoft Teams alerts.
- Statistical anomaly scores such as z-scores, which can be added without changing the config shape.
- Alerts while the laptop is asleep. A heartbeat to an outside service such as Healthchecks.io is the only cover, and the guide says so.

## 0.1.6 Business Intelligence Reporting Dashboarding

**Goal.** Turn the 0.1.4 gold tables into reports people read and act on. Reports are single HTML files that open offline with no server, and the same report goes to Slack or a webhook on a schedule without double posts. Every report shows a data health panel from the 0.1.5 monitors. A local marimo dashboard for exploring and recipes for Superset, Metabase and Evidence go further. This milestone also closes #106 by keeping a shared Google Sheet in sync on the way in and posting the scheduled report on the way out.

**What already exists**

- A read-only, guarded DuckDB sandbox with a row cap and a timeout, and table discovery from config folders (`src/local_data_platform/mcp_server/sandbox.py`, `src/local_data_platform/mcp_server/discovery.py`).
- Table-browser functions that need no MCP SDK (`LakeTools` in `src/local_data_platform/mcp_server/tools.py`).
- Atomic writes to local, `s3://` and `gs://` paths (`src/local_data_platform/fs.py`).
- After 0.1.3, `kind` dispatch, window shortcuts and the scheduler. After 0.1.4, releases, metrics, the HTML shell and `gold.duckdb`. After 0.1.5, `notify.py` and `monitoring.status.rows`.

**What this milestone adds**

- Report specs and `ldp report check` (M)
- Static HTML reports with built-in charts and `ldp report build` (M)
- Data health panel on every report from 0.1.5 status (S)
- Report site that builds a folder of reports into one index (S)
- Send reports to Slack and webhooks with `ldp report send` (S, links #106)
- Keep a link-shared Google Sheet in sync on a schedule (S, continues #106)
- Scheduled report delivery with the 0.1.3 scheduler (S)
- BI tool recipes on `gold.duckdb` (S)
- Optional live dashboard with `ldp dashboard` and the `notebook` extra (M)
- Restaurant reports and `make demo-bi` (S, links #97)
- Reporting and dashboards guide, design contract and changelog (S)

**The demo**

```bash
make install
make demo-bi
ldp report check ldp_demo/restaurant/reports
ldp report build ldp_demo/restaurant/reports --window 2026-07-31/2026-08-01 --out ldp_demo/restaurant/site
ldp report send ldp_demo/restaurant/reports/daily_sales.json --window 2026-07-31/2026-08-01 --dry-run
ldp runs ldp_demo/restaurant/reports/daily_sales.json
pip install -e ".[notebook]"
ldp dashboard --config ldp_demo/restaurant/config --reports ldp_demo/restaurant/reports
```

- `site/index.html` opens with the network off. It shows KPI tiles for orders, revenue and average ticket from the 0.1.4 metrics, a revenue bar chart by restaurant, a revenue line, a top menu items table and a data health panel.
- The demo sends the daily report twice to a local webhook stub. The first send delivers and the second prints that it was already delivered and posts nothing.
- `ldp runs` lists the build, the delivery and the skipped duplicate.
- The optional dashboard serves on 127.0.0.1 with Tables, SQL, Reports and Health pages.

**Key decisions**

- **First-party BI.** Static HTML reports drawn by LDP in pure Python with inline SVG charts, plus `gold.duckdb` from 0.1.4 as the bridge to external BI tools.
- **Live dashboard.** marimo with Altair in the `notebook` extra, which 0.1.7 reuses.
- **What reports read.** Each table at its snapshot in the current 0.1.4 release. `--live` reads `main`.
- **KPIs.** A KPI, bar or line block can name a 0.1.4 metric instead of a query.
- **Delivery.** Slack incoming webhooks and generic webhooks through `notify.py`, sent once per window by idempotency key.
- **Stale data.** A report held by the freshness gate exits with 3, as the 0.1.3 table says.
- **Superset.** A Docker recipe only, because apache-superset 6.1.0 pins SQLAlchemy below 2 and pyarrow below 19.

**Out of scope or moved later**

- Private Google Sheets through a service account is an optional 0.2.0 issue.
- Email delivery, Slack file uploads through a bot token, PDF or PNG export and multi-user auth for the dashboard.
- More chart types than KPI, bar, line, table and text. The live dashboard and external BI tools cover the rest.
- If time runs short, the live dashboard is the issue that moves to 0.1.7.

## 0.1.7 Data Science Insights

**Goal.** Turn the tables a learner or a small business already loads into the answers a data scientist is asked for first. That means churn, cohort retention, funnels, sessions and reply rates, plus training sets with stable train and test splits and no leakage. Everything runs from `ldp` or a notebook on a laptop, and every result is an ordinary quality-checked Iceberg table or a pinned dataset, so the scheduler, monitors, reports and agents from earlier milestones pick it up.

**What already exists**

- Reproducible pinned datasets with load checks and export (`src/local_data_platform/datasets.py`).
- Micro-batch idempotency and partitioning that suit event data (`run_config(window=...)` in `src/local_data_platform/etl.py`).
- Column profiling through `ldp query CONFIG "SUMMARIZE table"`.
- Safe identifier quoting for SQL parameters (`src/local_data_platform/engine/duckdb/row_filter.py`).
- After 0.1.2, `ldp query` over folders and `open_lake`. After 0.1.4, the `DUCKDB` source with `inputs`, CSV export and the churn metrics. After 0.1.6, the `notebook` extra.

**What this milestone adds**

- Insights as parameterized SQL templates that land as quality-checked gold tables (M)
- Churn and cohort retention insights on the restaurant orders (M)
- Ingest client events into a partitioned Iceberg events table (M, continues #89)
- Funnel, session and reply-rate insights over events (M)
- Point-in-time training datasets with reproducible train and test splits (M)
- A marimo notebook that runs in the editor and headless in CI (S)
- Profile a table with column statistics for people and agents (S)
- `make demo-insights`, the insights guide and release notes (S)

**The demo**

```bash
pip install -e ".[duckdb]"
make demo-insights
ldp insights list
ldp insights render ldp_demo/restaurant/insights/churn_rate.json
ldp query ldp_demo/restaurant/insights/churn_rate.json "SELECT * FROM churn_rate ORDER BY period"
ldp profile ldp_demo/restaurant/config/orders.json
ldp datasets export churn_train churn_train.csv --format csv --warehouse ldp_demo/restaurant/warehouse
pip install -e ".[notebook]"
python examples/restaurant/notebooks/churn.py --workdir ldp_demo/restaurant
```

- The demo loads three batches of app events, one of them replayed and skipped, then runs every insight and pins the churn training set.
- `ldp insights render` prints the exact SQL that `ldp run` executes.
- For every complete month, the churn insight matches the 0.1.4 `churn_rate` metric.
- The notebook scores a SQL baseline on the test split, and trains a logistic regression when scikit-learn is installed.

**Key decisions**

- **How an insight runs.** A named template in `metadata.insight`, run by `ldp run` through the `DUCKDB` source with `inputs`. Its output carries `metadata.docs` with layer gold.
- **Churn definition.** A customer churns in the period they stop ordering, the same rule as 0.1.2 and 0.1.4. The current incomplete period is left out.
- **Client events (#89).** Micro-batch JSONL files in a landing folder, each run as a window, with duplicates removed inside each insight.
- **Train and test split.** A split column from `md5_number`, because DuckDB's `hash()` may change between versions.
- **Modelling.** The library stops at reproducible data. Models live in the notebook.
- **Notebook format.** marimo files that run as plain Python, with a Jupyter copy made by `marimo export ipynb`.

**Out of scope or moved later**

- Event data at the scale of a billion users, which needs the 0.2.0 cloud work.
- An `ldp train` command and scikit-learn as a dependency.
- A status-based churn definition, until someone asks for it.

## 0.1.8 LLM

**Goal.** Ask the lakehouse a question in plain English and get back checked SQL, the rows and a short answer. Claude writes DuckDB SQL through the same read-only, audited sandbox that `ldp mcp` uses, fixes its own mistakes from DuckDB's hints, prefers the gold tables and metrics from 0.1.4 and is scored on an evaluation set. Catalog search (#59) finds the right table or the right record with no model and no network.

**What already exists**

- A read-only MCP server with six tools and an audit trail (`src/local_data_platform/mcp_server/tools.py`, `src/local_data_platform/mcp_server/audit.py`).
- A statement guard that lets exactly one SELECT or WITH through, and a locked sandbox with an allowlist, a row cap and a timeout (`guard.py`, `sandbox.py`).
- Deterministic offline datasets for an evaluation set (`ldp demo`, the robot example and, after 0.1.4, the restaurant gold tables).
- After 0.1.4, gold contracts in Iceberg table properties, `metrics.json` and the gold catalog entries in `gold/catalog.py`.

**What this milestone adds**

- Return DuckDB's correction hints with every failed query (S)
- Catalog search with `ldp search` and a `search_catalog` MCP tool (M)
- Search records in a table by plain-English query (M, continues #59)
- `ldp ask`, text to SQL with a self-correcting loop over the read-only tools (L)
- Ground ask and search on the 0.1.4 gold contracts and metrics (M)
- Evaluation set and the `ldp ask-eval` harness (M)
- `make demo-ask`, the Ask guide and the agents doc update (S)

**The demo**

```bash
pip install -e ".[dev,llm]"
make demo-ask
ldp search --config ldp_demo/restaurant/config "monthly customer churn"
ldp ask --config ldp_demo/restaurant/config "Which city lost the most customers in June 2026?"
ldp ask --config ldp_demo/restaurant/config "delete all orders"
ldp ask-eval examples/ask/eval/questions.jsonl --config ldp_demo/rides.json --config ldp_demo/robotics --config ldp_demo/restaurant/config --replay examples/ask/eval/cassettes
```

- Search ranks the gold churn table first, offline and with no key.
- `ldp ask` shows each SQL attempt, a corrected query after a DuckDB hint, the rows that LDP itself ran and a short answer. It prefers the `churn_rate` metric over hand-written SQL.
- The delete request is refused by the guard and the table's snapshot stays the same.
- With no API key the answers replay from recorded cassettes and say so. With `ANTHROPIC_API_KEY` set they call Claude live.

**Key decisions**

- **Model API.** Claude through the official `anthropic` SDK in an `llm` extra, behind a small protocol that tests replace with a scripted fake.
- **Default model.** `claude-opus-5-5` at effort medium, with `--model` and `--effort` flags. The evaluation decides any change.
- **Tool access.** A manual loop that calls `LakeTools.call()` in-process, so the guard, sandbox, allowlist and audit apply unchanged.
- **Data sent to the API.** Schema, metadata and query results capped at 50 rows. `--no-rows` sends no cell values.
- **Search ranking.** BM25 in the standard library for tables, and DuckDB full-text search for records when that extension is installed.
- **Grounding.** The keys 0.1.4 writes (`comment`, `ldp.owner`, `ldp.layer`, `ldp.grain`, `ldp.primary-key`, column docs) and metrics from `metrics.json`. Answers read the current 0.1.4 release, so they match the 0.1.6 reports.
- **Grading.** Execution accuracy. CI replays recorded cassettes offline, and a live run before each release blocks it on a drop of more than 5 points.

**Out of scope or moved later**

- Follow-up conversations with memory, charts in answers, live Snowflake or BigQuery querying and local open-weight models.
- Claude reranking of record search results and a live baseline for a second model are stretch items.

## 0.1.9 Launch Documentation

**Goal.** A beginner installs LDP from PyPI, follows the restaurant data from a raw file to a plain-English answer through every layer from 0.1.2 to 0.1.8, can look up any command or config key, and can make a first contribution. The docs are tested, so they cannot drift from the code.

**What already exists**

- `ldp demo`, a strict MkDocs build, recipes that CI runs (`tests/test_docs_recipes.py`), a quickstart and a contributing guide.
- Design notes for the pipeline layer (`docs/design/factory_registry.md`, `docs/design/v0_1_1.md`).
- A Read the Docs project and PyPI trusted publishing with a wheel smoke test.
- After 0.1.2 to 0.1.8, one guide per layer, written as a Learn chapter on `examples/restaurant`, and the shared block runner in `tests/docs_runner.py`.

**What this milestone adds**

- One mission and one navigation for the docs, with stale pages deleted (M, continues #29)
- Reference pages generated from the code for the CLI, config kinds and API (M)
- Onboarding from pip install to your own data in four commands (M, continues #107)
- Learn path that follows the restaurant data from a raw file to a plain-English answer (M)
- Class diagram of the package and a blog post on its design (M, continues #27)
- Docs home and Contribute Now page with a generated issue board (S, continues #94)
- README as the launch landing page, with the roadmap and project links (S, continues #6)
- Contributor templates, labels and starter issues (S, continues #53)
- Dependabot version updates for pip and GitHub Actions (S, continues #9)
- Release notes and a post-publish check for every release (S, continues #17)
- Read the Docs serves the released docs (S, continues #33)

**The demo**

```bash
python3 -m venv ldp-019
. ldp-019/bin/activate
pip install "local-data-platform[duckdb]==0.1.9"
ldp init sales.csv
ldp run configs/sales.json
ldp query configs/sales.json "select count(*) from sales"
make docs-test
make docs
make serve-docs
```

- `ldp init` writes a config for your own file, and the next two commands load and count it with no file edited by hand.
- In a clone, `make docs-test` runs every code block in `docs/learn/` and `docs/recipes.md` offline, and `make docs` fails on any page outside the navigation or a stale generated reference.
- `make serve-docs` shows the Learn path, the reference, the class diagram, the blog post and the issue board at `http://127.0.0.1:8000`. The live site opens on the docs for the version `pip` installs.

**Key decisions**

- **Learn path.** Chapters 03 to 09 are the milestone guides, moved under `docs/learn/`. This milestone writes chapters 01 and 02. `ldp demo --dataset restaurant` runs the `examples/restaurant` generator, which ships inside the package so it works after a pip install.
- **Docs tool.** Stay on MkDocs and Material for the launch, and try Zensical afterwards.
- **Issue board.** Three static tables (Trending, Top and New) generated on each Read the Docs build, with a committed copy as the fallback.
- **README roadmap.** A table of the milestones that links to this page. It also covers the visible-plan request in #12.

**Out of scope or moved later**

- A cross-post of the blog post to Medium and a video walk-through are stretch items.
- A move to Zensical is a follow-up after launch.
- Chapter 10, graduate to the cloud, comes with 0.2.0.

## 0.2.0 Cloud Integration

**Goal.** The same pipelines that run on a laptop run unchanged against cloud catalogs, cloud storage and Snowflake, on a schedule, from one container image. Every cloud path is tested in CI against local emulators, with no cloud account.

**What already exists**

- Pluggable catalogs (`local`, `sql`, `rest` and `glue`) with secrets read only from environment variables, and `ldp catalog test` (`src/local_data_platform/catalog/provider.py`).
- `s3://` and `gs://` file IO with atomic writes (`src/local_data_platform/fs.py`), tested on S3 against moto.
- The REST catalog tested against Apache Iceberg's REST fixture, opt-in (`tools/rest_fixture`).
- A BigQuery query source to copy for Snowflake (`src/local_data_platform/store/source/gcp/bigquery/__init__.py`).
- After 0.1.3, `ldp run --idempotency-key auto`, workflow files and schedules. After 0.1.9, the Learn path and the post-publish check.

**What this milestone adds**

- Profiles that run the same configs on a laptop and in the cloud (M)
- Postgres sql catalog with an install extra, a clear error and integration tests (S)
- Google Cloud Storage tested end to end against a local emulator (M)
- Hosted Iceberg REST catalogs tested against Polaris, with recipes for managed services (M)
- Snowflake source that pulls a query result into Iceberg or CSV (M, continues #75)
- Cloud runner with one container image and scheduled deployment recipes (M)
- Airflow DAGs from workflow files on managed Airflow (M)
- Private Google Sheets through a service account (S, optional, links #106)
- Graduate to the cloud with a Learn chapter, `make demo-cloud` and the 0.2.0 release (M)

**The demo**

```bash
pip install -e ".[dev,s3,postgres,snowflake]"
make demo-cloud
make postgres-test
make gcs-test
ldp pipelines
docker run --rm ghcr.io/tusharchou/local-data-platform:0.2.0 --version
```

- `make demo-cloud` starts a moto S3 server and runs the restaurant orders config with `--profile cloud --idempotency-key demo-1` twice. The second run is skipped. It then queries the table through the cloud profile and repeats the run inside the container when Docker is present.
- `ldp pipelines` now lists Snowflake to CSV and to Iceberg, and BigQuery to Iceberg.
- The container prints `ldp 0.2.0`.

**Key decisions**

- **Graduation.** Profiles in a `"kind": "Profiles"` file choose the catalog, the base path and the sinks, and the spec hash and idempotency keys do not change with the profile.
- **Cloud runner.** One image plus recipes for GitHub Actions, Google Cloud Run jobs and AWS ECS that reuse the 0.1.3 workflow and schedule files. There is no hosted control plane.
- **Snowflake.** A connector-based query source for #75, plus a Snowflake Horizon recipe through the existing `rest` catalog type.
- **Hosted REST.** Polaris runs in an opt-in workflow and Lakekeeper is optional. Three managed-catalog recipes are verified by hand, and the rest are marked not verified.
- **Test stores.** moto server instead of MinIO, whose repository is archived.
- **Azure storage.** Deferred until someone asks.

**Out of scope or moved later**

- A hosted control plane, agent and runner as sketched in `docs/design/saas_architecture.md`.
- Azure storage (`abfs://`).
- If the GCS emulator cannot serve an Iceberg warehouse without a real token, the `gs://` warehouse check becomes a manual recipe.

## Not planned

These issues are closed as not planned.

- **#12** A NEAR trader data API is not on this roadmap. This page and the README roadmap table answer its request for a visible plan.
- **#13** An issue-answering bot is not needed at current traffic, and pull requests already get AI review. Revisit after the 0.1.9 launch.
- **#31** Python 3.9 support. The package needs Python 3.12 or newer, and CI tests 3.12 and 3.13.
- **#101, #102, #103 and #105** Requests and advertisements unrelated to the data platform.
- **#104** A placeholder with no actionable content. The 0.1.9 contributor issue files real starter issues.
- **#108** A thank-you note, not a work item.
- **#111** Not a work item.
- **#114** A usage story, not a work item.

A few ideas raised during planning are not planned either.

- Partial publishing after a failed dbt node. Publishing stays all or nothing.
- Partition evolution after a table exists, and dbt snapshots.
- Unattended Colab Enterprise notebook schedules.
