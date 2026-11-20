"""A small restaurant chain on a local Iceberg lakehouse, from Excel and CSV exports to tested tables, with dbt.

The example follows one dataset through the layers of a company's data platform, and every milestone of the roadmap
adds a layer to it. The ``warehouse`` layer takes the restaurant system's exports, loads them
into Iceberg, models them with dbt, tests them, and publishes the models back to Iceberg as versioned tables,
offline, in under a minute:

1. **Excel to CSV.** The restaurant list is an Excel workbook with a title row above its header. ``ExcelToCSV`` turns
   its sheet into a CSV file, as you would to share it from a Google Drive folder.
2. **Ingest.** The restaurants, the customers, the menu, the reviews and the first batch of orders and order lines
   (January to June 2026) land in the raw namespace of the lakehouse. The orders are partitioned by ``day(order_ts)``
   and ``restaurant_id``.
3. **Model with dbt.** ``ldp dbt build`` registers the raw tables as sources, runs the staging views, the incremental
   ``fct_orders``, and the gold models ``churn_by_month``, ``gold_daily_sales``, ``gold_menu_item_weekly`` and
   ``gold_restaurant_scorecard``, runs their data tests, and publishes the marked models to Iceberg.
4. **Churn.** A customer churns in the month they stop ordering. The first batch gives the churn for February to June.
5. **Batch 2.** July arrives, and with it five late orders from the end of June. The build upserts only the new and
   late rows of ``fct_orders``, and rebuilds the gold tables.
6. **Time travel.** The late orders change history: June's churn falls. ``ldp query --snapshot`` reads June's churn as
   it was published after batch 1.

The ``orchestration`` layer runs the restaurant's nightly drops as a workflow, the way a scheduler would:

1. **Three drops, one bad row.** The restaurant system exports the orders and the order lines of a day into
   ``drops/``. The file of 3 August has a channel that the checks do not accept.
2. **A workflow.** ``workflows/daily_orders.json`` has the tasks ``orders``, ``order_lines``, ``models`` (the dbt
   project of the warehouse layer), ``gold`` (the gold build and its release) and ``tidy``, and ``ldp workflow plan``
   shows them with the key of the window.
3. **A backfill that stops at the gate.** ``ldp backfill`` publishes 1 and 2 August, and a data gate holds 3 August
   back, so its models, its gold build and its tidy task do not run.
4. **Fix and resume.** After the row is fixed, the same command runs only 3 August, and a third run skips every day.
5. **The next night.** ``ldp schedule tick`` loads 4 August when its time comes, ``ldp runs`` and ``ldp commits`` show
   what happened, and ``ldp schedule install`` prints the recipe that calls the tick.

The ``gold`` layer turns the same tables into a data mart that people who are not engineers can use:

1. **Raw tables.** The restaurants, the customers, the menu, the reviews, and the orders and order lines of January to
   July land in the raw namespace.
2. **A faulty batch.** The first week of August arrives as one file with three faults, a negative amount, an order of a
   restaurant that does not exist and an order that is in the file twice. The checks of the orders config stop it, and
   each failed check is printed.
3. **The same week, fixed.** It loads, and the models read it.
4. **Models.** ``ldp dbt build`` builds the models, passes their data tests and publishes the gold tables, each with its
   contract and its gate.
5. **A release.** ``ldp gold build`` checks every gold table and cuts a release, which is what readers see.
6. **A metric.** ``ldp metrics query`` gives revenue, orders and the average ticket by cuisine and month, as
   ``config/metrics.json`` defines them.
7. **For people.** An Excel file of the scorecard, one DuckDB file with every gold table, and a catalog page.

The ``monitoring`` layer watches the orders the way a job that runs every morning would, and shows what each monitor
says when something goes wrong and what the alerts say. It runs real ``ldp`` commands, and ``--now`` moves the clock of
the monitors, so that nothing has to wait:

1. **An SLA.** The orders config of the work folder gets ``metadata.sla``, the ``iceberg`` and ``jsonl`` sinks and a
   file channel for alerts. A normal day is 8 orders. A table is late after 26 hours, too late after 48, short under 5
   rows, and a day of revenue is too low under 1000.
2. **A late table.** The night of 3 August is loaded, and nothing arrives for 4 and 5 August. ``ldp monitor`` with
   ``--now`` 50 hours after the newest order reports a freshness error and exits with 2, and one alert is written to
   ``.ldp/alerts.jsonl``.
3. **A short batch.** The night of 6 August has 3 orders of 8. The table is fresh again, so the freshness alert is
   resolved, and the volume monitor raises an error.
4. **A new column.** 7 August has a ``discount`` column. The volume alert is resolved, and the schema monitor warns
   once.
5. **A revenue drop.** 8 August has all its orders at a tenth of their amounts. The metric monitor raises an error on
   ``revenue``, which it reads from the gold tables. This step needs the ``dbt`` extra, and without it a skip line is
   printed and the other steps run on the raw orders.
6. **A fresh batch.** 9 August is normal, so the revenue alert is resolved, and a second ``ldp monitor`` has nothing to
   send.
7. **One screen.** ``ldp status`` and ``ldp status --html``.

With ``LDP_SLACK_WEBHOOK`` set, the same alerts also go to that Slack webhook. Without it the layer is fully offline.

Run it from anywhere::

    python examples/restaurant/run.py                              # workdir: examples/restaurant/data
    python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer warehouse
    python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer orchestration
    python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer gold
    python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer monitoring

The ``warehouse`` and ``gold`` layers need the ``dbt`` and ``excel`` extras:
``pip install "local-data-platform[dbt,excel]"``. Without them they say so and exit with 1. The ``orchestration`` layer
needs neither. It loads the four small tables that it needs itself, and it runs the ``models`` and ``gold`` tasks when
the ``dbt`` extra is installed and prints a skip line when it is not. The ``monitoring`` layer needs neither either, and
runs its revenue step when the ``dbt`` extra is installed. ``--layer`` runs one layer and the layers that it needs
first, which are none so far.
"""

import argparse
import contextlib
import csv
import datetime as dt
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from local_data_platform import Config
from local_data_platform import metrics as metric_layer
from local_data_platform.cli import format_table
from local_data_platform.cli import main as ldp_main
from local_data_platform.exceptions import EngineNotFound
from local_data_platform.format.iceberg.commit import commit_log
from local_data_platform.gold import release as releases
from local_data_platform.lake import open_lake
from local_data_platform.monitoring.history import read_results
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.pipeline.registry import create_pipeline

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE / "config"
DBT_DIR = HERE / "dbt"
WORKFLOWS_DIR = HERE / "workflows"
DEFAULT_WORKDIR = HERE / "data"
MARKER = ".restaurant_workdir"
WORKDIR_FOLDERS = ("landing", "warehouse", "config", "dbt", "workflows", "drops", "out", ".ldp")
EXTRAS_HINT = 'pip install "local-data-platform[dbt,excel]"'
DBT_HINT = 'pip install "local-data-platform[dbt]"'

LAYERS = ("warehouse", "orchestration", "gold", "monitoring")
"""The layers of the example, in order. Each milestone adds one."""

LAYER_NEEDS = {"warehouse": (), "orchestration": (), "gold": (), "monitoring": ()}
"""The layers that a layer runs first. None needs another: ``orchestration`` loads the four small tables it needs
itself, so it runs without the dbt and excel extras that ``warehouse`` and ``gold`` need, ``gold`` loads the raw
tables itself, so that it shows the data mart and not the history of the warehouse, and ``monitoring`` loads the days
of its own story."""

RAW_LOADS = (("restaurants", "ExcelToIceberg", "overwrite"), ("customers", "CSVToIceberg", "overwrite"),
             ("menu_items", "CSVToIceberg", "overwrite"), ("reviews", "CSVToIceberg", "overwrite"),
             ("orders", "CSVToIceberg", "append"), ("order_lines", "CSVToIceberg", "append"))
"""The raw tables, in the order they load: the config, the route it takes and how it writes. The four small tables
are written whole each time, and the orders and their lines are appended, a batch at a time."""

WAREHOUSE_TABLES = (("raw.restaurants", "restaurants"), ("raw.customers", "customers"),
                    ("raw.menu_items", "menu_items"), ("raw.reviews", "reviews"), ("raw.orders", "orders"),
                    ("raw.order_lines", "order_lines"), ("silver.fct_orders", "fct_orders"),
                    ("gold.churn_by_month", "churn_by_month"), ("gold.gold_daily_sales", "gold_daily_sales"),
                    ("gold.gold_menu_item_weekly", "gold_menu_item_weekly"),
                    ("gold.gold_restaurant_scorecard", "gold_restaurant_scorecard"))
"""Every table that the warehouse and the gold layers write, as ``(name, config)``."""

CHURN_SQL = ("SELECT month, customers_at_start, churned, round(churn_rate, 3) AS churn_rate "
             "FROM churn_by_month ORDER BY month")
JUNE_SQL = ("SELECT month, customers_at_start, churned, round(churn_rate, 3) AS churn_rate "
            "FROM churn_by_month WHERE month = DATE '2026-06-01'")
JOIN_SQL = ("SELECT c.month, round(c.churn_rate, 3) AS churn_rate, count(o.order_id) AS orders "
            "FROM churn_by_month c JOIN fct_orders o ON date_trunc('month', o.order_ts) = c.month "
            "GROUP BY ALL ORDER BY c.month")


def _load_generator():
    spec = importlib.util.spec_from_file_location("restaurant_generate", HERE / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = _load_generator()


# --------------------------------------------------------------------------- building blocks


def require_extras() -> None:
    """Fail with the install command when dbt or the Excel reader is missing.

    Raises:
        EngineNotFound: Naming the extras that are missing.
    """
    from local_data_platform.dbt_layer.project import find_dbt

    missing = []
    try:
        find_dbt()
    except EngineNotFound:
        missing.append("dbt")
    if importlib.util.find_spec("openpyxl") is None:
        missing.append("excel")
    if missing:
        raise EngineNotFound(f"The restaurant example needs the {' and '.join(missing)} "
                             f"extra{'s' if len(missing) > 1 else ''}. Install with: {EXTRAS_HINT}")


def prepare_workdir(workdir: str | Path, *, keep_metrics: bool = True) -> Path:
    """Create (or clear) the work folder and copy the configs, the dbt project and the workflow files into it.

    The configs' relative paths (``../landing/…``, ``../warehouse``) resolve against the folder they are in, so the
    copies in ``<workdir>/config`` also work with the CLI. The dbt project writes its published configs next to them.
    The files of ``<workdir>/workflows`` read the drops in ``<workdir>/drops``, and write their ledger next to them.

    Args:
        workdir: The work folder.
        keep_metrics: Whether the config folder keeps ``metrics.json``. The nightly workflow builds one gold table, and
            ``ldp gold build`` holds the metrics of its folder to the tables that exist, so that folder has no
            metrics file.

    Raises:
        ValueError: If the folder is not empty and was not made by this example, or one of its folders cannot be
            cleared. A run on top of what a failed clearing left behind would mix old files with new ones.
    """
    workdir = Path(workdir).expanduser().resolve()
    if workdir.is_dir() and any(workdir.iterdir()):
        if not (workdir / MARKER).is_file():
            raise ValueError(f"{workdir} is not empty and was not created by this example; pass an empty --workdir")
        for folder in WORKDIR_FOLDERS:
            try:
                shutil.rmtree(workdir / folder)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise ValueError(f"could not clear {workdir / folder} ({error}); remove it, "
                                 f"or pass another --workdir") from error
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / MARKER).write_text("Created by examples/restaurant/run.py, which clears this folder on each run.\n")
    shutil.copytree(CONFIG_DIR, workdir / "config")
    if not keep_metrics:
        (workdir / "config" / "metrics.json").unlink()
    shutil.copytree(DBT_DIR, workdir / "dbt", ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"))
    shutil.copytree(WORKFLOWS_DIR, workdir / "workflows", ignore=shutil.ignore_patterns(".ldp"))
    return workdir


class Narrator:
    """Prints the walkthrough, and nothing when ``quiet``."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.step = 0
        self.last_error = ""
        """What the last ``ldp`` command wrote to standard error."""

    def section(self, title: str) -> None:
        self.step += 1
        self.say(f"\n== {self.step}. {title}")

    def say(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    def table(self, rows: Any) -> None:
        if not self.quiet:
            print("\n".join("   " + line for line in format_table(rows, float_digits=None).splitlines()))


def shown(path: Path) -> str:
    """``path`` relative to the current folder when it is below it, as a person would type it, else as it is."""
    try:
        relative = os.path.relpath(path)
    except ValueError:
        return str(path)
    return str(path) if relative.startswith("..") else relative


class StepFailed(RuntimeError):
    """An ``ldp`` command of the walkthrough exited with a failure."""


class _Kept:
    """What :func:`keep_fd` kept, as text, once its block has ended."""

    text = ""


@contextlib.contextmanager
def keep_fd(fd: int):
    """Keep what is written to the file descriptor ``fd``, also by a child process, instead of letting it through.

    A workflow with a dbt task starts dbt, which writes its log to the descriptor of standard error, so Python's own
    redirection does not catch it.
    """
    kept = _Kept()
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(fd)
    with tempfile.TemporaryFile() as sink:
        os.dup2(sink.fileno(), fd)
        try:
            yield kept
        finally:
            os.dup2(saved, fd)
            os.close(saved)
            sink.seek(0)
            kept.text = sink.read().decode(errors="replace")


def ldp(out: Narrator, *argv: Any, ok: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """Run an ``ldp`` command in this process, print it and what it printed, and return ``(status, stdout)``.

    What the command writes to standard error is kept. It is shown when the command fails, and when it ends with a
    status that is in ``ok`` and is not 0, because that is a status that the walkthrough is about to explain.

    Args:
        ok: The exit statuses that do not end the walkthrough, such as 3 for a data gate that held a window back.

    Raises:
        StepFailed: If the status is not in ``ok``. Its message is what the command printed to stderr.
    """
    arguments = [str(item) for item in argv]
    typed = [shown(item) if isinstance(item, Path) else str(item) for item in argv]  # as a person would type it
    out.say(f"   $ {' '.join(['ldp'] + [f'"{item}"' if ' ' in item else item for item in typed])}")
    stdout, stderr = io.StringIO(), io.StringIO()
    with keep_fd(2) as child, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = ldp_main(arguments)
    out.last_error = (stderr.getvalue() + child.text).strip()
    for line in stdout.getvalue().splitlines():
        out.say(f"     {line}")
    if status not in ok:
        raise StepFailed(f"ldp {' '.join(arguments[:2])} failed: {(stderr.getvalue() + child.text).strip()}")
    if status != 0:  # a status that the walkthrough expects, such as the 3 of a data gate, explains itself on stderr
        for line in stderr.getvalue().splitlines():
            out.say(f"     {line}")
    return status, stdout.getvalue()


def lake_rows(config: Path, sql: str, *, snapshot_id: int | None = None):
    """Run ``sql`` over the tables of ``config`` (a folder, or one config) and return the rows as dicts."""
    with open_lake(config, snapshot_id=snapshot_id) as lake:
        return lake.query(sql).to_pylist()


class BuildFailed(RuntimeError):
    """``ldp dbt build`` exited with a failure, so nothing was published."""


def dbt_build(workdir: Path, select: tuple[str, ...] = ()) -> dict[str, Any]:
    """Run ``ldp dbt build`` on the example's project, and return what it did.

    dbt's own output is long, so it is captured, and a summary is returned: the models and the data tests that ran, how
    many passed, how long it took, and the models that were published, each with its write mode, its rows before and
    after, and its snapshot.

    Args:
        workdir: The work folder, which has the dbt project.
        select: The dbt selection, such as ``("+gold_daily_sales",)``, for a build of part of the project. All of it
            when empty.

    Raises:
        BuildFailed: If the build failed. Its message is what ``ldp`` printed.
    """
    project = workdir / "dbt"
    started = time.perf_counter()
    selection = ["--select", *select] if select else []
    done = subprocess.run([sys.executable, "-m", "local_data_platform.cli", "dbt", "build", str(project), *selection],
                          capture_output=True, text=True, check=False)
    seconds = round(time.perf_counter() - started, 1)
    if done.returncode != 0:
        raise BuildFailed((done.stderr or done.stdout).strip()[-3000:])
    results = json.loads((project / "target" / "run_results.json").read_text())["results"]
    models = [item for item in results if item["unique_id"].startswith("model.")]
    tests = [item for item in results if item["unique_id"].startswith("test.")]
    published = [{"table": table, "mode": mode, "rows_before": int(before), "rows_after": int(after),
                  "snapshot_id": int(snapshot)}
                 for table, mode, before, after, snapshot in re.findall(
                     r"^ldp: published (\S+): (\w+), (\d+) -> (\d+) rows, snapshot (\d+)$", done.stdout, re.MULTILINE)]
    return {"models": len(models), "tests": len(tests),
            "passed": sum(item["status"] in ("success", "pass") for item in models + tests),
            "seconds": seconds, "published": published}


def say_build(out: Narrator, build: dict[str, Any]) -> None:
    out.say(f"   {build['models']} models and {build['tests']} data tests: {build['passed']} of "
            f"{build['models'] + build['tests']} passed, in {build['seconds']}s")
    for item in build["published"]:
        out.say(f"   published {item['table']}: {item['mode']}, {item['rows_before']} -> {item['rows_after']} rows "
                f"(snapshot {item['snapshot_id']})")


# --------------------------------------------------------------------------- the warehouse layer


def ingest(config: Path, name: str):
    """Run the ingest config ``<config>/<name>.json``, and return what the pipeline did."""
    return create_pipeline(Config.from_json(config / f"{name}.json")).run()


def load_raw(config: Path, loads: tuple[tuple[str, str, str], ...] = RAW_LOADS) -> list[dict[str, Any]]:
    """Load the raw tables of ``loads`` from the files in ``landing``, and say how each load went."""
    rows = []
    for name, route, mode in loads:
        result = ingest(config, name)
        rows.append({"table": f"raw.{name}", "rows": result.write_result.rows_after, "route": route, "mode": mode,
                     "checks": f"{len(result.quality)} passed" if result.quality.passed else "FAILED"})
    return rows


def table_rows(config: Path, tables: tuple[tuple[str, str], ...]) -> dict[str, dict[str, int]]:
    """The rows and the snapshots of each table of ``tables``, which are ``(name, config)`` pairs."""
    found = {}
    for name, stem in tables:
        table = iceberg_from_config(Config.from_json(config / f"{stem}.json"), "target")
        found[name] = {"rows": table.row_count(), "snapshots": len(table.snapshots())}
    return found


def run_warehouse(workdir: Path, dataset: Any, out: Narrator) -> dict[str, Any]:
    """Load batch 1, build, load batch 2 and rebuild, and tell the story of June's churn."""
    config = workdir / "config"
    landing = workdir / "landing"
    summary: dict[str, Any] = {}

    def run(name: str):
        return ingest(config, name)

    generate.write_static(landing, dataset)
    generate.write_batch(landing, dataset, "batch1")
    batch1, batch2 = dataset.batch_orders("batch1"), dataset.batch_orders("batch2")
    late = [order for order in batch2 if order["order_ts"].date() <= generate.BATCH1_END]

    out.section("Excel to CSV: the restaurant list")
    out.say("   The export is landing/restaurants.xlsx. Its sheet 'Restaurants' has a title row above the header,")
    out.say("   so the config says header_row 2.")
    result = run("restaurants_to_csv")
    out.say(f"   ExcelToCSV: read {result.rows_read} rows, wrote {result.rows_written} to landing/restaurants.csv")
    summary["excel_rows"] = result.rows_written

    out.section("Ingest the raw tables into Iceberg (namespace raw)")
    rows = load_raw(config)
    out.table(rows)
    orders = iceberg_from_config(Config.from_json(config / "orders.json"), "target")
    partitions = orders.table().inspect.partitions().num_rows
    out.say(f"   Batch 1 is the {len(batch1)} orders from January to June, in {partitions} partitions by "
            f"day(order_ts) and restaurant_id,")
    out.say(f"   and their {len(dataset.batch_order_lines('batch1'))} lines.")
    summary["raw_rows"] = {row["table"]: row["rows"] for row in rows}
    summary["partitions"] = partitions

    out.section("Model it with dbt: ldp dbt build")
    out.say("   ldp registers the raw tables as dbt sources, runs dbt, and publishes the models marked ldp_publish to")
    out.say("   Iceberg, but only when every model and every data test passed.")
    out.say(f"   $ ldp dbt build {shown(workdir / 'dbt')}")
    first = dbt_build(workdir)
    say_build(out, first)
    summary["first_build"] = first

    out.section("Churn by month, February to June")
    out.say("   A customer churns in the month they stop ordering. For month M, customers_at_start is the number of")
    out.say("   customers with an order in month M minus 1, churned is how many of them have no order in month M,")
    out.say("   and churn_rate is churned divided by customers_at_start. The first month has no row.")
    _, _ = ldp(out, "query", config / "churn_by_month.json", CHURN_SQL)
    summary["churn_after_batch_1"] = lake_rows(config / "churn_by_month.json", CHURN_SQL)
    june_published = next(item for item in first["published"] if item["table"] == "gold.churn_by_month")
    summary["churn_snapshot_after_batch_1"] = june_published["snapshot_id"]

    out.section(f"Batch 2 arrives: {len(batch2) - len(late)} orders from July and {len(late)} late ones from June")
    generate.write_batch(landing, dataset, "batch2")
    result = run("orders")
    lines = run("order_lines")
    out.say(f"   raw.orders: appended {result.rows_written} rows, now {result.write_result.rows_after}. "
            f"raw.order_lines: appended {lines.rows_written} rows, now {lines.write_result.rows_after}.")
    out.say(f"   The {len(late)} late orders were placed in June and reached the lake in July, "
            f"so their order_ts is old.")
    out.say("   fct_orders takes every order it has no row for, so it sees them.")
    out.say(f"   $ ldp dbt build {shown(workdir / 'dbt')}")
    second = dbt_build(workdir)
    say_build(out, second)
    fct = next(item for item in second["published"] if item["table"] == "silver.fct_orders")
    out.say(f"   fct_orders upserted {fct['rows_after'] - fct['rows_before']} rows, the new and the late ones, and no "
            f"other row was written.")
    summary["second_build"] = second
    summary["orders_upserted"] = fct["rows_after"] - fct["rows_before"]
    summary["churn_after_batch_2"] = lake_rows(config / "churn_by_month.json", CHURN_SQL)
    out.say("   Churn now, with July added and June changed:")
    ldp(out, "query", config / "churn_by_month.json", CHURN_SQL)

    out.section("Time travel: June's churn before and after the late orders")
    snapshot = summary["churn_snapshot_after_batch_1"]
    out.say(f"   churn_by_month has a snapshot for every build. Snapshot {snapshot} is the one after batch 1.")
    out.say("   June as it was published after batch 1:")
    ldp(out, "query", config / "churn_by_month.json", JUNE_SQL, "--snapshot", snapshot)
    out.say("   June now, with the late orders:")
    ldp(out, "query", config / "churn_by_month.json", JUNE_SQL)
    summary["june_before"] = lake_rows(config / "churn_by_month.json", JUNE_SQL, snapshot_id=snapshot)[0]
    summary["june_after"] = lake_rows(config / "churn_by_month.json", JUNE_SQL)[0]
    out.say("   One query over the whole lake folder joins two published tables:")
    ldp(out, "query", config, JOIN_SQL)

    summary["tables"] = table_rows(config, WAREHOUSE_TABLES)
    return summary


# --------------------------------------------------------------------------- the orchestration layer

BAD_CHANNEL = "drive_thru"
"""A channel that the checks of the orders config do not accept."""

TICK_AT = "2026-08-05T02:05:00Z"
"""The moment of the tick. The schedule of the workflow fires at 02:05 UTC, and the fire of 5 August owns 4 August."""

LIST_AT = "2026-08-05T02:06:00Z"
"""A minute after the tick, when ``ldp schedule list`` looks at what is due and what comes next."""

ORDERS_PER_DAY_SQL = ("SELECT cast(order_ts AS date) AS day, count(*) AS rows FROM raw.orders "
                      "GROUP BY 1 ORDER BY 1")
LINES_PER_DAY_SQL = ("SELECT cast(order_ts AS date) AS day, count(*) AS rows FROM raw.order_lines "
                     "GROUP BY 1 ORDER BY 1")
GOLD_PER_DAY_SQL = "SELECT day, count(*) AS rows FROM gold.gold_daily_sales GROUP BY day ORDER BY day"

_COUNTS = re.compile(r"(\d+) published, (\d+) skipped, (\d+) blocked, (\d+) failed")


def dbt_installed() -> bool:
    """Whether dbt can be found, so that the ``models`` task can run."""
    from local_data_platform.dbt_layer.project import find_dbt

    try:
        find_dbt()
    except EngineNotFound:
        return False
    return True


def counts_of(stdout: str) -> dict[str, int]:
    """How many windows a backfill or a tick published, skipped, blocked and failed, from the line that says so.

    Raises:
        StepFailed: If the output has no such line.
    """
    found = _COUNTS.search(stdout)
    if found is None:
        raise StepFailed("the command did not say how many windows it published, skipped, blocked and failed")
    return dict(zip(("published", "skipped", "blocked", "failed"), map(int, found.groups())))


def change_channel(path: Path, channel: str) -> tuple[str, str]:
    """Set the channel of the first order of a drop, and return the id of that order and the channel it had."""
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle))
    header, first = rows[0], rows[1]
    column = header.index("channel")
    order_id, before = first[header.index("order_id")], first[column]
    first[column] = channel
    with path.open("w", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    return order_id, before


def without_models(spec: dict[str, Any]) -> dict[str, Any]:
    """A workflow without its dbt and gold tasks, where what waited for one of them waits for what that task waited for.

    A gold task builds what the dbt tasks publish, so there is nothing for it to build without them.
    """
    models = {task["id"]: task for task in spec["tasks"] if "dbt" in task or "gold" in task}
    tasks = []
    for task in spec["tasks"]:
        if task["id"] in models:
            continue
        waits = list(dict.fromkeys(name for waited in task.get("depends_on", [])
                                   for name in _waits_without(waited, models)))
        tasks.append({**task, "depends_on": waits} if waits else task)
    return {**spec, "tasks": tasks}


def _waits_without(name: str, removed: dict[str, dict[str, Any]]) -> list[str]:
    """What a task that waits for ``name`` waits for when the tasks of ``removed`` are not there."""
    if name not in removed:
        return [name]
    return [found for waited in removed[name].get("depends_on", []) for found in _waits_without(waited, removed)]


def load_dimensions(workdir: Path, dataset: Any) -> list[dict[str, Any]]:
    """Load the four small raw tables from CSV files, which the models of the dbt project read.

    They are ``raw.restaurants``, ``raw.customers``, ``raw.menu_items`` and ``raw.reviews``, and they go to the tables,
    with the checks, of the configs of the warehouse layer. The restaurants come from a CSV file here and from an Excel
    workbook there, so this needs no Excel reader.
    """
    landing, config = workdir / "landing", workdir / "config"
    generate.write_dimensions(landing, dataset)
    rows = []
    for name in ("restaurants", "customers", "menu_items", "reviews"):
        spec = json.loads((config / f"{name}.json").read_text())
        spec["metadata"]["source"] = {"name": name, "format": "CSV", "path": f"../landing/{name}.csv"}
        for check in spec["metadata"]["quality"]["checks"]:
            if check["check"] == "schema":  # a CSV file gives seconds, where the workbook gave microseconds
                check["columns"] = {column: "timestamp[s]" if kind.startswith("timestamp") else kind
                                    for column, kind in check["columns"].items()}
        spec["metadata"].pop("observability", None)
        result = create_pipeline(Config.from_dict(spec, base_dir=config)).run()
        rows.append({"table": f"raw.{name}", "rows": result.write_result.rows_after, "source": f"landing/{name}.csv",
                     "checks": f"{len(result.quality)} passed" if result.quality.passed else "FAILED"})
    return rows


def rows_per_day(workdir: Path, models: bool) -> list[dict[str, Any]]:
    """The rows that each day has in the raw tables, and in the gold table when the models have run."""
    workflows = workdir / "workflows"
    orders = {str(row["day"]): row["rows"] for row in lake_rows(workflows, ORDERS_PER_DAY_SQL)}
    lines = {str(row["day"]): row["rows"] for row in lake_rows(workflows, LINES_PER_DAY_SQL)}
    gold = {str(row["day"]): row["rows"] for row in lake_rows(workdir / "config", GOLD_PER_DAY_SQL)} if models else {}
    return [{"day": day, "raw.orders": orders.get(day, 0), "raw.order_lines": lines.get(day, 0),
             **({"gold.gold_daily_sales": gold.get(day, 0)} if models else {})}
            for day in sorted(orders)]


def run_orchestration(workdir: Path, dataset: Any, out: Narrator) -> dict[str, Any]:
    """Run the daily workflow for three days with a bad row in the third, fix it, and leave the fourth to a tick."""
    drops, workflows, config = workdir / "drops", workdir / "workflows", workdir / "config"
    workflow, ledger = workflows / "daily_orders.json", workflows / ".ldp" / "events.jsonl"
    days = generate.DROP_DAYS
    models = dbt_installed()
    summary: dict[str, Any] = {"models": models}

    def say_gold(when: str) -> None:
        if models:
            gold = ", ".join(f"{row['day']} {row['rows']} rows" for row in lake_rows(config, GOLD_PER_DAY_SQL))
            out.say(f"   gold.gold_daily_sales {when}: {gold}")

    out.section("Three nightly drops, and a bad row in the third")
    for day in days[:3]:
        generate.write_drop(drops, dataset, day)
    bad_order, channel = change_channel(drops / f"orders_{days[2]}.csv", BAD_CHANNEL)
    out.say("   The restaurant system exports the orders and the order lines of a day into drops/ every night.")
    out.say(f"   These are 1 to 3 August. In the file of 3 August, order {bad_order} has the channel '{BAD_CHANNEL}',")
    out.say("   which the checks of the orders config do not accept.")
    summary["bad_order"] = bad_order

    if models:
        out.section("Load the four small tables that the models read, once")
        out.say("   The dbt project reads raw.restaurants, raw.customers, raw.menu_items and raw.reviews as well as")
        out.say("   the orders and their lines. They change rarely, so they are loaded once, and the workflow only")
        out.say("   loads the days.")
        rows = load_dimensions(workdir, dataset)
        out.table(rows)
        summary["dimensions"] = {row["table"]: row["rows"] for row in rows}

    out.section("One workflow for the day: ldp workflow plan")
    out.say("   workflows/daily_orders.json says what runs, in which order, and when: 02:05 UTC, for the day before.")
    if models:
        out.say("   orders and order_lines wait for nothing, models (a dbt task) waits for both, gold builds the gold")
        out.say("   tables and cuts a release after models, and tidy waits for gold.")
    else:
        spec = json.loads(workflow.read_text())
        workflow.write_text(json.dumps(without_models(spec), indent=2) + "\n")
        out.say(f"   Skipping the models and gold tasks: dbt is not installed ({DBT_HINT}). Without it, tidy waits for")
        out.say("   orders and order_lines, which wait for nothing.")
    ldp(out, "workflow", "plan", workflow, "--window", f"{days[0]}/{days[1]}")

    out.section("Backfill the three days: the first run")
    out.say("   ldp backfill runs the workflow for every window of a range, oldest first. The end of the range is")
    out.say("   not part of it, so this is the 1st, 2nd and 3rd of August.")
    window = ("--from", days[0].isoformat(), "--to", days[3].isoformat())
    status, stdout = ldp(out, "backfill", workflow, *window, ok=(0, 3))
    summary["first_backfill"] = {"exit": status, **counts_of(stdout)}
    out.say(f"   Exit status {status}: a data gate held 3 August back, and the data has to change before another run")
    out.say("   helps, so a scheduler never retries a 3. The order lines of that day did load, because nothing makes")
    out.say("   them wait for the orders. The tasks that do wait for the orders did not run, as the ledger shows")
    out.say("   below.")
    say_gold("so far")

    out.section("Fix the row, and run the same command again")
    change_channel(drops / f"orders_{days[2]}.csv", channel)
    out.say(f"   The restaurant system sends 3 August again, with the channel '{channel}'. A window whose keys are on")
    out.say("   the tables is skipped without being read, so only 3 August runs.")
    status, stdout = ldp(out, "backfill", workflow, *window)
    summary["second_backfill"] = {"exit": status, **counts_of(stdout)}
    say_gold("so far")

    out.section("A third run does nothing")
    out.say("   Every window is done, so no file is read and nothing is written.")
    status, stdout = ldp(out, "backfill", workflow, *window)
    summary["third_backfill"] = {"exit": status, **counts_of(stdout)}

    out.section("The next night, a scheduler takes over: ldp schedule tick")
    generate.write_drop(drops, dataset, days[3])
    out.say("   The drop of 4 August lands. cron or launchd would call ldp schedule tick every few minutes, and a tick")
    out.say("   runs every window that is due and not done. --now gives it the time of 02:05 UTC on 5 August.")
    status, stdout = ldp(out, "schedule", "tick", workdir, "--now", TICK_AT)
    summary["tick"] = {"exit": status, **counts_of(stdout)}
    out.say("   What is scheduled, when it fires next in UTC and in local time, and what was missed:")
    ldp(out, "schedule", "list", workdir, "--now", LIST_AT)
    say_gold("now")

    out.section("What happened, from the ledger and from the table")
    out.say("   Every run of the workflow and of each task is in the ledger next to the workflow file, newest first.")
    out.say("   Look for 3 August: blocked_quality for orders, upstream_failed for what waited for it, and then the")
    out.say("   second run that fixed it.")
    ldp(out, "runs", ledger)
    out.say("   The table has one publish for each day, whatever happened in between:")
    _, stdout = ldp(out, "commits", workflows / "orders.json")
    out.say("   The recipe that calls the tick every 5 minutes, as a crontab line (launchd on a Mac):")
    ldp(out, "schedule", "install", "--print", "cron", workdir)

    orders = iceberg_from_config(Config.from_json(workflows / "orders.json"), "target")
    summary["publishes"] = len(commit_log(orders.table()))
    summary["days"] = rows_per_day(workdir, models)
    tables = {}
    for name, path in (("raw.orders", workflows / "orders.json"), ("raw.order_lines", workflows / "order_lines.json"),
                       *((name, config / f"{stem}.json") for name, stem in (
                           ("raw.restaurants", "restaurants"), ("raw.customers", "customers"),
                           ("silver.fct_orders", "fct_orders"),
                           ("gold.gold_daily_sales", "gold_daily_sales")) if models)):
        table = iceberg_from_config(Config.from_json(path), "target")
        tables[name] = {"rows": table.row_count(), "snapshots": len(table.snapshots())}
    summary["tables"] = tables
    return summary


# --------------------------------------------------------------------------- the gold layer

METRIC_ARGS = ("revenue,orders,avg_ticket", "--by", "cuisine", "--grain", "month", "--from", "2026-07-01")
"""The metric query that the gold layer prints: three metrics by cuisine and month, for July and August."""

_FAILED_CHECK = re.compile(r"^\s+- (\w+)\(", re.MULTILINE)


def run_gold(workdir: Path, dataset: Any, out: Narrator) -> dict[str, Any]:
    """Load the raw tables, stop a faulty batch at the gate, build the data mart, release it and hand it to people."""
    config, landing, outputs = workdir / "config", workdir / "landing", workdir / "out"
    outputs.mkdir(exist_ok=True)
    summary: dict[str, Any] = {}

    def raw_orders() -> int:
        return iceberg_from_config(Config.from_json(config / "orders.json"), "target").row_count()

    out.section("Load the raw tables")
    out.say("   The exports of the restaurant system are in landing/. Each table is loaded with the checks of its")
    out.say("   config. The orders and their lines come in batches. Batch 1 is January to June and batch 2 is July.")
    generate.write_static(landing, dataset)
    generate.write_batch(landing, dataset, "batch1")
    rows = {row["table"]: row for row in load_raw(config)}
    generate.write_batch(landing, dataset, "batch2")
    rows.update({row["table"]: row for row in load_raw(config, RAW_LOADS[4:])})
    out.table(list(rows.values()))
    summary["raw_rows"] = {name: row["rows"] for name, row in rows.items()}

    out.section("A faulty batch arrives, and the gate stops it")
    _, faults = generate.write_faulty_week(landing, dataset)
    before = raw_orders()
    out.say("   The restaurant system sends the orders of 1 to 7 August as one file, landing/orders.csv. It has three")
    out.say("   faults, a negative amount, a restaurant that does not exist and an order that is in the file twice,")
    out.say("   each in an order of its own:")
    for fault in faults:
        out.say(f"     {fault.order_id}  {fault.detail}")
    out.say("   The checks of config/orders.json look at the whole file before anything is written, and a check that")
    out.say("   fails stops the run. Each failed check is named.")
    status, _ = ldp(out, "run", config / "orders.json", ok=(3,))
    failed = _FAILED_CHECK.findall(out.last_error)
    after = raw_orders()
    out.say(f"   Exit status {status} means that a data gate held the run back, and a retry would not help, because")
    out.say(f"   the file has to change. Nothing was written, and raw.orders still has {after} rows.")
    summary["gate"] = {"exit": status, "failed": failed, "faults": [fault.kind for fault in faults],
                       "orders_before": before, "orders_after": after}

    out.section("The restaurant sends the week again, without the faults")
    generate.write_week(landing, dataset)
    result, lines = ingest(config, "orders"), ingest(config, "order_lines")
    out.say(f"   raw.orders: appended {result.rows_written} rows, now {result.write_result.rows_after}, and "
            f"raw.order_lines: appended {lines.rows_written} rows, now {lines.write_result.rows_after}.")
    summary["week"] = {"orders": result.rows_written, "order_lines": lines.rows_written,
                       "orders_after": result.write_result.rows_after}

    out.section("Model it with dbt: ldp dbt build")
    out.say("   One build runs the staging views, fct_orders and the gold models, and passes their data tests. Each")
    out.say("   gold model carries its contract, which says who owns it, what a row is and what each column holds,")
    out.say("   and its gate, the checks that run before the table is written.")
    out.say(f"   $ ldp dbt build {shown(workdir / 'dbt')}")
    build = dbt_build(workdir)
    say_build(out, build)
    summary["build"] = build

    out.section("Check the gold tables and cut a release: ldp gold build")
    out.say("   dbt built the gold tables, so ldp gold build builds nothing. It runs the checks of each table on the")
    out.say("   snapshot it has now, and when every table passes it cuts a release: the version of the gold layer that")
    out.say("   readers see, which does not change until the next release.")
    status, _ = ldp(out, "gold", "build", config)
    current = releases.current_release(config)
    summary["gold_build"] = {"exit": status, "release": current.name if current else None,
                             "tables": [item.table for item in current.tables] if current else [],
                             "snapshots": current.snapshots() if current else {}}

    out.section("Ask for a number: ldp metrics query")
    out.say("   Revenue, orders and the average ticket are defined once, in config/metrics.json, and every reader of")
    out.say("   the gold tables gets the same numbers. The query reads the release, and not a table that is changing.")
    ldp(out, "metrics", "query", config, *METRIC_ARGS)
    summary["metric"] = metric_layer.query(config, "revenue,orders,avg_ticket", by="cuisine", grain="month",
                                           start="2026-07-01").to_pylist()

    out.section("Hand the tables to people who do not use Python")
    out.say("   An Excel file of the scorecard, with the contract of the table on a second sheet, one DuckDB file with")
    out.say("   every gold table, and a catalog page that opens with no network.")
    ldp(out, "gold", "export", config, "gold_restaurant_scorecard", outputs / "scorecard.xlsx")
    ldp(out, "gold", "duckdb", config, "--out", outputs / "gold.duckdb")
    ldp(out, "gold", "docs", config, "--out", outputs / "catalog.html")
    summary["files"] = {name: str(outputs / name) for name in ("scorecard.xlsx", "gold.duckdb", "catalog.html")}

    summary["tables"] = table_rows(config, WAREHOUSE_TABLES)
    return summary


# --------------------------------------------------------------------------- the monitoring layer

UTC = dt.timezone.utc

FRESHNESS = {"warn_after": {"count": 26, "period": "hour"}, "error_after": {"count": 48, "period": "hour"},
             "column": "order_ts"}
"""The freshness rule of the orders: late after 26 hours and too late after 48, by the newest ``order_ts``. The
restaurant system exports every night, so a day without a file is 24 hours more."""

ROWS_FLOOR = 5
"""The fewest rows that a load of the orders may have. A normal night has 8 and a short one has 3."""

REVENUE_FLOOR = 1000
"""The least that a day of revenue may be. A normal day is 6330, the short day is 1950, and the day with a revenue drop
is 633."""

REVENUE_METRICS = ("revenue", "orders", "avg_ticket")
"""The metrics of ``config/metrics.json`` that ``gold.gold_daily_sales`` answers. The folder holds the metrics file to
the gold tables that exist, and the monitoring layer builds that one only."""

ALERT_EVENTS = ["monitor.error", "monitor.warn", "monitor.resolved"]
"""What the channels of the walkthrough are told: an error, a warning and a recovery. A warning is not sent by
default."""

ALERTS_PATH = "../.ldp/alerts.jsonl"
"""The file channel, relative to the config folder, which puts it in ``<workdir>/.ldp``."""

EVENTS_PATH = "../.ldp/events.jsonl"
"""The ``jsonl`` sink, in the same folder."""

SLACK_ENV = "LDP_SLACK_WEBHOOK"
"""The environment variable that holds the Slack webhook, when there is one. A config never holds the URL."""

DBT_SELECT = ("+gold_daily_sales", "stg_customers")
"""What the walkthrough builds with dbt: the one gold table that the revenue reads, with the models it needs and the
customers that its data tests check the orders against."""

STALE_HOURS = 50
"""How long after its newest order the table is looked at first, which is more than the 48 hours of its error."""

LOOK_AT = dt.time(6, 0)
"""The time of day, in UTC, of the look that follows a night's drop. It measures the day before."""

LOOK_AGAIN = dt.timedelta(minutes=5)
"""How long after the last look the second look is, which has nothing new to say."""

MONITORS = ("freshness", "volume.rows", "schema", "metric.revenue.value")
"""The results that the orders have, in the order that ``ldp monitor`` prints them."""


def stamp(moment: dt.datetime) -> str:
    """A moment as ``--now`` takes it: ISO 8601 in UTC to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def monitoring_config(workdir: Path, *, revenue: bool) -> dict[str, Any]:
    """Give the orders config of the work folder its SLA, its sinks and its channels, and return what was added.

    The committed config stays as it is, so the other layers behave as before. The ``iceberg`` sink keeps the history
    that the monitors read, the ``jsonl`` sink is the one the config had, and the file channel gets errors, warnings
    and recoveries. With ``LDP_SLACK_WEBHOOK`` set, a Slack channel gets the same.

    Args:
        workdir: The work folder, whose ``config/orders.json`` is changed.
        revenue: Whether the SLA also watches the revenue, which is read from a gold table that needs dbt.
    """
    path = workdir / "config" / "orders.json"
    spec = json.loads(path.read_text())
    sla: dict[str, Any] = {"freshness": FRESHNESS, "volume": {"min_rows": ROWS_FLOOR}, "schema": {"on_change": "warn"}}
    if revenue:
        sla["metrics"] = [{"metric": "revenue", "grain": "day", "min": REVENUE_FLOOR}]
    channels: list[dict[str, Any]] = [{"type": "file", "path": ALERTS_PATH, "on": ALERT_EVENTS}]
    if os.environ.get(SLACK_ENV):
        channels.append({"type": "slack", "url_env": SLACK_ENV, "on": ALERT_EVENTS})
    added = {"sla": sla, "observability": {"sinks": ["iceberg", {"type": "jsonl", "path": EVENTS_PATH}],
                                           "alerts": channels}}
    spec["metadata"].update(added)
    path.write_text(json.dumps(spec, indent=2) + "\n")
    return added


def trim_metrics(config: Path) -> None:
    """Keep the metrics file of the config folder to what ``gold.gold_daily_sales`` answers.

    ``ldp gold build`` holds the metrics of a folder to the gold tables that exist, and this layer builds one.
    """
    path = config / "metrics.json"
    spec = json.loads(path.read_text())
    spec["semantic_models"] = [model for model in spec["semantic_models"] if model["name"] == "daily_sales"]
    spec["metrics"] = [metric for metric in spec["metrics"] if metric["name"] in REVENUE_METRICS]
    path.write_text(json.dumps(spec, indent=2) + "\n")


def block_lines(added: dict[str, Any]) -> list[str]:
    """The block that the orders config gained, one line for each of its sections, as it would sit in the file."""
    lines = []
    for key, value in added.items():
        lines.append(f'"{key}": {{')
        parts = list(value.items())
        lines += [f'  "{name}": {json.dumps(part)}{"," if number < len(parts) else ""}'
                  for number, (name, part) in enumerate(parts, start=1)]
        lines.append("}" + ("," if key != list(added)[-1] else ""))
    return lines


def newest_order(dataset: Any, story: Any) -> dt.datetime:
    """The newest ``order_ts`` of a day of the monitoring story, which is what its freshness is measured from."""
    rows, _ = dataset.monitoring_orders(story.day, story.orders, story.amount_scale)
    return max(row["order_ts"] for row in rows).replace(tzinfo=UTC)


def alert_lines(path: Path) -> list[dict[str, Any]]:
    """The alerts that the file channel has written, oldest first. None when the file is not there yet."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def refresh_gold(workdir: Path, out: Narrator) -> None:
    """Load the lines of the night, build the gold table that the revenue reads, and cut a release of it."""
    config = workdir / "config"
    ingest(config, "order_lines")
    build = dbt_build(workdir, DBT_SELECT)
    out.say(f"   $ ldp dbt build {shown(workdir / 'dbt')} --select {' '.join(DBT_SELECT)}")
    out.say(f"     {build['models']} models and {build['tests']} data tests: {build['passed']} of "
            f"{build['models'] + build['tests']} passed, in {build['seconds']}s")
    ldp(out, "gold", "build", config)


def run_monitoring(workdir: Path, dataset: Any, out: Narrator) -> dict[str, Any]:
    """Watch the orders through five nights, and say what each monitor and each alert does.

    Returns:
        The ``revenue`` step that ran or not, whether ``slack`` got the alerts too, a record of each of the ``looks``
        (what it came after, its ``--now``, the exit status of ``ldp monitor``, what each monitor said, and the alerts
        that it sent), every alert that the file channel got, the ``tables`` and the ``files`` that were written.
    """
    config, landing = workdir / "config", workdir / "landing"
    orders, alerts = config / "orders.json", workdir / ".ldp" / "alerts.jsonl"
    normal, short, column, drop, fresh = generate.MONITORING_DAYS
    revenue = dbt_installed()
    summary: dict[str, Any] = {"revenue": revenue, "slack": bool(os.environ.get(SLACK_ENV)), "looks": []}

    def arrive(story: Any) -> None:
        generate.write_monitoring_day(landing, dataset, story)
        ldp(out, "run", orders)
        if revenue:
            refresh_gold(workdir, out)

    def look(now: dt.datetime, after: str) -> dict[str, Any]:
        before = len(alert_lines(alerts))
        status, _ = ldp(out, "monitor", config, "--now", stamp(now), ok=(0, 2))
        said = {result.monitor: result.status for result in read_results(Config.from_json(orders))
                if result.evaluated_at == now}
        sent = [{"event": alert["event"], "monitor": alert["monitor"]} for alert in alert_lines(alerts)[before:]]
        record = {"after": after, "now": stamp(now), "exit": status, "statuses": said, "alerts": sent}
        summary["looks"].append(record)
        for alert in sent:
            out.say(f"   alert: {alert['event']} {alert['monitor']}")
        return record

    def morning(story: Any) -> dt.datetime:
        return dt.datetime.combine(story.day + dt.timedelta(days=1), LOOK_AT, tzinfo=UTC)

    out.section("What a good orders table looks like: an SLA, a history and a channel for alerts")
    added = monitoring_config(workdir, revenue=revenue)
    out.say("   The orders config of the work folder gets four things: the SLA, the iceberg sink that keeps the")
    out.say("   history the monitors read, the jsonl sink it had, and a file that the alerts go to. It gains this:")
    for line in block_lines(added):
        out.say(f"   {line}")
    out.say("   A table is late after 26 hours and too late after 48, a load is short under 5 rows, a schema that")
    out.say("   changes is a warning, and a day of revenue is too low under 1000. An alert says it once, when a")
    out.say("   monitor changes state, and again when it recovers.")
    if revenue:
        trim_metrics(config)
        out.say("   The revenue is the metric of config/metrics.json, read from the gold table gold_daily_sales. The")
        out.say("   models that build it read four small tables, so they are loaded once:")
        rows = load_dimensions(workdir, dataset)
        out.table(rows)
        summary["dimensions"] = {row["table"]: row["rows"] for row in rows}
    else:
        out.say("   The SLA has no entry for revenue, because the revenue is read from a gold table, and that")
        out.say("   needs dbt.")

    out.section("The night of 3 August arrives, and then nothing does")
    out.say("   The restaurant system exports the orders of a day every night, 8 orders on a normal day. The file of")
    out.say("   3 August lands, and the nights of 4 and 5 August bring nothing.")
    arrive(normal)
    stale = newest_order(dataset, normal) + dt.timedelta(hours=STALE_HOURS)
    out.say(f"   The monitor looks at {stamp(stale)}, {STALE_HOURS} hours after the newest order. --now gives it that")
    out.say("   time, and without it the monitor uses the clock.")
    look(stale, normal.story)
    out.say("   Exit status 2 says that a monitor is in error. It is a finding and not a failure of ldp, so a")
    out.say("   scheduler does not retry it. The alert is in .ldp/alerts.jsonl, one line of JSON:")
    out.table([{"event": alert["event"], "monitor": alert["monitor"], "status": alert["status"],
                "previous_status": alert.get("previous_status"), "at": alert["at"]} for alert in alert_lines(alerts)])

    out.section("A short batch: 3 orders where a normal night has 8")
    out.say("   The night of 6 August arrives, and it is cut short. The look of the next morning finds the table fresh")
    out.say("   again, so the freshness alert is resolved, and the load is under the 5 rows that min_rows asks for.")
    arrive(short)
    look(morning(short), short.story)

    out.section("A new column: the export gains discount")
    out.say("   From 7 August the file has a discount column. The load is normal in size, so the volume alert is")
    out.say("   resolved. The schema monitor says that the table and the source gained a column. That is a warning")
    out.say("   here, and a change is said once, by the look that first sees it.")
    arrive(column)
    look(morning(column), column.story)

    out.section("A revenue drop: a normal night, at a tenth of the amounts")
    if revenue:
        out.say("   8 August has all its 8 orders, with every amount a tenth of what it should be, as if a price list")
        out.say("   had been loaded with a decimal point in the wrong place. The load is the right size, so only the")
        out.say("   revenue shows it. The metric monitor reads the revenue of the day before from the gold table, and")
        out.say("   it is under 1000.")
    else:
        out.say(f"   Skipping the revenue step: dbt is not installed ({DBT_HINT}).")
        out.say("   8 August loads as a normal night, and the schema monitor, which saw the new column the day before,")
        out.say("   has nothing more to say.")
    arrive(drop)
    look(morning(drop), drop.story)

    out.section("A fresh night, and a second look that has nothing to say")
    if revenue:
        out.say("   9 August is normal again. The revenue is back inside its limit, so its alert is resolved.")
    else:
        out.say("   9 August is normal again, and nothing changes state.")
    arrive(fresh)
    look(morning(fresh), fresh.story)
    out.say("   The monitor looks again five minutes later. Nothing changed state, so it sends nothing.")
    look(morning(fresh) + LOOK_AGAIN, "a second look")
    if summary["slack"]:
        out.say(f"   {SLACK_ENV} is set, so the same alerts were posted to that Slack webhook.")

    out.section("One screen for every pipeline: ldp status")
    at = morning(fresh) + 2 * LOOK_AGAIN
    out.say("   ldp status shows every pipeline of the folder on one screen, the worst first. It records nothing and")
    out.say("   sends no alert. The configs with no SLA say so.")
    ldp(out, "status", config, "--now", stamp(at))
    page = workdir / "status.html"
    ldp(out, "status", config, "--now", stamp(at), "--html", page)
    out.say("   last_run is empty because the runs of this walkthrough were written when you ran it, and --now says")
    out.say("   that it is 10 August, so they are still to come. With the clock and the data of a real night they are")
    out.say("   there.")

    table = iceberg_from_config(Config.from_json(orders), "target")
    summary["alerts"] = alert_lines(alerts)
    summary["files"] = {"alerts": str(alerts), "status": str(page)}
    summary["tables"] = {"raw.orders": {"rows": table.row_count(), "snapshots": len(table.snapshots())}}
    return summary


# --------------------------------------------------------------------------- the walkthrough


def run_restaurant(workdir: str | Path = DEFAULT_WORKDIR, *, layer: str = "warehouse", seed: int = generate.SEED,
                   quiet: bool = False) -> dict[str, Any]:
    """Run a layer of the example, and the layers before it, and return a summary of what happened.

    Args:
        workdir: The work folder (created, or cleared if this example made it).
        layer: The layer to run; one of :data:`LAYERS`.
        seed: The generator's random seed.
        quiet: Print nothing.

    Returns:
        A JSON-friendly summary of the run: row counts, builds, and the churn tables.

    Raises:
        ValueError: If ``layer`` is not a layer, or the work folder is not this example's or cannot be cleared.
        EngineNotFound: If the dbt or the excel extra is missing.
        BuildFailed: If ``ldp dbt build`` fails.
        StepFailed: If another ``ldp`` command of the walkthrough fails.
    """
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; the layers are {list(LAYERS)}")
    layers = [*LAYER_NEEDS[layer], layer]
    if "warehouse" in layers or "gold" in layers:
        require_extras()
    started = time.perf_counter()
    out = Narrator(quiet)
    workdir = prepare_workdir(workdir, keep_metrics=layer != "orchestration")
    dataset = generate.generate(seed)
    out.say({"orchestration": "Restaurant chain -> a workflow that runs every night",
             "gold": "Restaurant chain -> a data mart that people who are not engineers can use",
             "monitoring": "Restaurant chain -> monitors that raise alerts, and one screen of health",
             "warehouse": "Restaurant chain -> Iceberg warehouse with dbt"}[layer] + f", in {shown(workdir)}")
    out.say(f"{len(dataset.restaurants)} restaurants, {len(dataset.customers)} customers, "
            f"{len(dataset.orders)} orders in {len(dataset.batches())} batches, January to August 2026 (seed {seed}). "
            f"This run: layer {layer}.")
    summary: dict[str, Any] = {"workdir": str(workdir), "layer": layer, "seed": seed}
    for name in layers:
        summary[name] = {"warehouse": run_warehouse, "orchestration": run_orchestration, "gold": run_gold,
                         "monitoring": run_monitoring}[name](workdir, dataset, out)
    summary["duration_s"] = round(time.perf_counter() - started, 1)

    out.section("Summary")
    config = workdir / "config"
    if layer == "orchestration":
        out.table(summary["orchestration"]["days"])
        out.say(f"   finished in {summary['duration_s']}s. Try these next:")
        commands = (f"ldp schedule list {shown(workdir)} --now {LIST_AT}",
                    f"ldp backfill {shown(workdir / 'workflows' / 'daily_orders.json')} --from {generate.DROP_DAYS[0]} "
                    f"--to {generate.DROP_DAYS[4]} --dry-run",
                    f"ldp schedule install --print launchd {shown(workdir)}")
    elif layer == "monitoring":
        out.table([{"look": number, "at": look["now"], "after": look["after"], "exit": look["exit"],
                    **{monitor.split(".")[0]: look["statuses"].get(monitor, "-") for monitor in MONITORS},
                    "alerts": len(look["alerts"])} for number, look in enumerate(summary["monitoring"]["looks"], 1)])
        out.say(f"   finished in {summary['duration_s']}s. Try these next:")
        commands = (f"ldp status {shown(config)} --now {summary['monitoring']['looks'][-1]['now']}",
                    f"ldp monitor {shown(config)} --now {summary['monitoring']['looks'][-1]['now']} --json",
                    f"ldp alert test {shown(config / 'orders.json')}",
                    f"ldp maintain {shown(config / 'orders.json')} --system",
                    f"ldp mcp --config {shown(config)}")
    elif layer == "gold":
        out.table([{"table": name, **info} for name, info in summary["gold"]["tables"].items()])
        out.say(f"   finished in {summary['duration_s']}s. Try these next:")
        commands = (f"ldp gold list {shown(config)}",
                    f"ldp gold show {shown(config)} gold_daily_sales",
                    f"ldp metrics query {shown(config)} revenue,avg_ticket --by cuisine --grain month",
                    f"ldp gold releases {shown(config)}",
                    f"ldp mcp --config {shown(config)} --layer gold")
    else:
        out.table([{"table": name, **info} for name, info in summary["warehouse"]["tables"].items()])
        out.say(f"   finished in {summary['duration_s']}s. Try these next:")
        commands = (f"ldp snapshots {shown(config / 'churn_by_month.json')}",
                    f"ldp metrics query {shown(config)} churn_rate,active_customers --grain month",
                    f"ldp dbt docs {shown(workdir / 'dbt')} --serve",
                    f"ldp mcp --config {shown(config)}")
    for command in commands:
        out.say(f"     {command}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A restaurant chain on a local Iceberg lakehouse: Excel and CSV exports, dbt models, tests, "
                    "a nightly workflow and a gold data mart.")
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR),
                        help="work folder; cleared on each run if this example created it (default: %(default)s)")
    parser.add_argument("--layer", choices=LAYERS, default="warehouse",
                        help="the layer to run, and the layers it needs first (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=generate.SEED, help=f"random seed (default {generate.SEED})")
    parser.add_argument("--quiet", action="store_true", help="print only the final line")
    args = parser.parse_args(argv)
    try:
        summary = run_restaurant(args.workdir, layer=args.layer, seed=args.seed, quiet=args.quiet)
    except (ValueError, EngineNotFound, BuildFailed, StepFailed) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.quiet:
        print(f"restaurant: {summary[args.layer]['tables']} in {summary['duration_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
