# A local dashboard in the browser

`ldp dashboard` serves a web page on your own machine, with four pages. Tables lists the tables with their schema, their first rows and their snapshots. SQL runs one query and shows the result as a table and as a quick chart. Reports builds a report for a window, as `ldp report build` does, and can write its HTML page. Health shows the pipelines as `ldp status` does. It is for someone who wants to click through the lakehouse and ask it a question without writing Python.

The page is a [marimo](https://marimo.io) notebook that ships in the package, and it needs the `notebook` extra, which installs marimo and Altair. Without the extra the command prints how to install it and exits with 1. The pages are tested in CI with marimo's `App.run()`, and so is the start of the server. What a browser shows was looked at by hand, and the last time is on this page.

## Start it

<!-- docs-runner: skip it installs from PyPI -->
```bash
pip install "local-data-platform[notebook]"
```

The examples use the demo of `ldp demo`, which makes a warehouse with one table of rides.

```bash
ldp demo --workdir ldp_demo
```

<!-- docs-runner: output -->
```text
...
Done in ...s. Explore it yourself:
...
```

A report is a JSON file, as [Define a report](https://github.com/tusharchou/local-data-platform#define-a-report) in the README says. Put this one in `ldp_demo/reports/rides_by_city.json` for the Reports page.

```json
{
  "apiVersion": "ldp/v1", "kind": "Report", "identifier": "rides_by_city", "who": "analyst",
  "what": "Rides and fares by city",
  "metadata": {
    "title": "Rides by city",
    "tables": {"config": ["../rides.json"], "allow": ["demo.*"]},
    "queries": {"by_city": {"sql": "SELECT city, count(*) AS rides, round(avg(fare), 2) AS avg_fare FROM rides GROUP BY 1 ORDER BY 2 DESC"}},
    "blocks": [
      {"type": "bar", "title": "Rides by city", "query": "by_city", "x": "city", "y": "rides"},
      {"type": "table", "query": "by_city"}
    ]
  }
}
```

Then start the dashboard.

<!-- docs-runner: skip it starts a server that runs until it is stopped with Ctrl-C -->
```bash
ldp dashboard --config ldp_demo/rides.json --reports ldp_demo/reports
```

The command checks the extra and the sources, prints a line that says what it will show, and starts marimo, which opens the page in your browser and prints its address, `http://localhost:2718` by default. Ctrl-C stops it. A mistake is an error in the terminal and not a page in the browser. The extra is missing, a path is not there, or no table can be shown.

| Option | What it does |
|---|---|
| `--config PATH` | A dataset config, or a folder of configs. Every Iceberg source and target in it is shown, and the Health page lists its pipelines. It can be given more than once. |
| `--catalog SPEC` | A catalog spec file. Every table of the catalog is shown. It can be given more than once. |
| `--allow TABLES` | A comma-separated allowlist, as `ldp mcp --allow` takes it, such as `demo.*`. Without it every table is shown except the `_ldp` system tables. |
| `--reports DIR` | A folder of report files, or one report file, for the Reports page. |
| `--port N` | The port to listen on. Without it marimo uses 2718, or the next free port. |
| `--no-browser` | Does not open the browser. The address is printed. |
| `--edit` | Opens the notebook for editing, see [Change the notebook](#change-the-notebook). |

## The four pages

- **Tables.** A list of every table with its layer, owner, row count, when it last changed, what it means and how it is read. Pick a table to see what it says about itself, its schema with the description of each column, its first 10 rows and its 20 newest snapshots, each with what it added and deleted. All of it comes from the tools `list_tables`, `describe_table`, `sample_rows` and `table_history` of [`ldp mcp`](agents.md).
- **SQL.** One query, in an editor with SQL colours. **Run the query** sends it, and the result stays until the next one is run. It shows the rows as a table, with the time that the query took and a note when the row cap cut it, and a quick chart of the result. The chart starts as a bar chart of the first column that is not a number against the first number, or as a line when that column is a date or a time, and you can change the kind and the two columns. A result with no number has no chart, and the page says so.
- **Reports.** Pick a report of `--reports` and a window, `yesterday`, `last-week`, `last-month`, none, or one of your own such as `2026-07-31/2026-08-01`. The report is built as `ldp report build` builds it, and its page is shown, with a line for each table that it read and the release of it. **Write the HTML file** writes the same page to a path, which starts as the identifier of the report and `.html` in the folder that the dashboard was started in. A report that has a problem, or that `metadata.freshness` holds back, is a message that says why, and nothing is written.
- **Health.** The rows of `ldp status` for each pipeline of `--config`, the worst first, and a line that counts them. A config that cannot be read is a line too.

## What it can and cannot do

- **It listens on 127.0.0.1 only.** The command gives marimo `--host 127.0.0.1` and no option changes it. Nothing on another machine can reach the page. There is no login, so a program that runs on your machine as another user can reach it too, which matters on a shared machine and not on a laptop. The command turns off marimo's check for a new version, and the page loads everything from the local server.
- **It reads and never writes the lake.** Every query that a person writes, and every call that lists, describes, samples or reads the history of a table, goes through [`LakeTools`](agents.md), the tools of `ldp mcp`. The statement guard, the locked-down DuckDB, the row cap of 500 rows and the timeout of 30 seconds apply to each, so `DELETE`, `COPY`, `ATTACH` and a file outside the tables are refused, with the reason on the page. The Health page reads the run events and the monitors as `ldp status` does and writes nothing.
- **A report is read by the report engine.** It uses the same sandbox and the same guard, with the row cap and the timeout of the report, but it runs the queries of the report file and not text that a person typed. **Write the HTML file** records the build as run events in the sinks of the report, as the command does. A preview records none.
- **Every call is audited as the dashboard.** The records go to the JSONL file of `ldp mcp`, `<warehouse>/.ldp/audit/mcp_audit.jsonl`, and the tool name has `dashboard.` in front of it. The records of the pages are `dashboard.list_tables`, `dashboard.describe_table`, `dashboard.sample_rows`, `dashboard.table_history` and `dashboard.query`, and each report that the page builds or writes is `dashboard.report`, with the report and the window in its arguments. The dashboard does not write the `_ldp.audit` table, because that table is appended when the tools are closed and a page that is closed in the browser leaves no moment for it.
- **A big integer is shown as text.** A whole number beyond 2^53 would be rounded by the browser, so a snapshot id, or any such number in a result, is shown as text and is exact.

## Change the notebook

`ldp dashboard --config ldp_demo/rides.json --edit` opens the notebook in the marimo editor, so that you can add a page or change one. It opens a copy, `ldp_dashboard.py` in the current folder, which is made the first time and is never overwritten, so your changes stay and the installed package is not touched. The editor runs any Python that you write, so the guard of the dashboard does not apply to what is written there. The pages work from a few plain values, `selected_table`, `sql_to_run`, `report_choice`, `window_choice`, `write_requested` and `write_path`, which the controls set, and the notebook reads what the command was given from the environment variable `LDP_DASHBOARD_SETTINGS`. The code that decides what a page shows is in `local_data_platform.dashboard.service` and runs without marimo.

## Versions

The `notebook` extra is pinned to the minor releases that the tests ran on, `marimo>=0.26,<0.27` and `altair>=6.3,<6.4`, because both change what they draw between minor releases. A later release of the project reuses this extra.

**Last looked at by hand 2026-11-27** with marimo 0.26.0 and Altair 6.3.0, on macOS with Python 3.12, in a Chromium based browser. The four pages, the chart, a refused statement, the report page, the written file and Ctrl-C were tried. A Linux host, Windows and other browsers were not.

## Troubleshooting

- **`ldp dashboard needs marimo and altair`.** Install the extra with `pip install "local-data-platform[notebook]"`.
- **The page says that the dashboard could not start.** It was started in another way than `ldp dashboard`, which sets what it needs. Start it with the command.
- **The port is taken.** Give `--port` a free one.
- **The Reports page says that there is no report.** Start the command with `--reports DIR`, with a folder that has report files.
