"""The demo: deterministic data, the numbers each step reports, and safe re-runs."""

import io
import json

import pyarrow as pa
import pytest

from local_data_platform import Config
from local_data_platform.demo import CITIES, MARKER, _day_partitioning_available, generate_rides, run_demo
from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.format.iceberg import Iceberg

ROWS = 200
VOLATILE = {"duration_s", "workdir", "config_path"}


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """Run the demo once with 200 rows; return ``(numbers, narrative, workdir)``."""
    workdir = tmp_path_factory.mktemp("demo") / "ldp_demo"
    out = io.StringIO()
    numbers = run_demo(workdir, rows=ROWS, out=out)
    return numbers, out.getvalue(), workdir


def test_append_twice_doubles_the_rows_and_overwrite_is_idempotent(demo):
    numbers, _, _ = demo
    assert numbers["append_rows"] == [ROWS, 2 * ROWS]
    assert numbers["duplicate_ride_ids"] == ROWS
    assert numbers["overwrite_rows"] == [ROWS, ROWS]


def test_upsert_updates_changed_rides_and_inserts_new_ones(demo):
    numbers, _, _ = demo
    assert numbers["pipeline"] == "CSVToIceberg"
    assert numbers["initial_rows"] == ROWS
    assert numbers["upsert_updated"] == 50
    assert numbers["upsert_inserted"] == 10
    assert numbers["upsert_rows_before"] == ROWS
    assert numbers["upsert_rows_after"] == ROWS + 10
    assert numbers["upsert_rerun_written"] == 0


def test_the_partitioned_table_has_one_partition_per_day(demo):
    numbers, _, _ = demo
    expected_spec = "day(pickup_ts)" if _day_partitioning_available() else "identity(pickup_date)"
    assert numbers["partition_spec"] == expected_spec
    assert numbers["partitions"] == 7
    assert numbers["partition_files_total"] == 7
    assert numbers["partition_files_scanned"] == 1


def test_the_clean_batch_passes_and_the_bad_batch_is_blocked(demo):
    numbers, _, _ = demo
    assert numbers["quality_passed"] is True
    assert numbers["quality_checks_run"] == 7
    assert numbers["bad_batch_blocked"] is True
    assert numbers["bad_batch_failed_checks"] == ["not_null(ride_id, pickup_ts, fare)", "unique(ride_id)",
                                                  "range(fare)"]
    assert numbers["rows_before_bad_batch"] == numbers["rows_after_bad_batch"] == ROWS + 10


def test_duckdb_sums_every_ride(demo):
    numbers, _, workdir = demo
    assert numbers["sql_total_rides"] == ROWS + 10
    assert numbers["sql_rows"] == 7 * len(CITIES)  # every day has every city at 200 rides with seed 42
    rides = Iceberg("rides", {"identifier": "demo", "warehouse_path": "warehouse"}, base_dir=workdir).get()
    assert numbers["sql_total_revenue"] == pytest.approx(sum(rides["fare"].to_pylist()), abs=0.05)


def test_time_travel_reads_the_first_snapshot(demo):
    numbers, _, _ = demo
    assert numbers["first_snapshot_rows"] == ROWS
    assert numbers["current_rows"] == ROWS + 10
    # The initial load, then the upsert's overwrite and two appends. The re-run and the blocked batch add none.
    assert numbers["snapshot_count"] == 4


def test_the_export_matches_the_table(demo):
    numbers, _, workdir = demo
    assert numbers["export_rows"] == ROWS + 10
    assert numbers["export_verified"] is True
    assert (workdir / "exports" / "rides.csv").is_file()


def test_the_config_is_written_as_json_and_loads(demo):
    numbers, _, workdir = demo
    config = Config.from_json(numbers["config_path"])
    assert config.base_dir == workdir
    assert config.target["write_mode"] == "upsert"
    assert len(config.quality["checks"]) == 7
    assert json.loads((workdir / "rides.json").read_text())["identifier"] == "demo_rides"


def test_the_narrative_walks_through_every_step(demo):
    numbers, narrative, _ = demo
    for step in range(1, 9):
        assert f"[{step}/8]" in narrative
    assert "Summary" in narrative
    assert "blocked" in narrative
    assert numbers["duration_s"] < 60


def test_the_demo_writes_only_inside_its_workdir(demo):
    _, _, workdir = demo
    assert sorted(path.name for path in workdir.iterdir()) == sorted(
        [MARKER, "data", "exports", "rides.json", "warehouse"])
    assert sorted(path.name for path in (workdir / "data").iterdir()) == [
        "rides.csv", "rides.parquet", "rides_bad.parquet", "rides_changes.parquet"]


def test_a_rerun_in_the_same_workdir_gives_the_same_numbers(tmp_path):
    workdir = tmp_path / "demo"
    first = run_demo(workdir, rows=40, out=io.StringIO())
    (workdir / "notes.txt").write_text("mine")
    second = run_demo(workdir, rows=40, out=io.StringIO())

    assert {k: v for k, v in first.items() if k not in VOLATILE} == \
        {k: v for k, v in second.items() if k not in VOLATILE}
    assert (workdir / "notes.txt").read_text() == "mine", "the demo deleted a file it did not create"


def test_the_demo_refuses_a_non_empty_folder_it_did_not_create(tmp_path):
    (tmp_path / "project.txt").write_text("keep me")
    with pytest.raises(ConfigError, match="not created by ldp demo"):
        run_demo(tmp_path, rows=40, out=io.StringIO())
    assert [path.name for path in tmp_path.iterdir()] == ["project.txt"]


def test_the_demo_refuses_a_file_as_workdir(tmp_path):
    path = tmp_path / "file"
    path.write_text("")
    with pytest.raises(ConfigError, match="not a folder"):
        run_demo(path, rows=40, out=io.StringIO())


def test_the_demo_uses_an_existing_empty_folder(tmp_path):
    numbers = run_demo(tmp_path, rows=40, out=io.StringIO())
    assert numbers["export_verified"] is True
    assert (tmp_path / MARKER).is_file()


@pytest.mark.parametrize("rows", [0, 19, "200"])
def test_the_demo_needs_at_least_twenty_rows(tmp_path, rows):
    with pytest.raises(ValueError, match="at least 20"):
        run_demo(tmp_path / "demo", rows=rows, out=io.StringIO())
    assert not (tmp_path / "demo").exists()


def test_the_demo_skips_sql_without_duckdb(tmp_path, monkeypatch):
    import local_data_platform.engine.duckdb as duckdb_engine

    def missing():
        raise EngineNotFound(f"The DuckDB engine needs duckdb. Install it with: {duckdb_engine.INSTALL_HINT}")

    monkeypatch.setattr(duckdb_engine, "_import_duckdb", missing)
    out = io.StringIO()
    numbers = run_demo(tmp_path / "demo", rows=40, out=out)
    assert numbers["sql_rows"] is None and numbers["sql_total_rides"] is None
    assert numbers["export_verified"] is True
    assert "Skipped" in out.getvalue()


def test_generated_rides_are_deterministic_and_cover_every_day():
    first, again, other = generate_rides(100, 42), generate_rides(100, 42), generate_rides(100, 43)
    assert first.equals(again)
    assert not first.equals(other)
    assert first.schema == pa.schema([("ride_id", pa.int64()), ("pickup_ts", pa.timestamp("us")),
                                      ("city", pa.string()), ("distance_km", pa.float64()),
                                      ("fare", pa.float64())])
    assert first["ride_id"].to_pylist() == list(range(1, 101))
    assert len({ts.date() for ts in first["pickup_ts"].to_pylist()}) == 7
    assert set(first["city"].to_pylist()) <= set(CITIES)
    assert min(first["fare"].to_pylist()) > 0


def test_generate_rides_start_id():
    assert generate_rides(3, 1, start_id=500)["ride_id"].to_pylist() == [500, 501, 502]
