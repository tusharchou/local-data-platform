"""The restaurant example's warehouse layer, end to end, with the real dbt, offline.

``run.run_restaurant`` runs the whole walkthrough once for the module, and the tests read what it returned, so every
number below is the one the example prints for the fixed seed. Two more small runs cover a batch with a duplicate
``order_id`` and the churn example of issue #47. Every test is marked ``dbt``, so it skips without the extra, and the
``dbt`` CI job runs it.
"""

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_data_platform import Config, metrics
from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError
from local_data_platform.mcp_server.tools import LakeTools
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.pipeline.registry import create_pipeline
from tests.support import load_example_module, without_log_warnings

pytestmark = pytest.mark.dbt

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "restaurant"
run = load_example_module("restaurant_run_dbt", EXAMPLE / "run.py")
generate = run.generate


def table_of(workdir: Path, name: str):
    return iceberg_from_config(Config.from_json(workdir / "config" / f"{name}.json"), "target")


def ingest(workdir: Path, name: str):
    return create_pipeline(Config.from_json(workdir / "config" / f"{name}.json")).run()


def churn(month: str, start: int, churned: int, rate: float | None) -> dict:
    return {"month": dt.date.fromisoformat(month), "customers_at_start": start, "churned": churned,
            "churn_rate": rate}


# The churn the first batch gives, and what it is once the second batch has arrived.
AFTER_BATCH_ONE = [churn("2026-02-01", 68, 16, 0.235), churn("2026-03-01", 68, 24, 0.353),
                   churn("2026-04-01", 71, 24, 0.338), churn("2026-05-01", 75, 23, 0.307),
                   churn("2026-06-01", 74, 21, 0.284)]
AFTER_BATCH_TWO = [*AFTER_BATCH_ONE[:4], churn("2026-06-01", 74, 16, 0.216), churn("2026-07-01", 80, 32, 0.4)]


@pytest.fixture(scope="module")
def walkthrough(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("restaurant") / "work"
    narrative = io.StringIO()
    with contextlib.redirect_stdout(narrative):
        summary = run.run_restaurant(workdir)
    return workdir, summary, narrative.getvalue()


# --------------------------------------------------------------------------- the walkthrough


def test_the_warehouse_layer_runs_offline_in_under_two_minutes(walkthrough):
    _, summary, _ = walkthrough

    assert summary["layer"] == "warehouse" and summary["duration_s"] < 120


def test_the_first_build_runs_twelve_models_and_forty_one_data_tests_and_every_one_passes(walkthrough):
    build = walkthrough[1]["warehouse"]["first_build"]

    assert (build["models"], build["tests"], build["passed"]) == (12, 41, 53)
    assert [(item["table"], item["mode"], item["rows_before"], item["rows_after"]) for item in build["published"]] == [
        ("silver.fct_orders", "upsert", 0, 745), ("gold.churn_by_month", "overwrite", 0, 5),
        ("gold.gold_daily_sales", "overwrite", 0, 534), ("gold.gold_menu_item_weekly", "overwrite", 0, 795),
        ("gold.gold_restaurant_scorecard", "overwrite", 0, 6)]


def test_the_raw_tables_hold_the_exports_and_the_orders_are_partitioned_by_day_and_restaurant(walkthrough):
    warehouse = walkthrough[1]["warehouse"]

    assert warehouse["excel_rows"] == 6
    assert warehouse["raw_rows"] == {"raw.restaurants": 6, "raw.customers": 150, "raw.menu_items": 36,
                                     "raw.reviews": 94, "raw.orders": 745, "raw.order_lines": 1829}
    assert warehouse["partitions"] == 534


def test_churn_for_february_to_june_is_the_share_of_last_months_customers_who_did_not_order(walkthrough):
    rows = walkthrough[1]["warehouse"]["churn_after_batch_1"]

    assert rows == AFTER_BATCH_ONE
    assert all(row["churned"] / row["customers_at_start"] == pytest.approx(row["churn_rate"], abs=0.0005)
               for row in rows)


def test_batch_two_upserts_only_the_july_orders_and_the_late_june_orders(walkthrough):
    warehouse = walkthrough[1]["warehouse"]
    second = warehouse["second_build"]

    assert warehouse["orders_upserted"] == 108 == 103 + 5
    assert [(item["table"], item["rows_before"], item["rows_after"]) for item in second["published"]] == [
        ("silver.fct_orders", 745, 853), ("gold.churn_by_month", 5, 6), ("gold.gold_daily_sales", 534, 621),
        ("gold.gold_menu_item_weekly", 795, 928), ("gold.gold_restaurant_scorecard", 6, 6)]
    assert (second["models"], second["tests"], second["passed"]) == (12, 41, 53)


def test_the_late_june_orders_lower_junes_churn_and_time_travel_shows_it(walkthrough):
    warehouse = walkthrough[1]["warehouse"]

    assert warehouse["churn_after_batch_2"] == AFTER_BATCH_TWO
    assert warehouse["june_before"] == AFTER_BATCH_ONE[-1]  # read with --snapshot: 21 of 74 churned
    assert warehouse["june_after"] == churn("2026-06-01", 74, 16, 0.216)  # now: the 5 late customers did order
    assert warehouse["june_before"]["churned"] - warehouse["june_after"]["churned"] == 5
    first = walkthrough[1]["warehouse"]["first_build"]["published"][1]
    assert warehouse["churn_snapshot_after_batch_1"] == first["snapshot_id"]


def test_the_churn_metrics_match_the_model_for_every_month_the_example_has(walkthrough):
    config = walkthrough[0] / "config"
    in_the_model = run.lake_rows(config / "churn_by_month.json", "SELECT month, churn_rate, active_customers "
                                                                 "FROM churn_by_month ORDER BY month")

    answer = metrics.query(config, "churn_rate,active_customers", grain="month")

    assert answer.column_names == ["month", "churn_rate", "active_customers"]
    assert answer.to_pylist() == in_the_model
    assert [row["month"] for row in in_the_model] == [dt.date(2026, month, 1) for month in range(2, 8)]
    assert [row["active_customers"] for row in in_the_model] == [68, 71, 75, 74, 80, 64]
    assert answer.to_pylist()[4]["churn_rate"] == pytest.approx(16 / 74)  # June, after the late orders


def test_ldp_metrics_query_prints_what_the_readme_of_the_example_shows(walkthrough, capsys):
    config = walkthrough[0] / "config"

    assert main(["metrics", "validate", str(config)]) == 0
    assert capsys.readouterr().out == (f"metrics.json: 3 semantic models and 6 metrics fit the gold tables of "
                                       f"{config.resolve()}\n")
    assert main(["metrics", "query", str(config), "churn_rate,active_customers", "--grain", "month"]) == 0

    out = without_log_warnings(capsys.readouterr().out)
    assert out.splitlines() == [
        "month       churn_rate  active_customers", "----------  ----------  ----------------",
        "2026-02-01       23.5%                68", "2026-03-01       35.3%                71",
        "2026-04-01       33.8%                75", "2026-05-01       30.7%                74",
        "2026-06-01       21.6%                80", "2026-07-01       40.0%                64", "(6 rows)",
        "read the tables as they are now, because no release has been cut yet"]


def test_the_catalog_shows_the_contract_of_the_churn_table_and_its_two_metrics(walkthrough, capsys):
    config = walkthrough[0] / "config"

    assert main(["gold", "show", str(config), "churn_by_month"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "gold.churn_by_month  (gold)"
    assert "counted in the month a customer stops ordering" in lines[1]
    assert "  Owner      customer insights" in lines and "  Grain      one row for each month" in lines
    columns = lines[lines.index("Columns (5)") + 3:][:5]
    assert [line.split()[:2] for line in columns] == [
        ["month", "date"], ["customers_at_start", "long"], ["churned", "long"], ["churn_rate", "double"],
        ["active_customers", "long"]]
    metrics_at = lines.index("Metrics")
    assert [line.split()[0] for line in lines[metrics_at + 3:metrics_at + 5]] == ["churn_rate", "active_customers"]


def test_an_agent_asks_for_the_churn_metrics_and_gets_the_numbers_of_the_model(walkthrough, tmp_path):
    config = walkthrough[0] / "config"
    served = LakeTools.from_sources([config], audit_path=tmp_path / "audit.jsonl", iceberg_audit=False, native=False,
                                    max_rows=50, timeout_s=30)
    try:
        listed = served.call("list_metrics", {})
        asked = served.call("query_metric", {"metrics": ["churn_rate", "active_customers"], "grain": "month"})
    finally:
        served.close()

    in_the_model = run.lake_rows(config / "churn_by_month.json",
                                 "SELECT churn_rate, active_customers FROM churn_by_month ORDER BY month")
    assert listed.ok and [item["name"] for item in listed.data["metrics"]] == [
        "revenue", "orders", "avg_ticket", "avg_rating", "churn_rate", "active_customers"]
    assert asked.ok and [row[1:] for row in asked.data["rows"]] == [
        [row["churn_rate"], row["active_customers"]] for row in in_the_model]


def test_the_churn_metrics_are_only_by_month_and_say_why_when_asked_for_more(walkthrough):
    config = walkthrough[0] / "config"

    for options in ({"grain": "quarter"}, {"grain": "year"}, {}):
        with pytest.raises(ConfigError, match=r"the grains \['month'\]"):
            metrics.query(config, "churn_rate", **options)


def test_every_published_table_has_a_snapshot_for_each_build_that_changed_it(walkthrough):
    tables = walkthrough[1]["warehouse"]["tables"]

    assert tables == {
        "raw.restaurants": {"rows": 6, "snapshots": 1}, "raw.customers": {"rows": 150, "snapshots": 1},
        "raw.menu_items": {"rows": 36, "snapshots": 1}, "raw.reviews": {"rows": 94, "snapshots": 1},
        "raw.orders": {"rows": 853, "snapshots": 2}, "raw.order_lines": {"rows": 2113, "snapshots": 2},
        "silver.fct_orders": {"rows": 853, "snapshots": 2},
        "gold.churn_by_month": {"rows": 6, "snapshots": 3}, "gold.gold_daily_sales": {"rows": 621, "snapshots": 3},
        "gold.gold_menu_item_weekly": {"rows": 928, "snapshots": 3},
        "gold.gold_restaurant_scorecard": {"rows": 6, "snapshots": 3}}


def test_every_published_model_carries_its_contract_in_its_table(walkthrough):
    workdir = walkthrough[0]
    expected = {"churn_by_month": ("gold", "customer insights"), "gold_daily_sales": ("gold", "restaurant operations"),
                "gold_menu_item_weekly": ("gold", "kitchen operations"),
                "gold_restaurant_scorecard": ("gold", "restaurant operations"), "fct_orders": ("silver", None)}

    for name, (layer, owner) in expected.items():
        table = table_of(workdir, name).table()
        assert table.properties["ldp.layer"] == layer and table.properties.get("ldp.owner") == owner, name
        assert table.properties["comment"], name
        assert all(field.doc for field in table.schema().fields), f"{name} describes every column"
    daily = table_of(workdir, "gold_daily_sales").table()
    assert daily.properties["ldp.grain"] == "one row for each restaurant on each day it had an order"
    assert table_of(workdir, "fct_orders").table().properties["ldp.primary-key"] == "order_id"


def test_the_run_events_of_each_published_model_list_the_tables_it_was_made_from(walkthrough):
    workdir = walkthrough[0]
    inputs = {}
    for line in (workdir / "config" / ".ldp" / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["type"] == "run.started":
            inputs[event["payload"]["pipeline"]] = [ref["name"] for ref in event["payload"].get("inputs", [])]

    assert inputs["fct_orders"] == ["raw.orders"], "through the staging view that reads the source"
    assert inputs["churn_by_month"] == ["silver.fct_orders"], "through the monthly activity model that is not published"
    assert inputs["gold_daily_sales"] == ["raw.restaurants", "silver.fct_orders"]
    assert inputs["gold_menu_item_weekly"] == ["raw.menu_items", "raw.order_lines", "raw.restaurants",
                                               "silver.fct_orders"]
    assert inputs["gold_restaurant_scorecard"] == ["raw.restaurants", "raw.reviews", "silver.fct_orders"]
    assert inputs["orders"] == [], "an ingest pipeline does not know what its file was made from"


def test_the_narrative_tells_the_story_in_order(walkthrough):
    narrative = walkthrough[2]
    steps = ["== 1. Excel to CSV: the restaurant list", "== 2. Ingest the raw tables into Iceberg (namespace raw)",
             "== 3. Model it with dbt: ldp dbt build", "== 4. Churn by month, February to June",
             "== 5. Batch 2 arrives: 103 orders from July and 5 late ones from June",
             "== 6. Time travel: June's churn before and after the late orders", "== 7. Summary"]

    positions = [narrative.index(step) for step in steps]

    assert positions == sorted(positions)
    for text in ("ExcelToCSV: read 6 rows, wrote 6 to landing/restaurants.csv",
                 "12 models and 41 data tests: 53 of 53 passed",
                 "published silver.fct_orders: upsert, 745 -> 853 rows",
                 "fct_orders upserted 108 rows, the new and the late ones, and no other row was written.",
                 "--snapshot ", "June as it was published after batch 1:", "ldp dbt docs ", "ldp mcp --config ",
                 "ldp metrics query ", " churn_rate,active_customers --grain month"):
        assert text in narrative, text


def test_the_published_models_have_configs_beside_the_ingest_configs(walkthrough):
    workdir = walkthrough[0]

    assert sorted(path.stem for path in (workdir / "config").glob("*.json")) == [
        "churn_by_month", "customers", "fct_orders", "gold_daily_sales", "gold_menu_item_weekly",
        "gold_restaurant_scorecard", "menu_items", "metrics", "order_lines", "orders", "restaurants",
        "restaurants_to_csv", "reviews"]
    assert table_of(workdir, "fct_orders").identifier == "silver.fct_orders"
    assert table_of(workdir, "churn_by_month").identifier == "gold.churn_by_month"


def test_the_commands_the_example_suggests_work_on_the_work_folder(walkthrough, capsys):
    workdir = walkthrough[0]
    config = workdir / "config"

    assert main(["snapshots", str(config / "churn_by_month.json")]) == 0
    assert "(3 rows)" in capsys.readouterr().out  # the first build's append, then the second's delete and append
    assert main(["query", str(config), run.JOIN_SQL]) == 0
    out = capsys.readouterr().out
    assert "2026-06-01       0.216     126" in out and out.splitlines()[-1] == "(6 rows)"
    assert main(["runs", str(config / "fct_orders.json")]) == 0
    assert "published" in capsys.readouterr().out
    assert main(["runs", str(config / "orders.json")]) == 0
    assert capsys.readouterr().out.count("published") >= 2  # the two batches
    assert main(["metrics", "query", str(config), "churn_rate,active_customers", "--grain", "month"]) == 0
    assert "(6 rows)" in capsys.readouterr().out


def test_a_build_with_nothing_new_adds_no_snapshot_to_the_upserted_table(walkthrough):
    workdir = walkthrough[0]
    silver, gold = table_of(workdir, "fct_orders"), table_of(workdir, "churn_by_month")
    before = (len(silver.snapshots()), len(gold.snapshots()))

    build = run.dbt_build(workdir)

    assert [(item["table"], item["rows_before"], item["rows_after"]) for item in build["published"]] == [
        ("silver.fct_orders", 853, 853), ("gold.churn_by_month", 6, 6), ("gold.gold_daily_sales", 621, 621),
        ("gold.gold_menu_item_weekly", 928, 928), ("gold.gold_restaurant_scorecard", 6, 6)]
    assert len(table_of(workdir, "fct_orders").snapshots()) == before[0]  # the upsert changed no row
    assert len(table_of(workdir, "churn_by_month").snapshots()) > before[1]  # an overwrite is always a commit


def test_dbt_docs_writes_the_catalog(walkthrough, capfd):
    project = walkthrough[0] / "dbt"

    assert main(["dbt", "docs", str(project)]) == 0

    capfd.readouterr()
    assert (project / "target" / "catalog.json").is_file() and (project / "target" / "index.html").is_file()
    catalog = (project / "target" / "catalog.json").read_text()
    assert "churn_by_month" in catalog and "fct_orders" in catalog


# --------------------------------------------------------------------------- a batch with a duplicate order_id


@pytest.fixture(scope="module")
def after_batch_one(tmp_path_factory):
    """A work folder with batch 1 loaded and built, and what was published then."""
    workdir = run.prepare_workdir(tmp_path_factory.mktemp("restaurant_one") / "work")
    dataset = generate.generate()
    generate.write_static(workdir / "landing", dataset)
    generate.write_batch(workdir / "landing", dataset, "batch1")
    for name in ("restaurants", "customers", "menu_items", "reviews", "orders", "order_lines"):
        ingest(workdir, name)
    build = run.dbt_build(workdir)
    return workdir, dataset, build


def snapshot_ids(workdir: Path) -> dict[str, list[int]]:
    return {name: [item["snapshot_id"] for item in table_of(workdir, name).snapshots()]
            for name in ("fct_orders", "churn_by_month", "gold_daily_sales", "gold_menu_item_weekly",
                         "gold_restaurant_scorecard")}


def test_a_new_order_that_arrives_in_two_files_fails_the_build_and_publishes_nothing(after_batch_one):
    """One file cannot repeat an order, because the checks of the ingest stop it. Two files can, and dbt sees both."""
    workdir, dataset, first = after_batch_one
    before = snapshot_ids(workdir)
    assert [item["rows_after"] for item in first["published"]] == [745, 5, 534, 795, 6]
    july = dataset.batch_orders("batch2")[10]
    row = {**july, "order_id": "O900001", "updated_at": dt.datetime(2026, 7, 31, 12, 0)}
    for _ in range(2):  # the same order, sent twice, and each file has it once, so the checks of each file pass
        generate._write_csv(workdir / "landing" / "orders.csv", generate.ORDER_COLUMNS, [row],
                            {"order_ts": generate._stamp, "updated_at": generate._stamp,
                             "total_amount": generate._money})
        ingest(workdir, "orders")
    with pytest.raises(run.BuildFailed) as error:
        run.dbt_build(workdir)

    message = without_log_warnings(str(error.value))  # without DuckDB's iceberg extension, a warning comes first
    assert message.startswith("ldp: error: dbt build failed with exit status 1, so nothing was published")
    assert "test.restaurant.unique_fct_orders_order_id" in message and "fail" in message
    assert snapshot_ids(workdir) == before  # no table was touched


# --------------------------------------------------------------------------- a small work folder of our own


def small_workdir(path: Path) -> Path:
    """A work folder with one restaurant, four customers, a menu of one item and a review, and no orders yet."""
    workdir = run.prepare_workdir(path / "work")
    landing = workdir / "landing"
    landing.mkdir(exist_ok=True)
    generate.write_restaurants_xlsx(landing / "restaurants.xlsx", SimpleNamespace(restaurants=[
        {"restaurant_id": "R01", "name": "Dosa Corner", "location": "Bengaluru", "cuisine": "South Indian",
         "created_at": dt.datetime(2022, 1, 1, 9, 0)}]))
    signed_up = dt.datetime(2022, 12, 1, 9, 0)
    generate._write_csv(landing / "customers.csv", ("customer_id", "name", "city", "signed_up_at"),
                        [{"customer_id": f"C{n}", "name": f"Customer {n}", "city": "Bengaluru",
                          "signed_up_at": signed_up} for n in range(1, 5)], {"signed_up_at": generate._stamp})
    generate._write_csv(landing / "menu_items.csv", ("menu_item_id", "restaurant_id", "name", "category", "price"),
                        [{"menu_item_id": "M001", "restaurant_id": "R01", "name": "Thali", "category": "main",
                          "price": 100}], {"price": generate._money})
    generate._write_csv(landing / "reviews.csv",
                        ("review_id", "order_id", "customer_id", "restaurant_id", "rating", "comment", "reviewed_at"),
                        [{"review_id": "V1", "order_id": "O1", "customer_id": "C1", "restaurant_id": "R01",
                          "rating": 5, "comment": "Great food", "reviewed_at": signed_up}],
                        {"reviewed_at": generate._stamp})
    return workdir


def order(order_id: str, customer: str, placed: dt.datetime, total: float, updated: dt.datetime | None = None) -> dict:
    """An order of restaurant R01, last changed five minutes after it was placed unless ``updated`` says when."""
    return {"order_id": order_id, "restaurant_id": "R01", "customer_id": customer, "order_ts": placed,
            "channel": "dine_in", "total_amount": total, "updated_at": updated or placed + dt.timedelta(minutes=5)}


def write_orders(workdir: Path, orders: list[dict]) -> None:
    """Write ``orders.csv``, and ``order_lines.csv`` with one line for each order that has no line yet.

    The raw order lines are append-only, so a line is written once, and a version of an order that was sent before
    brings no line with it.
    """
    landing = workdir / "landing"
    generate._write_csv(landing / "orders.csv", generate.ORDER_COLUMNS, orders,
                        {"order_ts": generate._stamp, "updated_at": generate._stamp, "total_amount": generate._money})
    lined = landing / ".lined"
    done = set(lined.read_text().split()) if lined.exists() else set()
    fresh = {item["order_id"]: item for item in orders if item["order_id"] not in done}
    generate._write_csv(landing / "order_lines.csv", generate.LINE_COLUMNS,
                        [{"order_id": order_id, "line_no": 1, "menu_item_id": "M001", "quantity": 1,
                          "unit_price": item["total_amount"]} for order_id, item in fresh.items()],
                        {"unit_price": generate._money})
    lined.write_text("\n".join(sorted(done | set(fresh))))


def ingest_orders(workdir: Path, *, first: bool = False) -> None:
    """Load the files that :func:`write_orders` wrote, and with ``first`` the small tables that the models read too."""
    for name in ("restaurants", "customers", "menu_items", "reviews") if first else ():
        ingest(workdir, name)
    ingest(workdir, "orders")
    if (workdir / "landing" / "order_lines.csv").read_text().count("\n") > 1:  # a header, and a line at least
        ingest(workdir, "order_lines")


# --------------------------------------------------------------------------- orders in several versions, out of order


def fct_orders_rows(workdir: Path) -> list[tuple[str, float]]:
    rows = run.lake_rows(workdir / "config" / "fct_orders.json",
                         "SELECT order_id, total_amount FROM fct_orders ORDER BY order_id")
    return [(row["order_id"], row["total_amount"]) for row in rows]


def test_fct_orders_keeps_the_newest_version_of_an_order_and_takes_a_late_order_whatever_its_updated_at(tmp_path):
    """What a load on the newest updated_at in the table gets wrong, over two batches.

    Batch 1 holds two versions of O1, so the model has to pick one, and keeping both would make ``order_id`` not
    unique. Batch 2 holds a correction of O1 and a new order, O3, that are both older than the newest row already
    loaded, which that load would skip, leaving O1's total stale and O3 missing. It also holds an old version of O2
    that reached the lake late and must not replace the newer one. Two months of orders make the churn model
    publish something.
    """
    workdir = small_workdir(tmp_path)
    january, february = dt.datetime(2023, 1, 5, 12, 0), dt.datetime(2023, 2, 12, 13, 0)
    write_orders(workdir, [order("O1", "C1", january, 250),
                           order("O1", "C1", january, 300, dt.datetime(2023, 1, 5, 13)),  # changed an hour later
                           order("O2", "C2", february, 100)])
    ingest_orders(workdir, first=True)

    first = run.dbt_build(workdir)

    assert (first["models"], first["tests"], first["passed"]) == (12, 41, 53)
    assert [(item["mode"], item["rows_before"], item["rows_after"]) for item in first["published"][:1]] == [
        ("upsert", 0, 2)]
    assert fct_orders_rows(workdir) == [("O1", 300.0), ("O2", 100.0)]  # the later of the two versions of O1
    write_orders(workdir, [
        order("O3", "C3", dt.datetime(2023, 1, 3, 12, 0), 400),  # new, and older than the newest row loaded
        order("O1", "C1", january, 320, dt.datetime(2023, 2, 1, 9)),  # newer than O1's row, older than that one
        order("O2", "C2", february, 90, february)])  # a version of O2 that is older than the loaded one
    ingest_orders(workdir)

    second = run.dbt_build(workdir)

    assert fct_orders_rows(workdir) == [("O1", 320.0), ("O2", 100.0), ("O3", 400.0)]
    assert [(item["mode"], item["rows_before"], item["rows_after"]) for item in second["published"][:1]] == [
        ("upsert", 2, 3)]


# --------------------------------------------------------------------------- what the generated data does not have


def test_the_gold_tables_rank_a_tie_alike_count_a_quiet_restaurant_as_zero_and_an_item_twice_in_an_order_once(tmp_path):
    """Three cases that the generated data has none of, on a chain of three restaurants and three orders.

    Two restaurants of one city take the same revenue, so they have the same rank. A third restaurant has only an
    old order, so its last 30 days are 0 and not no value. One order has the same item on two lines, so the item
    was ordered in one order, and three of it were ordered.
    """
    workdir = run.prepare_workdir(tmp_path / "work")
    landing = workdir / "landing"
    landing.mkdir(exist_ok=True)
    opened = dt.datetime(2022, 1, 1, 9, 0)
    generate.write_restaurants_xlsx(landing / "restaurants.xlsx", SimpleNamespace(restaurants=[
        {"restaurant_id": "R01", "name": "North Cafe", "location": "Pune", "cuisine": "Cafe", "created_at": opened},
        {"restaurant_id": "R02", "name": "South Cafe", "location": "Pune", "cuisine": "Cafe", "created_at": opened},
        {"restaurant_id": "R03", "name": "Quiet Kitchen", "location": "Delhi", "cuisine": "Thali",
         "created_at": opened}]))
    generate._write_csv(landing / "customers.csv", ("customer_id", "name", "city", "signed_up_at"),
                        [{"customer_id": f"C{n}", "name": f"Customer {n}", "city": "Pune", "signed_up_at": opened}
                         for n in range(1, 4)], {"signed_up_at": generate._stamp})
    generate._write_csv(landing / "menu_items.csv", ("menu_item_id", "restaurant_id", "name", "category", "price"),
                        [{"menu_item_id": f"M00{n}", "restaurant_id": f"R0{n}", "name": "Thali", "category": "main",
                          "price": 100} for n in range(1, 4)], {"price": generate._money})
    generate._write_csv(landing / "reviews.csv",
                        ("review_id", "order_id", "customer_id", "restaurant_id", "rating", "comment", "reviewed_at"),
                        [{"review_id": "V1", "order_id": "O1", "customer_id": "C1", "restaurant_id": "R01",
                          "rating": 4, "comment": "Good", "reviewed_at": opened}], {"reviewed_at": generate._stamp})
    placed = {"O1": ("R01", "C1", dt.datetime(2023, 3, 10, 12, 0), 300),
              "O2": ("R02", "C2", dt.datetime(2023, 3, 10, 13, 0), 300),
              "O3": ("R03", "C3", dt.datetime(2023, 1, 2, 12, 0), 100)}
    orders = [{**order(order_id, customer, when, total), "restaurant_id": restaurant}
              for order_id, (restaurant, customer, when, total) in placed.items()]
    write_orders(workdir, orders)
    lines = [{"order_id": "O1", "line_no": 1, "menu_item_id": "M001", "quantity": 2, "unit_price": 100},
             {"order_id": "O1", "line_no": 2, "menu_item_id": "M001", "quantity": 1, "unit_price": 100},
             {"order_id": "O2", "line_no": 1, "menu_item_id": "M002", "quantity": 3, "unit_price": 100},
             {"order_id": "O3", "line_no": 1, "menu_item_id": "M003", "quantity": 1, "unit_price": 100}]
    generate._write_csv(landing / "order_lines.csv", generate.LINE_COLUMNS, lines, {"unit_price": generate._money})
    for name in ("restaurants", "customers", "menu_items", "reviews", "orders", "order_lines"):
        ingest(workdir, name)

    run.dbt_build(workdir)

    card = run.lake_rows(workdir / "config" / "gold_restaurant_scorecard.json",
                         "SELECT restaurant_id, as_of, orders_30d, revenue_30d, review_count, rating_sum, avg_rating, "
                         "city_rank FROM gold_restaurant_scorecard ORDER BY restaurant_id")
    day = dt.date(2023, 3, 10)
    assert [tuple(row.values()) for row in card] == [
        ("R01", day, 1, 300, 1, 4, 4.0, 1), ("R02", day, 1, 300, 0, None, None, 1),  # a tie in Pune, both first
        ("R03", day, 0, 0, 0, None, None, 1)]  # an old order only, so nothing in the last 30 days
    item = run.lake_rows(workdir / "config" / "gold_menu_item_weekly.json",
                         "SELECT orders, quantity, revenue FROM gold_menu_item_weekly WHERE menu_item_id = 'M001'")
    assert item == [{"orders": 1, "quantity": 3, "revenue": 300.0}]


# --------------------------------------------------------------------------- the churn example of issue #47


@pytest.fixture(scope="module")
def issue_47(tmp_path_factory):
    """The orders of issue #47 loaded and built once: the work folder, and what the build did."""
    workdir = small_workdir(tmp_path_factory.mktemp("restaurant_47"))
    # (customer, order time): January C1 C2 C3, February C4, March C4 and C1, April C1, June C1.
    placed = [("C1", dt.datetime(2023, 1, 5, 12, 0)), ("C2", dt.datetime(2023, 1, 12, 13, 0)),
              ("C3", dt.datetime(2023, 1, 20, 19, 0)), ("C4", dt.datetime(2023, 2, 3, 12, 0)),
              ("C4", dt.datetime(2023, 3, 2, 12, 0)), ("C1", dt.datetime(2023, 3, 15, 20, 0)),
              ("C1", dt.datetime(2023, 4, 10, 12, 0)), ("C1", dt.datetime(2023, 6, 5, 12, 0))]
    write_orders(workdir, [order(f"O{n}", customer, when, 250) for n, (customer, when) in enumerate(placed, start=1)])
    ingest_orders(workdir, first=True)
    return workdir, run.dbt_build(workdir)


def test_churn_counts_a_customer_in_the_month_they_stop_ordering(issue_47):
    """The first month has no row, three customers who stop after January make February 3 of 3, and so on."""
    workdir, build = issue_47

    assert (build["models"], build["tests"], build["passed"]) == (12, 41, 53)
    rows = run.lake_rows(workdir / "config" / "churn_by_month.json",
                         "SELECT month, customers_at_start, churned, churn_rate FROM churn_by_month ORDER BY month")
    assert rows == [
        churn("2023-02-01", 3, 3, 1.0),   # C1, C2 and C3 ordered in January and not in February
        churn("2023-03-01", 1, 0, 0.0),   # C4 ordered in February and again in March
        churn("2023-04-01", 2, 1, 0.5),   # C4 and C1 ordered in March, and only C1 in April
        churn("2023-05-01", 1, 1, 1.0),   # C1 ordered in April and not in May, a month with no order at all
        churn("2023-06-01", 0, 0, None),  # nobody ordered in May, so there is no rate
    ]  # and January, the first month, has no row


def test_the_active_customers_are_the_customers_with_an_order_in_the_month_and_0_when_there_is_none(issue_47):
    workdir = issue_47[0]

    rows = run.lake_rows(workdir / "config" / "churn_by_month.json",
                         "SELECT month, active_customers FROM churn_by_month ORDER BY month")

    assert [(row["month"].isoformat(), row["active_customers"]) for row in rows] == [
        ("2023-02-01", 1),  # C4
        ("2023-03-01", 2),  # C4 and C1
        ("2023-04-01", 1),  # C1
        ("2023-05-01", 0),  # nobody, and the month has a row all the same
        ("2023-06-01", 1),  # C1
    ]


def test_the_example_of_issue_47_runs_through_ldp_metrics_query_and_february_is_100_percent(issue_47, capsys):
    config = issue_47[0] / "config"

    assert main(["metrics", "query", str(config), "churn_rate", "--grain", "month"]) == 0

    lines = without_log_warnings(capsys.readouterr().out).splitlines()
    assert lines[:2] == ["month       churn_rate", "----------  ----------"]
    assert [line.split() for line in lines[2:7]] == [
        ["2023-02-01", "100.0%"], ["2023-03-01", "0.0%"], ["2023-04-01", "50.0%"], ["2023-05-01", "100.0%"],
        ["2023-06-01", "NULL"]]  # June has no churn rate, because nobody ordered in May
    assert not any(line.startswith("2023-01") for line in lines), "January has no row, so it has no value"
    assert lines[7] == "(5 rows)"


def test_the_metrics_of_issue_47_are_the_values_of_the_model_with_no_value_where_the_model_has_none(issue_47):
    config = issue_47[0] / "config"
    answer = metrics.query(config, "churn_rate,active_customers", grain="month").to_pylist()

    assert [(row["month"].isoformat(), row["churn_rate"], row["active_customers"]) for row in answer] == [
        ("2023-02-01", 1.0, 1), ("2023-03-01", 0.0, 2), ("2023-04-01", 0.5, 1), ("2023-05-01", 1.0, 0),
        ("2023-06-01", None, 1)]
    assert "2023-01-01" not in [row["month"].isoformat() for row in answer]
