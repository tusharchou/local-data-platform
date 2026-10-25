"""The gold layer of the restaurant example, ``run.py --layer gold``, with the real dbt, offline.

The layer loads the raw tables, stops a faulty batch at the gate, loads the week again without the faults, builds the
data mart with dbt, checks the gold tables and cuts a release, asks for a metric, and writes an Excel file, a DuckDB
file and a catalog page. ``run.run_restaurant`` runs it once for the module, and the tests read what it returned and
what it wrote. The numbers of the gold tables and of the metrics are worked out here in plain Python from the
generator's own lists, and not with the SQL that builds them, so a mistake in the SQL shows. Every test is marked
``dbt``, so it skips without the extra, and the ``dbt`` CI job runs it.
"""

import collections
import contextlib
import datetime as dt
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_data_platform import Config, metrics
from local_data_platform.cli import main
from local_data_platform.gold import release as releases
from local_data_platform.pipeline.builders import iceberg_from_config
from tests.support import load_example_module

pytestmark = pytest.mark.dbt

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "restaurant"
run = load_example_module("restaurant_run_gold", EXAMPLE / "run.py")
generate = run.generate

GOLD_TABLES = ["churn_by_month", "gold_daily_sales", "gold_menu_item_weekly", "gold_restaurant_scorecard"]


@pytest.fixture(scope="module")
def mart(tmp_path_factory):
    """One run of the gold layer: where it ran, what it returned and what it printed."""
    workdir = tmp_path_factory.mktemp("restaurant_gold") / "work"
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        whole = run.run_restaurant(workdir, layer="gold")
    root = Path(whole["workdir"])
    return SimpleNamespace(workdir=root, config=root / "config", summary=whole["gold"], whole=whole,
                           text=printed.getvalue(), dataset=generate.generate())


def monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def rows_of(mart, table: str) -> list[dict]:
    return run.lake_rows(mart.config, f"SELECT * FROM gold.{table}")


# --------------------------------------------------------------------------- the story


def test_the_layer_runs_offline_and_tells_its_story_in_order(mart):
    steps = ["== 1. Load the raw tables", "== 2. A faulty batch arrives, and the gate stops it",
             "== 3. The restaurant sends the week again, without the faults", "== 4. Model it with dbt: ldp dbt build",
             "== 5. Check the gold tables and cut a release: ldp gold build",
             "== 6. Ask for a number: ldp metrics query",
             "== 7. Hand the tables to people who do not use Python", "== 8. Summary"]

    positions = [mart.text.index(step) for step in steps]

    assert positions == sorted(positions) and mart.whole["layer"] == "gold" and mart.whole["duration_s"] < 120
    assert "finished in" in mart.text and "Try these next:" in mart.text
    assert [line.split()[1:3] for line in mart.text.splitlines() if line.startswith("     ldp ")][-5:] == [
        ["gold", "list"], ["gold", "show"], ["metrics", "query"], ["gold", "releases"], ["mcp", "--config"]]


def test_the_raw_tables_hold_the_exports_and_the_two_batches_of_orders_and_lines(mart):
    assert mart.summary["raw_rows"] == {"raw.restaurants": 6, "raw.customers": 150, "raw.menu_items": 36,
                                        "raw.reviews": 94, "raw.orders": 853, "raw.order_lines": 2113}


# --------------------------------------------------------------------------- the faulty batch


def test_the_faulty_batch_is_stopped_at_the_gate_and_each_failed_check_is_named(mart):
    gate = mart.summary["gate"]

    assert gate["exit"] == 3 and gate["failed"] == ["accepted_values", "unique", "range"]
    assert gate["faults"] == ["negative_amount", "unknown_restaurant", "duplicated_order"]
    assert "3 of 7 data quality checks failed:" in mart.text
    assert "accepted_values(restaurant_id): 1 of 31 rows have a restaurant_id outside" in mart.text
    assert "unique(order_id, updated_at): 1 duplicated key(s)" in mart.text
    assert "range(total_amount): 1 of 31 rows violate total_amount >= 0" in mart.text
    for fault in ("O000855  total_amount is -40.00", "O000858  restaurant_id is R99",
                  "O000861  the order is in the file twice"):
        assert fault in mart.text


def test_nothing_of_the_faulty_batch_was_written(mart):
    gate = mart.summary["gate"]

    assert gate["orders_before"] == gate["orders_after"] == 853
    orders = run.lake_rows(mart.config, "SELECT order_id, restaurant_id, total_amount FROM raw.orders")
    assert {row["restaurant_id"] for row in orders} <= {row["restaurant_id"] for row in mart.dataset.restaurants}
    assert min(row["total_amount"] for row in orders) > 0
    assert len({row["order_id"] for row in orders}) == len(orders) == 883, "every order once, after the clean week"


def test_the_week_without_the_faults_loads_and_the_lines_come_with_it(mart):
    assert mart.summary["week"] == {"orders": 30, "order_lines": 82, "orders_after": 883}


# --------------------------------------------------------------------------- the build


def test_dbt_builds_twelve_models_passes_41_data_tests_and_publishes_five_tables(mart):
    build = mart.summary["build"]

    assert (build["models"], build["tests"], build["passed"]) == (12, 41, 53)
    assert [(item["table"], item["mode"], item["rows_before"]) for item in build["published"]] == [
        ("silver.fct_orders", "upsert", 0), ("gold.churn_by_month", "overwrite", 0),
        ("gold.gold_daily_sales", "overwrite", 0), ("gold.gold_menu_item_weekly", "overwrite", 0),
        ("gold.gold_restaurant_scorecard", "overwrite", 0)]
    assert [item["rows_after"] for item in build["published"]] == [883, 7, 643, 953, 6]


def test_the_row_counts_of_every_table_are_the_ones_worked_out_from_the_generated_data(mart):
    dataset = mart.dataset
    days = {(order["restaurant_id"], order["order_ts"].date()) for order in dataset.orders}
    orders = {order["order_id"]: order for order in dataset.orders}
    weeks = {(line["menu_item_id"], monday(orders[line["order_id"]]["order_ts"].date()))
             for line in dataset.order_lines}

    assert (len(days), len(weeks)) == (643, 953)
    assert {name: info["rows"] for name, info in mart.summary["tables"].items()} == {
        "raw.restaurants": 6, "raw.customers": 150, "raw.menu_items": 36, "raw.reviews": 94, "raw.orders": 883,
        "raw.order_lines": 2195, "silver.fct_orders": 883, "gold.churn_by_month": 7,
        "gold.gold_daily_sales": len(days), "gold.gold_menu_item_weekly": len(weeks),
        "gold.gold_restaurant_scorecard": len(dataset.restaurants)}


def test_every_gold_table_carries_its_contract_and_every_column_is_described(mart):
    expected = {"churn_by_month": ("customer insights", "one row for each month"),
                "gold_daily_sales": ("restaurant operations",
                                     "one row for each restaurant on each day it had an order"),
                "gold_menu_item_weekly": ("kitchen operations",
                                          "one row for each menu item in each week in which it was ordered"),
                "gold_restaurant_scorecard": ("restaurant operations", "one row for each restaurant")}

    for name, (owner, grain) in expected.items():
        table = iceberg_from_config(Config.from_json(mart.config / f"{name}.json"), "target").table()
        assert table.properties["ldp.layer"] == "gold" and table.properties["comment"], name
        assert (table.properties["ldp.owner"], table.properties["ldp.grain"]) == (owner, grain)
        assert all(field.doc for field in table.schema().fields), f"{name} describes every column"


def test_the_catalog_lists_each_gold_table_with_its_last_run_its_gate_and_its_release(mart, capsys):
    assert main(["gold", "list", str(mart.config), "--json"]) == 0

    listed = json.loads(capsys.readouterr().out)
    entries = {entry["name"]: entry for entry in listed["tables"]}
    assert sorted(entries) == sorted(GOLD_TABLES) and listed["counts"] == {"tables": 4, "layers": {"gold": 4},
                                                                           "lacking": 0}
    for name, checks in (("churn_by_month", 4), ("gold_daily_sales", 5), ("gold_menu_item_weekly", 5),
                         ("gold_restaurant_scorecard", 5)):
        entry = entries[name]
        assert (entry["run"]["status"], entry["release"], entry["release_state"]) == ("published", "r1", "current")
        assert (entry["quality"]["passed"], entry["quality"]["checks_run"], entry["quality"]["checks_failed"]) == (
            True, checks, 0)
        assert entry["problems"] == [] and entry["tags"] and all(column["description"] for column in entry["columns"])
    assert [metric["name"] for metric in entries["gold_daily_sales"]["metrics"]] == ["revenue", "orders", "avg_ticket"]
    assert [metric["name"] for metric in entries["gold_restaurant_scorecard"]["metrics"]] == ["avg_rating"]


def test_every_gold_table_has_the_columns_of_its_contract_in_the_same_order(mart):
    yaml = pytest.importorskip("yaml")
    described = {model["name"]: [column["name"] for column in model["columns"]]
                 for model in yaml.safe_load((EXAMPLE / "dbt" / "models" / "schema.yml").read_text())["models"]}

    for name in GOLD_TABLES:
        table = iceberg_from_config(Config.from_json(mart.config / f"{name}.json"), "target").table()

        assert [field.name for field in table.schema().fields] == described[name], name


# --------------------------------------------------------------------------- the numbers of the gold tables


def test_the_daily_sales_are_the_orders_the_revenue_and_the_average_ticket_of_each_restaurant_day(mart):
    restaurants = {row["restaurant_id"]: row for row in mart.dataset.restaurants}
    worked = collections.defaultdict(lambda: [0, 0])
    for order in mart.dataset.orders:
        entry = worked[(order["restaurant_id"], order["order_ts"].date())]
        entry[0] += 1
        entry[1] += order["total_amount"]

    rows = rows_of(mart, "gold_daily_sales")

    assert len(rows) == len(worked)
    for row in rows:
        orders, revenue = worked[(row["restaurant_id"], row["day"])]
        place = restaurants[row["restaurant_id"]]
        assert (row["orders"], row["revenue"]) == (orders, revenue)
        assert row["avg_ticket"] == pytest.approx(revenue / orders, abs=0.005)
        assert (row["restaurant_name"], row["city"], row["cuisine"]) == (
            place["name"], place["location"], place["cuisine"])


def test_the_menu_item_weeks_are_the_lines_of_the_orders_added_up_by_item_and_monday(mart):
    orders = {order["order_id"]: order for order in mart.dataset.orders}
    items = {item["menu_item_id"]: item for item in mart.dataset.menu_items}
    worked = collections.defaultdict(lambda: {"orders": set(), "quantity": 0, "revenue": 0})
    for line in mart.dataset.order_lines:
        entry = worked[(line["menu_item_id"], monday(orders[line["order_id"]]["order_ts"].date()))]
        entry["orders"].add(line["order_id"])
        entry["quantity"] += line["quantity"]
        entry["revenue"] += line["quantity"] * line["unit_price"]

    rows = rows_of(mart, "gold_menu_item_weekly")

    assert len(rows) == len(worked)
    for row in rows:
        entry = worked[(row["menu_item_id"], row["week"])]
        assert row["week"].weekday() == 0
        assert (row["orders"], row["quantity"], row["revenue"]) == (len(entry["orders"]), entry["quantity"],
                                                                    entry["revenue"])
        assert (row["item_name"], row["category"]) == (items[row["menu_item_id"]]["name"],
                                                       items[row["menu_item_id"]]["category"])


def test_the_scorecard_has_the_last_30_days_the_rating_and_the_rank_in_the_city(mart):
    dataset = mart.dataset
    as_of = max(order["order_ts"].date() for order in dataset.orders)
    first = as_of - dt.timedelta(days=29)
    recent = collections.defaultdict(lambda: [0, 0])
    for order in dataset.orders:
        if first <= order["order_ts"].date() <= as_of:
            recent[order["restaurant_id"]][0] += 1
            recent[order["restaurant_id"]][1] += order["total_amount"]
    stars = collections.defaultdict(list)
    for review in dataset.reviews:
        stars[review["restaurant_id"]].append(review["rating"])
    city = {row["restaurant_id"]: row["location"] for row in dataset.restaurants}

    rows = {row["restaurant_id"]: row for row in rows_of(mart, "gold_restaurant_scorecard")}

    assert (as_of, first) == (dt.date(2026, 8, 7), dt.date(2026, 7, 9)) and len(rows) == 6
    for restaurant_id, row in rows.items():
        orders, revenue = recent[restaurant_id]
        assert row["as_of"] == as_of and (row["orders_30d"], row["revenue_30d"]) == (orders, revenue)
        assert (row["review_count"], row["rating_sum"]) == (len(stars[restaurant_id]), sum(stars[restaurant_id]))
        assert row["avg_rating"] == pytest.approx(sum(stars[restaurant_id]) / len(stars[restaurant_id]), abs=0.005)
        better = [other for other in rows if city[other] == city[restaurant_id]
                  and recent[other][1] > recent[restaurant_id][1]]
        assert row["city_rank"] == len(better) + 1
    assert sorted(row["city_rank"] for row in rows.values() if row["city"] == "Mumbai") == [1, 2]
    assert [row["city_rank"] for row in rows.values() if row["city"] != "Mumbai"] == [1, 1, 1, 1]


def test_churn_is_counted_in_the_month_a_customer_stops_ordering_and_every_month_has_a_row(mart):
    by_month = collections.defaultdict(set)
    for order in mart.dataset.orders:
        by_month[(order["order_ts"].year, order["order_ts"].month)].add(order["customer_id"])
    months = [(2026, month) for month in range(1, 9)]

    rows = {row["month"]: row for row in rows_of(mart, "churn_by_month")}

    assert sorted(rows) == [dt.date(*month, 1) for month in months[1:]]
    for before, now in zip(months, months[1:]):
        row = rows[dt.date(*now, 1)]
        start, lost = by_month[before], by_month[before] - by_month[now]
        assert (row["customers_at_start"], row["churned"], row["active_customers"]) == (
            len(start), len(lost), len(by_month[now]))
        assert row["churn_rate"] == pytest.approx(len(lost) / len(start))


# --------------------------------------------------------------------------- the release


def test_ldp_gold_build_checked_the_four_tables_and_cut_release_r1_with_all_of_them(mart):
    build = mart.summary["gold_build"]
    known = releases.list_releases(mart.config)

    assert build["exit"] == 0 and build["release"] == "r1" and len(known) == 1
    assert sorted(build["tables"]) == sorted(f"gold.{name}" for name in GOLD_TABLES)
    assert known[0].snapshots() == build["snapshots"] and known[0].rows == 7 + 643 + 953 + 6
    assert "release r1 cut with 4 tables and 1609 rows in gold" in mart.text
    assert "metrics: 3 semantic models and 6 metrics fit the gold tables" in mart.text
    for name, checks in (("churn_by_month", 4), ("gold_daily_sales", 5), ("gold_menu_item_weekly", 5),
                         ("gold_restaurant_scorecard", 5)):
        assert re.search(rf"^\s+{name}\s+checked\s+\d+ rows\s+snapshot \d+\s+{checks} of {checks} checks passed$",
                         mart.text, flags=re.MULTILINE), name


def test_a_reader_of_the_release_sees_the_snapshots_that_it_names(mart):
    release = releases.current_release(mart.config)
    current = {f"gold.{name}": iceberg_from_config(Config.from_json(mart.config / f"{name}.json"),
                                                   "target").table().current_snapshot().snapshot_id
               for name in GOLD_TABLES}

    assert release.snapshots() == current, "nothing has written to the gold tables since the build"
    assert "read release r1" in mart.text


# --------------------------------------------------------------------------- the metrics


def test_the_metric_query_gives_revenue_orders_and_the_average_ticket_by_cuisine_and_month(mart):
    cuisine = {row["restaurant_id"]: row["cuisine"] for row in mart.dataset.restaurants}
    worked = collections.defaultdict(lambda: [0, 0])
    for order in mart.dataset.orders:
        day = order["order_ts"].date()
        if day >= dt.date(2026, 7, 1):
            entry = worked[(day.replace(day=1), cuisine[order["restaurant_id"]])]
            entry[0] += order["total_amount"]
            entry[1] += 1

    rows = mart.summary["metric"]

    assert [(row["month"], row["cuisine"]) for row in rows] == sorted(worked)
    for row in rows:
        revenue, orders = worked[(row["month"], row["cuisine"])]
        assert (row["revenue"], row["orders"]) == (revenue, orders)
        assert row["avg_ticket"] == pytest.approx(revenue / orders)
    assert len(rows) == 12 and "(12 rows)" in mart.text


def test_the_average_ticket_of_a_month_is_its_revenue_over_its_orders_and_not_the_average_of_the_days(mart):
    january = metrics.query(mart.config, "revenue,orders,avg_ticket", grain="month", start="2026-01-01",
                            end="2026-01-31").to_pylist()
    days = rows_of(mart, "gold_daily_sales")
    days = [row for row in days if row["day"].month == 1]

    [month] = january
    revenue, orders = sum(row["revenue"] for row in days), sum(row["orders"] for row in days)
    assert month["avg_ticket"] == pytest.approx(revenue / orders)
    assert month["avg_ticket"] != pytest.approx(sum(row["avg_ticket"] for row in days) / len(days))


def test_the_rating_of_a_city_is_all_its_stars_over_all_its_reviews(mart):
    city = {row["restaurant_id"]: row["location"] for row in mart.dataset.restaurants}
    stars = collections.defaultdict(list)
    for review in mart.dataset.reviews:
        stars[city[review["restaurant_id"]]].append(review["rating"])

    answer = {row["city"]: row["avg_rating"] for row in metrics.query(mart.config, "avg_rating", by="city").to_pylist()}

    assert answer == pytest.approx({name: sum(values) / len(values) for name, values in stars.items()})
    assert len(stars["Mumbai"]) == 42, "two restaurants, so an average of their averages would be another number"


def test_churn_and_active_customers_are_the_numbers_of_the_model_for_every_month(mart):
    answer = metrics.query(mart.config, "churn_rate,active_customers", grain="month").to_pylist()
    model = {row["month"]: row for row in rows_of(mart, "churn_by_month")}

    assert [row["month"] for row in answer] == sorted(model)
    for row in answer:
        assert (row["churn_rate"], row["active_customers"]) == (model[row["month"]]["churn_rate"],
                                                                model[row["month"]]["active_customers"])


# --------------------------------------------------------------------------- the files for people


def test_the_excel_file_has_the_scorecard_and_the_contract_of_the_table_on_a_second_sheet(mart):
    openpyxl = pytest.importorskip("openpyxl")
    path = Path(mart.summary["files"]["scorecard.xlsx"])

    book = openpyxl.load_workbook(path, read_only=True)

    assert book.sheetnames == ["gold_restaurant_scorecard", "about"]
    rows = list(book["gold_restaurant_scorecard"].iter_rows(values_only=True))
    assert rows[0] == ("restaurant_id", "restaurant_name", "city", "cuisine", "as_of", "orders_30d", "revenue_30d",
                       "review_count", "rating_sum", "avg_rating", "city_rank") and len(rows) == 7
    about = {row[0]: row[1] for row in book["about"].iter_rows(values_only=True) if len(row) > 1 and row[0]}
    assert about["Owner"] == "restaurant operations" and about["Release"] == "r1" and about["Rows"] == 6


def test_the_duckdb_file_has_every_gold_table_with_its_comments_a_catalog_and_the_six_metrics(mart):
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(mart.summary["files"]["gold.duckdb"], read_only=True)
    try:
        names = [row[0] for row in connection.execute("SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]
        counts = {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchall()[0][0] for name in names}
        comment = connection.execute("SELECT comment FROM duckdb_tables() WHERE table_name = 'gold_daily_sales'"
                                     ).fetchall()
        released = connection.execute("SELECT DISTINCT release FROM _catalog").fetchall()
        defined = [row[0] for row in connection.execute("SELECT metric FROM _metrics ORDER BY 1").fetchall()]
    finally:
        connection.close()

    assert names == ["_catalog", "_metrics", *GOLD_TABLES]
    assert counts == {"_catalog": 4, "_metrics": 6, "churn_by_month": 7, "gold_daily_sales": 643,
                      "gold_menu_item_weekly": 953, "gold_restaurant_scorecard": 6}
    assert comment == [("Orders, revenue and the average ticket for each restaurant on each day it had an order. "
                        "The average ticket is the revenue divided by the orders.",)]
    assert released == [("r1",)] and defined == [
        "active_customers", "avg_rating", "avg_ticket", "churn_rate", "orders", "revenue"]


def test_the_catalog_page_names_the_four_tables_their_owners_and_the_release_and_needs_no_network(mart):
    page = Path(mart.summary["files"]["catalog.html"]).read_text()

    for name in GOLD_TABLES:
        assert f"gold.{name}" in page
    for words in ("customer insights", "restaurant operations", "kitchen operations", "r1"):
        assert words in page
    assert "http://" not in page.replace("http://www.w3.org", "") and "https://" not in page
    assert 'src="http' not in page and 'href="http' not in page


# --------------------------------------------------------------------------- the gates, which change the tables


def bad_table(mart, name: str, change):
    """Rewrite the gold table ``name`` with ``change`` applied to its rows, and return a function that puts it back."""
    table = iceberg_from_config(Config.from_json(mart.config / f"{name}.json"), "target")
    original = table.get()
    table.put(change(original), mode="overwrite")
    return lambda: table.put(original, mode="overwrite")


def set_first(table, column: str, value):
    values = table.column(column).to_pylist()
    values[0] = value
    index = table.schema.get_field_index(column)
    import pyarrow as pa

    return table.set_column(index, column, pa.array(values, type=table.schema.field(column).type))


def repeat_first(table):
    import pyarrow as pa

    return pa.concat_tables([table, table.slice(0, 1)])


@pytest.mark.parametrize("name, change, failed", [
    ("gold_daily_sales", lambda table: set_first(table, "revenue", -5.0), "range(revenue)"),
    ("gold_daily_sales", repeat_first, "unique(restaurant_day_id)"),
    ("gold_menu_item_weekly", lambda table: set_first(table, "quantity", 0), "range(quantity)"),
    ("gold_menu_item_weekly", repeat_first, "unique(menu_item_week_id)"),
    ("gold_restaurant_scorecard", lambda table: set_first(table, "avg_rating", 7.0), "range(avg_rating)"),
    ("gold_restaurant_scorecard", repeat_first, "unique(restaurant_id)"),
    ("churn_by_month", lambda table: set_first(table, "churn_rate", 1.5), "range(churn_rate)"),
    ("churn_by_month", repeat_first, "unique(month)"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_a_gold_table_that_breaks_its_gate_blocks_the_release_and_readers_keep_the_last_one(
        mart, name, change, failed, capsys):
    before = releases.current_release(mart.config)
    restore = bad_table(mart, name, change)
    try:
        status = main(["gold", "build", str(mart.config)])
        captured = capsys.readouterr()
    finally:
        restore()

    assert status == 3, captured.err
    assert failed in captured.err + captured.out
    assert [release.name for release in releases.list_releases(mart.config)] == [before.name]
    assert releases.current_release(mart.config).snapshots() == before.snapshots()
    assert "no release cut" in captured.out


def test_the_tables_are_healthy_again_after_the_gate_tests_and_a_new_build_cuts_a_second_release(mart, capsys):
    assert main(["gold", "build", str(mart.config)]) == 0

    out = capsys.readouterr().out
    assert [release.name for release in releases.list_releases(mart.config)] == ["r2", "r1"]
    assert "release r2 cut with 4 tables and 1609 rows in gold" in out
