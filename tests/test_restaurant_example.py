"""The restaurant example (``examples/restaurant``): its generator, configs, ingest, workdir and dbt project.

Nothing here runs dbt, so it runs in every CI job. ``test_restaurant_warehouse.py`` runs the whole walkthrough with
dbt and checks the numbers it prints.
"""

import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import pytest

from local_data_platform import Config
from local_data_platform.dbt_layer.project import DbtProject
from local_data_platform.exceptions import DataQualityError, EngineNotFound
from local_data_platform.format.excel import Excel
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.pipeline.registry import create_pipeline
from local_data_platform.spec import plan, validate_spec
from tests.support import load_example_module

pytest.importorskip("openpyxl")

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "restaurant"
run = load_example_module("restaurant_run", EXAMPLE / "run.py")
generate = run.generate

INGEST_CONFIGS = ("restaurants_to_csv", "restaurants", "customers", "menu_items", "reviews", "orders", "order_lines")


@pytest.fixture(scope="module")
def dataset():
    return generate.generate()


@pytest.fixture
def workdir(tmp_path):
    return run.prepare_workdir(tmp_path / "work")


def table_of(workdir: Path, name: str):
    return iceberg_from_config(Config.from_json(workdir / "config" / f"{name}.json"), "target")


def ingest(workdir: Path, name: str):
    return create_pipeline(Config.from_json(workdir / "config" / f"{name}.json")).run()


def land_batch_one(workdir: Path, dataset) -> None:
    generate.write_static(workdir / "landing", dataset)
    generate.write_batch(workdir / "landing", dataset, "batch1")


# --------------------------------------------------------------------------- the generated data


def test_the_generator_is_deterministic_and_the_seed_matters(dataset):
    again = generate.generate()
    other = generate.generate(8)

    assert again.orders == dataset.orders and again.order_lines == dataset.order_lines
    assert again.customers == dataset.customers and again.reviews == dataset.reviews
    assert other.orders != dataset.orders


def test_the_dataset_has_the_shape_the_example_describes(dataset):
    assert (len(dataset.restaurants), len(dataset.menu_items), len(dataset.customers)) == (6, 36, 150)
    assert (len(dataset.orders), len(dataset.order_lines), len(dataset.reviews)) == (883, 2195, 94)
    assert dataset.batches() == ["batch1", "batch2", *(f"2026-08-0{day}" for day in range(1, 8))]
    sizes = {batch: len(dataset.batch_orders(batch)) for batch in dataset.batches()}
    assert sizes == {"batch1": 745, "batch2": 108, "2026-08-01": 3, "2026-08-02": 4, "2026-08-03": 7,
                     "2026-08-04": 7, "2026-08-05": 1, "2026-08-06": 3, "2026-08-07": 5}


def test_batch_one_is_the_first_half_of_the_year_and_batch_two_is_july_and_five_late_orders(dataset):
    first, second = dataset.batch_orders("batch1"), dataset.batch_orders("batch2")
    late = [order for order in second if order["order_ts"].date() <= generate.BATCH1_END]

    assert all(generate.FIRST_DAY <= order["order_ts"].date() <= generate.BATCH1_END for order in first)
    assert len(late) == generate.LATE_ORDERS == 5
    july = [order for order in second if order not in late]
    assert len(july) == 103 and all(order["order_ts"].month == 7 for order in july)
    assert all(order["order_ts"] >= dt.datetime(2026, 6, 22) for order in late)  # placed at the end of June


def test_the_late_orders_reach_the_lake_in_july(dataset):
    late = [order for order in dataset.batch_orders("batch2") if order["order_ts"].month == 6]

    assert all(order["updated_at"].month == 7 and order["updated_at"].day <= 3 for order in late)
    # Batch 2 is newer than batch 1 on updated_at, yet the late orders carry the order_ts of June.
    assert max(order["updated_at"] for order in dataset.batch_orders("batch1")) < min(
        order["updated_at"] for order in dataset.batch_orders("batch2"))


def test_the_late_orders_belong_to_customers_who_looked_as_if_they_had_stopped_in_june(dataset):
    first_half = dataset.batch_orders("batch1")
    may = {order["customer_id"] for order in first_half if order["order_ts"].month == 5}
    june = {order["customer_id"] for order in first_half if order["order_ts"].month == 6}
    late = [order for order in dataset.batch_orders("batch2") if order["order_ts"].month == 6]

    customers = {order["customer_id"] for order in late}
    assert len(customers) == 5 and customers <= may and not customers & june


def test_every_order_is_updated_after_it_is_placed_and_in_opening_hours(dataset):
    for order in dataset.orders:
        assert order["updated_at"] > order["order_ts"], order
        if order["order_ts"].month != 6 or order["batch"] != "batch2":
            assert order["updated_at"].date() == order["order_ts"].date()  # the late orders sync days later
        assert dt.time(10, 0) <= order["order_ts"].time() < dt.time(22, 0)


def test_every_key_refers_to_something_and_ids_are_unique(dataset):
    restaurants = {row["restaurant_id"] for row in dataset.restaurants}
    customers = {row["customer_id"] for row in dataset.customers}
    items = {row["menu_item_id"]: row for row in dataset.menu_items}
    orders = {row["order_id"]: row for row in dataset.orders}

    assert len(orders) == len(dataset.orders) and len(customers) == len(dataset.customers)
    assert {row["restaurant_id"] for row in dataset.orders} <= restaurants
    assert {row["customer_id"] for row in dataset.orders} <= customers
    assert {row["channel"] for row in dataset.orders} == {"dine_in", "takeaway", "delivery"}
    assert all(line["order_id"] in orders and line["menu_item_id"] in items for line in dataset.order_lines)
    assert all(items[line["menu_item_id"]]["restaurant_id"] == orders[line["order_id"]]["restaurant_id"]
               and items[line["menu_item_id"]]["price"] == line["unit_price"] for line in dataset.order_lines)
    assert all(row["order_id"] in orders and row["customer_id"] == orders[row["order_id"]]["customer_id"]
               for row in dataset.reviews)


def test_an_order_total_is_the_sum_of_its_lines(dataset):
    totals = {}
    for line in dataset.order_lines:
        totals[line["order_id"]] = totals.get(line["order_id"], 0) + line["quantity"] * line["unit_price"]

    assert totals == {order["order_id"]: order["total_amount"] for order in dataset.orders}


# --------------------------------------------------------------------------- the files


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_static_tables_are_written_once_and_the_restaurants_as_a_workbook(tmp_path, dataset):
    written = generate.write_static(tmp_path, dataset)

    assert [path.name for path in written] == list(generate.STATIC_FILES)
    assert len(read_csv(tmp_path / "customers.csv")) == 150 and len(read_csv(tmp_path / "menu_items.csv")) == 36
    assert read_csv(tmp_path / "customers.csv")[0]["signed_up_at"] == dataset.customers[0]["signed_up_at"].strftime(
        "%Y-%m-%d %H:%M:%S")
    assert read_csv(tmp_path / "reviews.csv")[0].keys() == {
        "review_id", "order_id", "customer_id", "restaurant_id", "rating", "comment", "reviewed_at"}


def test_a_batch_is_written_as_the_orders_and_lines_of_that_batch_only(tmp_path, dataset):
    orders, lines = generate.write_batch(tmp_path, dataset, "batch2")

    assert (orders.name, lines.name) == ("orders.csv", "order_lines.csv")
    rows = read_csv(orders)
    assert len(rows) == 108 and list(rows[0]) == list(generate.ORDER_COLUMNS)
    assert all(re.fullmatch(r"\d+\.\d\d", row["total_amount"]) for row in rows)  # a decimal point in every amount
    assert {line["order_id"] for line in read_csv(lines)} == {row["order_id"] for row in rows}
    assert generate.write_batch(tmp_path, dataset, "batch1", "o1.csv", "l1.csv")[0].name == "o1.csv"


def test_an_unknown_batch_is_refused(tmp_path, dataset):
    with pytest.raises(ValueError, match="unknown batch 'batch3'; the batches are"):
        generate.write_batch(tmp_path, dataset, "batch3")


def test_a_drop_is_the_orders_of_a_day_and_lines_that_carry_the_time_of_their_order(tmp_path, dataset):
    orders, lines = generate.write_drop(tmp_path, dataset, dt.date(2026, 8, 3))

    assert (orders.name, lines.name) == ("orders_2026-08-03.csv", "order_lines_2026-08-03.csv")
    placed = {row["order_id"]: row["order_ts"] for row in read_csv(orders)}
    assert len(placed) == 7 and all(stamp.startswith("2026-08-03 ") for stamp in placed.values())
    rows = read_csv(lines)
    assert len(rows) == 20 and list(rows[0]) == list(generate.DROP_LINE_COLUMNS)
    assert all(row["order_ts"] == placed[row["order_id"]] for row in rows), "a line has the time of its order"
    assert all(re.fullmatch(r"\d+\.\d\d", row["unit_price"]) for row in rows)  # a decimal point in every price
    assert generate.write_drop(tmp_path, dataset, "2026-08-04")[0].name == "orders_2026-08-04.csv"


@pytest.mark.parametrize("day", ["batch1", "2026-07-31", "2026-08-08", dt.date(2026, 8, 8), ""])
def test_a_day_without_a_drop_is_refused(tmp_path, dataset, day):
    with pytest.raises(ValueError, match="unknown drop .*; the drops are .*2026-08-01.*2026-08-07"):
        generate.write_drop(tmp_path, dataset, day)

    assert list(tmp_path.iterdir()) == []


def test_the_seven_drops_hold_the_30_orders_of_august_and_each_line_has_its_order_ts(tmp_path, dataset):
    generate.write_all(tmp_path, dataset)

    drops = [read_csv(tmp_path / f"orders_2026-08-0{day}.csv") for day in range(1, 8)]
    lines = [read_csv(tmp_path / f"order_lines_2026-08-0{day}.csv") for day in range(1, 8)]
    assert [len(rows) for rows in drops] == [3, 4, 7, 7, 1, 3, 5] and sum(map(len, drops)) == 30
    assert all(list(rows[0]) == list(generate.DROP_LINE_COLUMNS) for rows in lines)
    batch = read_csv(tmp_path / "order_lines_batch1.csv")
    assert list(batch[0]) == list(generate.LINE_COLUMNS), "the batches keep the columns they had"


def test_two_restaurants_share_a_city_so_that_a_rank_in_a_city_means_something(dataset):
    cities = [row["location"] for row in dataset.restaurants]

    assert sorted(set(cities)) == ["Bengaluru", "Delhi", "Hyderabad", "Kolkata", "Mumbai"]
    assert [row["restaurant_id"] for row in dataset.restaurants if row["location"] == "Mumbai"] == ["R02", "R04"]


def test_the_first_week_of_august_is_the_seven_drops_as_one_batch(tmp_path, dataset):
    orders, lines = generate.write_week(tmp_path, dataset)

    assert (orders.name, lines.name) == ("orders.csv", "order_lines.csv")
    rows = read_csv(orders)
    assert len(rows) == 30 and list(rows[0]) == list(generate.ORDER_COLUMNS)
    assert [row["order_id"] for row in rows] == [order["order_id"] for order in dataset.week_orders()]
    assert {row["order_ts"][:10] for row in rows} == {f"2026-08-0{day}" for day in range(1, 8)}
    assert len(read_csv(lines)) == 82 and list(read_csv(lines)[0]) == list(generate.LINE_COLUMNS)
    assert {row["order_id"] for row in read_csv(lines)} == {row["order_id"] for row in rows}


def test_the_faulty_week_has_three_faults_each_in_an_order_of_its_own(tmp_path, dataset):
    path, faults = generate.write_faulty_week(tmp_path, dataset)

    rows = read_csv(path)
    assert path.name == "orders.csv" and len(rows) == 31, "the 30 orders of the week and one that is there twice"
    assert [(fault.kind, fault.order_id) for fault in faults] == [
        ("negative_amount", "O000855"), ("unknown_restaurant", "O000858"), ("duplicated_order", "O000861")]
    by_id = {fault.order_id: fault for fault in faults}
    assert len(by_id) == 3
    assert [float(row["total_amount"]) for row in rows if row["order_id"] == "O000855"] == [-40.0]
    assert [row["restaurant_id"] for row in rows if row["order_id"] == "O000858"] == [generate.UNKNOWN_RESTAURANT]
    twice = [row for row in rows if row["order_id"] == "O000861"]
    assert len(twice) == 2 and twice[0] == twice[1], "the same order_id with the same updated_at"
    clean = {order["order_id"] for order in dataset.week_orders()}
    assert {row["order_id"] for row in rows} == clean
    assert generate.UNKNOWN_RESTAURANT not in {row["restaurant_id"] for row in dataset.restaurants}


def test_the_faulty_week_does_not_change_the_dataset(dataset):
    before = [dict(order) for order in dataset.week_orders()]

    dataset.faulty_week()

    assert dataset.week_orders() == before


def test_the_dimensions_are_written_as_csv_and_need_no_excel_reader(tmp_path, dataset, monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # an import of it fails, as if the excel extra were missing

    written = generate.write_dimensions(tmp_path, dataset)

    assert [path.name for path in written] == ["restaurants.csv", "customers.csv", "menu_items.csv", "reviews.csv"]
    restaurants = read_csv(tmp_path / "restaurants.csv")
    assert list(restaurants[0]) == ["restaurant_id", "name", "location", "cuisine", "created_at"]
    assert len(restaurants) == 6 and len(read_csv(tmp_path / "customers.csv")) == 150
    assert len(read_csv(tmp_path / "menu_items.csv")) == 36 and len(read_csv(tmp_path / "reviews.csv")) == 94
    assert restaurants[0]["created_at"] == dataset.restaurants[0]["created_at"].strftime("%Y-%m-%d %H:%M:%S")


def test_the_restaurant_workbook_has_a_title_row_and_an_about_sheet_first(tmp_path, dataset):
    path = generate.write_restaurants_xlsx(tmp_path / "restaurants.xlsx", dataset)

    table = Excel("restaurants", path, sheet="Restaurants", header_row=2).get()

    assert table.column_names == ["restaurant_id", "name", "location", "cuisine", "created_at"]
    assert table.num_rows == 6 and str(table.schema.field("created_at").type) == "timestamp[us]"
    assert table.column("name").to_pylist()[:2] == ["Dosa Corner", "Spice Route"]
    from local_data_platform.exceptions import ConfigError

    with pytest.raises(ConfigError, match=r"\(sheet 'Restaurants'\): the header cell B1 is empty"):
        Excel("restaurants", path, sheet="Restaurants").get()  # the title row is not a header


def test_the_generator_cli_writes_every_file(tmp_path, capsys):
    assert generate.main(["--out", str(tmp_path), "--seed", "7"]) == 0

    assert len(list(tmp_path.iterdir())) == 4 + 2 * 9
    assert "Wrote 22 files" in capsys.readouterr().out and (tmp_path / "orders_2026-08-03.csv").is_file()


# --------------------------------------------------------------------------- the configs and the ingest


def test_every_ingest_config_is_valid_and_takes_the_route_the_example_names(workdir, dataset):
    land_batch_one(workdir, dataset)

    routes = {name: plan(workdir / "config" / f"{name}.json") for name in INGEST_CONFIGS}

    assert {name: (result["valid"], result["pipeline"]) for name, result in routes.items()} == {
        "restaurants_to_csv": (True, "ExcelToCSV"), "restaurants": (True, "ExcelToIceberg"),
        "customers": (True, "CSVToIceberg"), "menu_items": (True, "CSVToIceberg"), "reviews": (True, "CSVToIceberg"),
        "orders": (True, "CSVToIceberg"), "order_lines": (True, "CSVToIceberg")}


def test_the_raw_tables_are_loaded_without_dbt_and_every_check_passes(workdir, dataset):
    land_batch_one(workdir, dataset)

    results = {name: ingest(workdir, name) for name in INGEST_CONFIGS}

    assert (workdir / "landing" / "restaurants.csv").is_file()
    assert {name: result.rows_written for name, result in results.items()} == {
        "restaurants_to_csv": 6, "restaurants": 6, "customers": 150, "menu_items": 36, "reviews": 94, "orders": 745,
        "order_lines": 1829}
    assert all(result.quality.passed for result in results.values())
    orders = table_of(workdir, "orders")
    assert [field.transform.__class__.__name__ for field in orders.table().spec().fields] == [
        "DayTransform", "IdentityTransform"]
    assert orders.table().inspect.partitions().num_rows == 534
    schema = {field.name: str(field.type) for field in orders.get().schema}
    assert schema == {"order_id": "string", "restaurant_id": "string", "customer_id": "string",
                      "order_ts": "timestamp[us]", "channel": "string", "total_amount": "double",
                      "updated_at": "timestamp[us]"}


def test_the_raw_orders_keep_everything_they_are_given(workdir, dataset):
    land_batch_one(workdir, dataset)

    ingest(workdir, "orders")
    ingest(workdir, "orders")  # the same batch again: raw is append-only, so it keeps both copies

    assert table_of(workdir, "orders").row_count() == 2 * 745


def test_a_batch_with_an_unknown_channel_is_stopped_by_the_ingest_checks(workdir, dataset):
    land_batch_one(workdir, dataset)
    ingest(workdir, "orders")
    landing = workdir / "landing" / "orders.csv"
    landing.write_text(landing.read_text().replace(",dine_in,", ",walk_in,", 1))

    with pytest.raises(DataQualityError, match="accepted_values"):
        ingest(workdir, "orders")

    assert table_of(workdir, "orders").row_count() == 745  # nothing of the bad batch was written


def test_the_faulty_week_is_stopped_by_the_checks_of_the_orders_config_and_each_failed_check_is_named(workdir, dataset):
    land_batch_one(workdir, dataset)
    ingest(workdir, "orders")
    generate.write_faulty_week(workdir / "landing", dataset)

    with pytest.raises(DataQualityError) as error:
        ingest(workdir, "orders")

    message = str(error.value)
    assert message.startswith("3 of 7 data quality checks failed:")
    assert "accepted_values(restaurant_id): 1 of 31 rows have a restaurant_id outside" in message and "R99" in message
    assert "unique(order_id, updated_at): 1 duplicated key(s)" in message and "O000861" in message
    assert "range(total_amount): 1 of 31 rows violate total_amount >= 0: 1 below min" in message
    assert table_of(workdir, "orders").row_count() == 745  # nothing of the faulty file was written


def test_each_fault_of_the_faulty_week_is_stopped_by_a_check_of_its_own(workdir, dataset):
    land_batch_one(workdir, dataset)
    ingest(workdir, "orders")
    rows = {kind: [dict(order) for order in dataset.week_orders()] for kind in ("amount", "restaurant", "twice")}
    rows["amount"][3]["total_amount"] = -1
    rows["restaurant"][3]["restaurant_id"] = "R99"
    rows["twice"].append(dict(rows["twice"][3]))
    expected = {"amount": "range(total_amount)", "restaurant": "accepted_values(restaurant_id)",
                "twice": "unique(order_id, updated_at)"}

    for kind, orders in rows.items():
        generate._write_csv(workdir / "landing" / "orders.csv", generate.ORDER_COLUMNS, orders,
                            {"order_ts": generate._stamp, "updated_at": generate._stamp,
                             "total_amount": generate._money})
        with pytest.raises(DataQualityError) as error:
            ingest(workdir, "orders")

        assert str(error.value).startswith("1 of 7 data quality checks failed:") and expected[kind] in str(error.value)


def test_the_clean_week_loads_after_the_faulty_one_was_stopped(workdir, dataset):
    land_batch_one(workdir, dataset)
    ingest(workdir, "orders")
    ingest(workdir, "order_lines")
    generate.write_faulty_week(workdir / "landing", dataset)
    with pytest.raises(DataQualityError):
        ingest(workdir, "orders")

    generate.write_week(workdir / "landing", dataset)
    orders, lines = ingest(workdir, "orders"), ingest(workdir, "order_lines")

    assert (orders.rows_written, lines.rows_written) == (30, 82)
    assert table_of(workdir, "orders").row_count() == 775 and table_of(workdir, "order_lines").row_count() == 1911


def test_the_daily_workflow_and_the_batch_load_hold_the_orders_to_the_same_seven_checks():
    batch = json.loads((EXAMPLE / "config" / "orders.json").read_text())["metadata"]["quality"]
    daily = json.loads((EXAMPLE / "workflows" / "orders.json").read_text())["metadata"]["quality"]

    assert batch == daily and [item["check"] for item in batch["checks"]] == [
        "row_count", "not_null", "accepted_values", "unique", "accepted_values", "range", "schema"]
    assert batch["checks"][2]["values"] == [row[0] for row in generate.RESTAURANTS]


# --------------------------------------------------------------------------- the work folder and the command line


def test_the_work_folder_gets_the_configs_the_dbt_project_and_the_workflow_files(workdir):
    assert (workdir / run.MARKER).is_file()
    assert sorted(path.stem for path in (workdir / "config").glob("*.json")) == sorted([*INGEST_CONFIGS, "metrics"])
    assert (workdir / "dbt" / "ldp_dbt.json").is_file() and (workdir / "dbt" / "models" / "schema.yml").is_file()
    assert not (workdir / "dbt" / "target").exists()
    assert (workdir / "workflows" / "daily_orders.json").is_file()


def test_a_work_folder_can_leave_out_the_metrics_file(tmp_path):
    work = run.prepare_workdir(tmp_path / "work", keep_metrics=False)

    assert sorted(path.stem for path in (work / "config").glob("*.json")) == sorted(INGEST_CONFIGS)
    assert (run.CONFIG_DIR / "metrics.json").is_file(), "the example itself keeps it"


def test_running_again_clears_what_the_example_made_and_nothing_else(tmp_path):
    first = run.prepare_workdir(tmp_path / "work")
    (first / "warehouse").mkdir()
    (first / "warehouse" / "old.db").write_text("old")
    (first / "dbt" / "models" / "mine.sql").write_text("select 1")
    (first / "notes.txt").write_text("keep me")

    again = run.prepare_workdir(first)

    assert not (again / "warehouse").exists() and not (again / "dbt" / "models" / "mine.sql").exists()
    assert (again / "notes.txt").read_text() == "keep me"


def test_a_folder_the_example_did_not_make_is_refused(tmp_path):
    (tmp_path / "mine.txt").write_text("hello")

    with pytest.raises(ValueError, match="is not empty and was not created by this example"):
        run.prepare_workdir(tmp_path)

    assert [path.name for path in tmp_path.iterdir()] == ["mine.txt"]


def test_a_folder_that_cannot_be_cleared_stops_the_run_and_names_it(tmp_path):
    first = run.prepare_workdir(tmp_path / "work")
    stuck = first / "warehouse" / "locked"
    stuck.mkdir(parents=True)
    (stuck / "old.parquet").write_text("old")
    stuck.chmod(0o500)  # the files in it cannot be removed
    try:
        if os.access(stuck, os.W_OK):
            pytest.skip("this user can remove files from a read-only folder")
        with pytest.raises(ValueError, match=r"could not clear .*warehouse \(.*Permission denied.*\); remove it, "
                                             r"or pass another --workdir"):
            run.prepare_workdir(first)
    finally:
        stuck.chmod(0o700)

    assert (stuck / "old.parquet").read_text() == "old"  # nothing was run on top of it


def test_a_failing_ldp_step_ends_the_example_with_a_message_and_exit_1(tmp_path, monkeypatch, capsys):
    def failing_ldp(argv):
        print("ldp: error: no such table", file=sys.stderr)
        return 2

    monkeypatch.setattr(run, "require_extras", lambda: None)
    monkeypatch.setattr(run, "ldp_main", failing_ldp)
    monkeypatch.setattr(run, "run_warehouse", lambda workdir, dataset, out: run.ldp(out, "query", "config", "SELECT 1"))

    assert run.main(["--workdir", str(tmp_path / "work"), "--quiet"]) == 1

    assert capsys.readouterr().err == "error: ldp query config failed: ldp: error: no such table\n"


def test_an_unknown_layer_is_refused(tmp_path):
    with pytest.raises(ValueError, match=r"unknown layer 'bronze'; the layers are "
                                         r"\['warehouse', 'orchestration', 'gold'\]"):
        run.run_restaurant(tmp_path / "work", layer="bronze", quiet=True)
    with pytest.raises(SystemExit):
        run.main(["--layer", "bronze"])


@pytest.mark.parametrize("layer", ["warehouse", "gold"])
def test_without_dbt_the_example_says_which_extra_to_install_and_exits_with_1(tmp_path, monkeypatch, capsys, layer):
    from local_data_platform.dbt_layer import project

    def missing(*args, **kwargs):
        raise EngineNotFound("dbt was not found")

    monkeypatch.setattr(project, "find_dbt", missing)

    assert run.main(["--workdir", str(tmp_path / "work"), "--layer", layer]) == 1

    assert capsys.readouterr().err == ('error: The restaurant example needs the dbt extra. Install with: '
                                       'pip install "local-data-platform[dbt,excel]"\n')
    assert not (tmp_path / "work").exists()  # nothing was created before the check


def test_without_the_excel_reader_it_names_both_extras_when_both_are_missing(monkeypatch):
    from local_data_platform.dbt_layer import project

    monkeypatch.setattr(project, "find_dbt", lambda *args, **kwargs: (_ for _ in ()).throw(EngineNotFound("no dbt")))
    monkeypatch.setattr(run.importlib.util, "find_spec", lambda name: None)

    with pytest.raises(EngineNotFound, match="needs the dbt and excel extras"):
        run.require_extras()


# --------------------------------------------------------------------------- the dbt project, read without running it


DBT = EXAMPLE / "dbt"


def model_sql(name: str) -> str:
    return next(DBT.glob(f"models/**/{name}.sql")).read_text()


def test_the_dbt_project_is_a_valid_ldp_project_that_publishes_next_to_the_ingest_configs():
    project = DbtProject.load(DBT)

    assert project.source_configs == ((DBT.parent / "config").resolve(),)
    assert project.config_dir == (DBT.parent / "config").resolve()
    assert project.publish_catalog == {"identifier": "gold", "warehouse_path": "../warehouse"}
    assert project.observability == {"sinks": ["jsonl", "iceberg"]}, "the catalog reads the runs and the gates from it"


GOLD_MODELS = ["churn_by_month", "gold_daily_sales", "gold_menu_item_weekly", "gold_restaurant_scorecard"]


def test_the_models_are_the_ones_the_roadmap_names():
    assert sorted(path.stem for path in DBT.glob("models/**/*.sql")) == sorted([
        *GOLD_MODELS, "customer_activity_monthly", "fct_orders", "stg_customers", "stg_menu_items", "stg_order_lines",
        "stg_orders", "stg_restaurants", "stg_reviews"])
    assert not list(DBT.glob("models/**/revenue_by_restaurant_day.sql")), "gold_daily_sales replaces it"


@pytest.mark.parametrize("model, namespace", [("fct_orders", "silver"), *((model, "gold") for model in GOLD_MODELS)])
def test_the_marked_models_publish_to_their_namespace(model, namespace):
    sql = model_sql(model)

    assert "'ldp_publish': true" in sql and f"'ldp_namespace': '{namespace}'" in sql


@pytest.mark.parametrize("model", GOLD_MODELS)
def test_the_gold_models_name_their_owner_and_their_grain(model):
    sql = model_sql(model)

    assert "'owner':" in sql and "'grain':" in sql


@pytest.mark.parametrize("model", GOLD_MODELS)
def test_every_gold_model_carries_a_gate_of_checks_that_run_before_it_is_written(model):
    """The gate is the ``ldp_quality`` of the model, which ``ldp gold build`` runs again on the table it made."""
    sql = model_sql(model)
    gate = re.search(r"'ldp_quality': \{(.*?)\n        \}", sql, flags=re.DOTALL)

    assert gate and "'on_failure': 'fail'" in gate.group(1)
    kinds = re.findall(r"'check': '(\w+)'", gate.group(1))
    assert kinds[0] == "row_count" and "not_null" in kinds and "unique" in kinds and "range" in kinds


@pytest.mark.parametrize("model", ["stg_restaurants", "stg_customers", "stg_orders", "customer_activity_monthly"])
def test_the_other_models_are_not_published(model):
    assert "ldp_publish" not in model_sql(model)


def test_fct_orders_compares_each_order_with_its_own_row_and_keeps_the_newest_version():
    sql = " ".join(model_sql("fct_orders").split())

    assert "materialized='incremental'" in sql and "unique_key='order_id'" in sql
    assert "incremental_strategy='delete+insert'" in sql
    assert ("where not exists ( select 1 from {{ this }} as loaded where loaded.order_id = incoming.order_id "
            "and loaded.updated_at >= incoming.updated_at )") in sql
    assert "qualify rank() over (partition by order_id order by updated_at desc) = 1" in sql
    assert "max(" not in sql  # a load on the newest value in the table would skip the late orders


def test_the_churn_model_and_its_description_say_what_churn_is():
    yaml = pytest.importorskip("yaml")
    schema = yaml.safe_load((DBT / "models" / "schema.yml").read_text())
    description = next(model["description"] for model in schema["models"] if model["name"] == "churn_by_month")

    for words in ("customers_at_start is the number of customers with an order in month M minus 1",
                  "churned is the number of those customers with no order in month M",
                  "churn_rate is churned divided by customers_at_start, or null when customers_at_start is 0",
                  "active_customers is the number of customers with an order in month M",
                  "The first month has no row"):
        assert words in " ".join(description.split())
    sql = model_sql("churn_by_month")
    assert "month a customer stops ordering" in sql and "when customers_at_start = 0 then null" in sql


def metrics_spec() -> dict:
    return json.loads((EXAMPLE / "config" / "metrics.json").read_text())


def semantic_models(spec: dict) -> dict[str, dict]:
    return {item["name"]: item for item in spec["semantic_models"]}


def test_the_metrics_file_defines_the_six_metrics_over_the_three_gold_tables_that_they_read():
    spec = metrics_spec()

    assert [str(error) for error in validate_spec(spec)] == []
    assert {name: item["table"] for name, item in semantic_models(spec).items()} == {
        "daily_sales": "gold.gold_daily_sales", "restaurant_scorecard": "gold.gold_restaurant_scorecard",
        "customer_churn": "gold.churn_by_month"}
    assert [(item["name"], item["format"]) for item in spec["metrics"]] == [
        ("revenue", "currency"), ("orders", "number"), ("avg_ticket", "currency"), ("avg_rating", "number"),
        ("churn_rate", "percent"), ("active_customers", "number")]
    assert all(item["description"] for item in spec["metrics"])


def test_the_sales_metrics_add_up_with_sums_and_the_average_ticket_is_revenue_over_orders():
    spec = metrics_spec()
    daily = semantic_models(spec)["daily_sales"]
    measures = {item["name"]: (item["agg"], item["expr"]) for item in daily["measures"]}
    metrics = {item["name"]: item for item in spec["metrics"]}

    assert daily["time_dimension"] == {"column": "day", "grains": ["day", "week", "month", "quarter", "year"]}
    assert [item["column"] if isinstance(item, dict) and "column" in item else item["name"]
            for item in daily["dimensions"]] == ["restaurant_name", "city", "cuisine"]
    assert measures == {"revenue_sum": ("sum", "revenue"), "order_sum": ("sum", "orders")}
    assert (metrics["revenue"]["measure"], metrics["orders"]["measure"]) == ("revenue_sum", "order_sum")
    ticket = metrics["avg_ticket"]
    assert (ticket["type"], ticket["numerator"], ticket["denominator"]) == ("ratio", "revenue_sum", "order_sum")


def test_the_rating_is_the_stars_of_all_the_reviews_over_their_number_and_not_the_average_of_averages():
    spec = metrics_spec()
    scorecard = semantic_models(spec)["restaurant_scorecard"]
    measures = {item["name"]: (item["agg"], item["expr"]) for item in scorecard["measures"]}
    rating = next(item for item in spec["metrics"] if item["name"] == "avg_rating")

    assert "time_dimension" not in scorecard, "the scorecard is as of one day, so it has no grain"
    assert (measures[rating["numerator"]], measures[rating["denominator"]]) == (("sum", "rating_sum"),
                                                                                ("sum", "review_count"))


def test_every_column_that_the_metrics_read_is_a_column_of_the_model_that_makes_the_table():
    """A column that a model drops or renames is found here in every job, and not only in the dbt job."""
    yaml = pytest.importorskip("yaml")
    columns = {model["name"]: {column["name"] for column in model["columns"]}
               for model in yaml.safe_load((DBT / "models" / "schema.yml").read_text())["models"]}

    for semantic in metrics_spec()["semantic_models"]:
        read = {semantic["time_dimension"]["column"]} if "time_dimension" in semantic else set()
        read |= {item.get("column", item["name"]) if isinstance(item, dict) else item
                 for item in semantic.get("dimensions", [])}
        read |= {measure["expr"] for measure in semantic["measures"]}
        model = semantic["table"].split(".", 1)[1]

        assert read <= columns[model], (semantic["name"], sorted(read - columns[model]))


def test_the_churn_metrics_are_defined_over_the_churn_table_and_only_by_month():
    spec = metrics_spec()
    churn = semantic_models(spec)["customer_churn"]

    assert churn["table"] == "gold.churn_by_month"
    assert churn["time_dimension"] == {"column": "month", "grains": ["month"]}, "a sum over months is not a churn"
    measures = {item["name"]: (item["agg"], item["expr"]) for item in churn["measures"]}
    metrics = {item["name"]: item for item in spec["metrics"]}
    rate = metrics["churn_rate"]
    assert (rate["type"], rate["format"]) == ("ratio", "percent")
    assert (measures[rate["numerator"]], measures[rate["denominator"]]) == (("sum", "churned"),
                                                                            ("sum", "customers_at_start"))
    active = metrics["active_customers"]
    assert (active["type"], measures[active["measure"]]) == ("simple", ("sum", "active_customers"))


def test_the_schema_tests_cover_every_key_the_relationships_and_the_channels():
    yaml = pytest.importorskip("yaml")
    models = {model["name"]: model for model in yaml.safe_load((DBT / "models" / "schema.yml").read_text())["models"]}

    def tests(model: str, column: str) -> list:
        entry = next(item for item in models[model]["columns"] if item["name"] == column)
        return [next(iter(test)) if isinstance(test, dict) else test for test in entry.get("data_tests", [])]

    for model, key in (("stg_restaurants", "restaurant_id"), ("stg_customers", "customer_id"),
                       ("stg_menu_items", "menu_item_id"), ("stg_order_lines", "order_line_id"),
                       ("stg_reviews", "review_id"), ("fct_orders", "order_id"),
                       ("customer_activity_monthly", "customer_month_id"), ("churn_by_month", "month"),
                       ("gold_daily_sales", "restaurant_day_id"), ("gold_menu_item_weekly", "menu_item_week_id"),
                       ("gold_restaurant_scorecard", "restaurant_id")):
        assert {"unique", "not_null"} <= set(tests(model, key)), (model, key)
    for model in ("stg_orders", "fct_orders"):
        assert "relationships" in tests(model, "customer_id") and "relationships" in tests(model, "restaurant_id")
        assert "accepted_values" in tests(model, "channel")
    assert "unique" not in tests("stg_orders", "order_id")  # the raw orders keep every version of an order


def test_a_singular_test_checks_that_the_churn_rate_is_a_share():
    sql = (DBT / "tests" / "churn_rate_between_0_and_1.sql").read_text()

    assert "ref('churn_by_month')" in sql and "churn_rate < 0 or churn_rate > 1" in sql


# --------------------------------------------------------------------------- the Makefile


def test_the_makefile_has_the_warehouse_demo_and_demo_all_runs_it_only_when_it_can():
    makefile = (REPO_ROOT / "Makefile").read_text()

    assert "demo-warehouse: $(DEMO_CONFIG)" in makefile
    assert "examples/restaurant/run.py --workdir $(WAREHOUSE_DIR) --layer warehouse" in makefile
    assert "WAREHOUSE_DIR ?= $(DEMO_DIR)/restaurant" in makefile
    assert "demo-warehouse Run the restaurant example's warehouse layer" in makefile
    assert 'import dbt.cli.main, dbt.adapters.duckdb, openpyxl' in makefile and "Skipping demo-warehouse" in makefile


def test_the_makefile_has_the_gold_demo_and_demo_all_runs_it_only_when_it_can():
    makefile = (REPO_ROOT / "Makefile").read_text()
    demo_all = makefile.split("\ndemo-all:", 1)[1].split("\ncheck-scala-cli:", 1)[0]

    assert "demo-gold: $(DEMO_CONFIG)" in makefile
    assert "examples/restaurant/run.py --workdir $(WAREHOUSE_DIR) --layer gold" in makefile
    assert "demo-gold      Run the restaurant example's gold layer" in makefile
    assert "demo-gold" in makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "$(MAKE) demo-warehouse demo-gold;" in demo_all and "Skipping demo-warehouse and demo-gold" in demo_all


def test_the_makefile_has_the_orchestration_demo_and_demo_all_runs_it_with_or_without_dbt():
    makefile = (REPO_ROOT / "Makefile").read_text()
    demo_all = makefile.split("\ndemo-all:", 1)[1].split("\ncheck-scala-cli:", 1)[0]

    assert "demo-orchestration: $(DEMO_CONFIG)" in makefile
    assert "examples/restaurant/run.py --workdir $(WAREHOUSE_DIR) --layer orchestration" in makefile
    assert "demo-orchestration Run the restaurant example's orchestration layer" in makefile
    assert "demo-orchestration" in makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "\t$(MAKE) demo-orchestration\n" in demo_all, "it runs whether or not dbt is installed"
