"""A small restaurant chain on a local Iceberg lakehouse, from Excel and CSV exports to tested tables, with dbt.

The example follows one dataset through the layers of a company's data platform, and every milestone of the roadmap
adds a layer to it. The ``warehouse`` layer takes the restaurant system's exports, loads them
into Iceberg, models them with dbt, tests them, and publishes the models back to Iceberg as versioned tables,
offline, in under a minute:

1. **Excel to CSV.** The restaurant list is an Excel workbook with a title row above its header. ``ExcelToCSV`` turns
   its sheet into a CSV file, as you would to share it from a Google Drive folder.
2. **Ingest.** The restaurants, the customers and the first batch of orders (January to June 2026) land in the raw
   namespace of the lakehouse. The orders are partitioned by ``day(order_ts)`` and ``restaurant_id``.
3. **Model with dbt.** ``ldp dbt build`` registers the raw tables as sources, runs the staging views, the incremental
   ``fct_orders``, and the gold models ``churn_by_month`` and ``revenue_by_restaurant_day``, runs their data tests,
   and publishes the marked models to Iceberg.
4. **Churn.** A customer churns in the month they stop ordering. The first batch gives the churn for February to June.
5. **Batch 2.** July arrives, and with it five late orders from the end of June. The build upserts only the new and
   late rows of ``fct_orders``, and rebuilds the gold tables.
6. **Time travel.** The late orders change history: June's churn falls. ``ldp query --snapshot`` reads June's churn as
   it was published after batch 1.

The ``orchestration`` layer runs the restaurant's nightly drops as a workflow, the way a scheduler would:

1. **Three drops, one bad row.** The restaurant system exports the orders and the order lines of a day into
   ``drops/``. The file of 3 August has a channel that the checks do not accept.
2. **A workflow.** ``workflows/daily_orders.json`` has the tasks ``orders``, ``order_lines``, ``models`` (the dbt
   project of the warehouse layer) and ``tidy``, and ``ldp workflow plan`` shows them with the key of the window.
3. **A backfill that stops at the gate.** ``ldp backfill`` publishes 1 and 2 August, and a data gate holds 3 August
   back, so its models and its tidy task do not run.
4. **Fix and resume.** After the row is fixed, the same command runs only 3 August, and a third run skips every day.
5. **The next night.** ``ldp schedule tick`` loads 4 August when its time comes, ``ldp runs`` and ``ldp commits`` show
   what happened, and ``ldp schedule install`` prints the recipe that calls the tick.

Run it from anywhere::

    python examples/restaurant/run.py                              # workdir: examples/restaurant/data
    python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer warehouse
    python examples/restaurant/run.py --workdir ldp_demo/restaurant --layer orchestration

The ``warehouse`` layer needs the ``dbt`` and ``excel`` extras: ``pip install "local-data-platform[dbt,excel]"``.
Without them it says so and exits with 1. The ``orchestration`` layer needs neither. It loads the two small tables that
it needs itself, and it runs the ``models`` task when the ``dbt`` extra is installed and prints a skip line when it is
not. ``--layer`` runs one layer and the layers that it needs first, which are none so far.
"""

import argparse
import contextlib
import csv
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
from local_data_platform.cli import format_table
from local_data_platform.cli import main as ldp_main
from local_data_platform.exceptions import EngineNotFound
from local_data_platform.format.iceberg.commit import commit_log
from local_data_platform.lake import open_lake
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.pipeline.registry import create_pipeline

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE / "config"
DBT_DIR = HERE / "dbt"
WORKFLOWS_DIR = HERE / "workflows"
DEFAULT_WORKDIR = HERE / "data"
MARKER = ".restaurant_workdir"
WORKDIR_FOLDERS = ("landing", "warehouse", "config", "dbt", "workflows", "drops", ".ldp")
EXTRAS_HINT = 'pip install "local-data-platform[dbt,excel]"'
DBT_HINT = 'pip install "local-data-platform[dbt]"'

LAYERS = ("warehouse", "orchestration")
"""The layers of the example, in order. Each milestone adds one."""

LAYER_NEEDS = {"warehouse": (), "orchestration": ()}
"""The layers that a layer runs first. ``orchestration`` needs none, because it loads the two small tables it needs
itself, so it runs without the dbt and excel extras that ``warehouse`` needs."""

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


def prepare_workdir(workdir: str | Path) -> Path:
    """Create (or clear) the work folder and copy the configs, the dbt project and the workflow files into it.

    The configs' relative paths (``../landing/…``, ``../warehouse``) resolve against the folder they are in, so the
    copies in ``<workdir>/config`` also work with the CLI. The dbt project writes its published configs next to them.
    The files of ``<workdir>/workflows`` read the drops in ``<workdir>/drops``, and write their ledger next to them.

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
    shutil.copytree(DBT_DIR, workdir / "dbt", ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"))
    shutil.copytree(WORKFLOWS_DIR, workdir / "workflows", ignore=shutil.ignore_patterns(".ldp"))
    return workdir


class Narrator:
    """Prints the walkthrough, and nothing when ``quiet``."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.step = 0

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


def dbt_build(workdir: Path) -> dict[str, Any]:
    """Run ``ldp dbt build`` on the example's project, and return what it did.

    dbt's own output is long, so it is captured, and a summary is returned: the models and the data tests that ran, how
    many passed, how long it took, and the models that were published, each with its write mode, its rows before and
    after, and its snapshot.

    Raises:
        BuildFailed: If the build failed. Its message is what ``ldp`` printed.
    """
    project = workdir / "dbt"
    started = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "local_data_platform.cli", "dbt", "build", str(project)],
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


def run_warehouse(workdir: Path, dataset: Any, out: Narrator) -> dict[str, Any]:
    """Load batch 1, build, load batch 2 and rebuild, and tell the story of June's churn."""
    config = workdir / "config"
    landing = workdir / "landing"
    summary: dict[str, Any] = {}

    def run(name: str):
        return create_pipeline(Config.from_json(config / f"{name}.json")).run()

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
    rows = []
    loads = (("restaurants", "ExcelToIceberg", "overwrite"), ("customers", "CSVToIceberg", "overwrite"),
             ("orders", "CSVToIceberg", "append"))
    for name, route, mode in loads:
        result = run(name)
        rows.append({"table": f"raw.{name}", "rows": result.write_result.rows_after, "route": route, "mode": mode,
                     "checks": f"{len(result.quality)} passed" if result.quality.passed else "FAILED"})
    out.table(rows)
    orders = iceberg_from_config(Config.from_json(config / "orders.json"), "target")
    partitions = orders.table().inspect.partitions().num_rows
    out.say(f"   Batch 1 is the {len(batch1)} orders from January to June, in {partitions} partitions by "
            f"day(order_ts) and restaurant_id.")
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
    out.say(f"   raw.orders: appended {result.rows_written} rows, now {result.write_result.rows_after}. The "
            f"{len(late)} late orders were placed in June and reached the lake in July,")
    out.say("   so their order_ts is old. fct_orders takes every order it has no row for, so it sees them.")
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

    tables = {}
    for name, path in (("raw.restaurants", "restaurants"), ("raw.customers", "customers"), ("raw.orders", "orders"),
                       ("silver.fct_orders", "fct_orders"), ("gold.churn_by_month", "churn_by_month"),
                       ("gold.revenue_by_restaurant_day", "revenue_by_restaurant_day")):
        table = iceberg_from_config(Config.from_json(config / f"{path}.json"), "target")
        tables[name] = {"rows": table.row_count(), "snapshots": len(table.snapshots())}
    summary["tables"] = tables
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
GOLD_PER_DAY_SQL = ("SELECT day, count(*) AS rows FROM gold.revenue_by_restaurant_day GROUP BY day ORDER BY day")

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
    """A workflow without its dbt tasks, where what waited for a dbt task waits for what that task waited for."""
    models = {task["id"]: task for task in spec["tasks"] if "dbt" in task}
    tasks = []
    for task in spec["tasks"]:
        if task["id"] in models:
            continue
        waits = [name for waited in task.get("depends_on", [])
                 for name in (models[waited].get("depends_on", []) if waited in models else [waited])]
        tasks.append({**task, "depends_on": list(dict.fromkeys(waits))} if waits else task)
    return {**spec, "tasks": tasks}


def load_dimensions(workdir: Path, dataset: Any) -> list[dict[str, Any]]:
    """Load ``raw.restaurants`` and ``raw.customers`` from CSV files, which the models of the dbt project read.

    They go to the tables, with the checks, of the configs of the warehouse layer. The restaurants come from a CSV
    file here and from an Excel workbook there, so this needs no Excel reader.
    """
    landing, config = workdir / "landing", workdir / "config"
    generate.write_dimensions(landing, dataset)
    rows = []
    for name in ("restaurants", "customers"):
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
             **({"gold.revenue_by_restaurant_day": gold.get(day, 0)} if models else {})}
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
            out.say(f"   gold.revenue_by_restaurant_day {when}: {gold}")

    out.section("Three nightly drops, and a bad row in the third")
    for day in days[:3]:
        generate.write_drop(drops, dataset, day)
    bad_order, channel = change_channel(drops / f"orders_{days[2]}.csv", BAD_CHANNEL)
    out.say("   The restaurant system exports the orders and the order lines of a day into drops/ every night.")
    out.say(f"   These are 1 to 3 August. In the file of 3 August, order {bad_order} has the channel '{BAD_CHANNEL}',")
    out.say("   which the checks of the orders config do not accept.")
    summary["bad_order"] = bad_order

    if models:
        out.section("Load the two small tables that the models read, once")
        out.say("   The dbt project reads raw.restaurants and raw.customers as well as raw.orders. They change rarely,")
        out.say("   so they are loaded once, and the workflow only loads the days.")
        rows = load_dimensions(workdir, dataset)
        out.table(rows)
        summary["dimensions"] = {row["table"]: row["rows"] for row in rows}

    out.section("One workflow for the day: ldp workflow plan")
    out.say("   workflows/daily_orders.json says what runs, in which order, and when: 02:05 UTC, for the day before.")
    if models:
        out.say("   orders and order_lines wait for nothing, models (a dbt task) waits for both, and tidy waits for")
        out.say("   models.")
    else:
        spec = json.loads(workflow.read_text())
        workflow.write_text(json.dumps(without_models(spec), indent=2) + "\n")
        out.say(f"   Skipping the models task: dbt is not installed ({DBT_HINT}). Without it, tidy waits for")
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
                           ("gold.revenue_by_restaurant_day", "revenue_by_restaurant_day")) if models)):
        table = iceberg_from_config(Config.from_json(path), "target")
        tables[name] = {"rows": table.row_count(), "snapshots": len(table.snapshots())}
    summary["tables"] = tables
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
    if "warehouse" in layers:
        require_extras()
    started = time.perf_counter()
    out = Narrator(quiet)
    workdir = prepare_workdir(workdir)
    dataset = generate.generate(seed)
    out.say(("Restaurant chain -> a workflow that runs every night" if layer == "orchestration"
             else "Restaurant chain -> Iceberg warehouse with dbt") + f", in {shown(workdir)}")
    out.say(f"{len(dataset.restaurants)} restaurants, {len(dataset.customers)} customers, "
            f"{len(dataset.orders)} orders in {len(dataset.batches())} batches, January to August 2026 (seed {seed}). "
            f"This run: layer {layer}.")
    summary: dict[str, Any] = {"workdir": str(workdir), "layer": layer, "seed": seed}
    for name in layers:
        summary[name] = {"warehouse": run_warehouse, "orchestration": run_orchestration}[name](workdir, dataset, out)
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
    else:
        out.table([{"table": name, **info} for name, info in summary["warehouse"]["tables"].items()])
        out.say(f"   finished in {summary['duration_s']}s. Try these next:")
        commands = (f"ldp snapshots {shown(config / 'churn_by_month.json')}",
                    f"ldp dbt docs {shown(workdir / 'dbt')} --serve",
                    f"ldp mcp --config {shown(config)}")
    for command in commands:
        out.say(f"     {command}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A restaurant chain on a local Iceberg lakehouse: Excel and CSV exports, dbt models, tests, churn.")
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
