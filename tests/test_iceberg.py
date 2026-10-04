"""Tests for the Iceberg format: write modes, partitioning, timestamps, schema evolution and time travel."""

import dataclasses
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from pyiceberg.exceptions import NotInstalledError
from pyiceberg.table import Table as PyIcebergTable
from pyiceberg.types import LongType, StringType, TimestampType, TimestamptzType

import local_data_platform.format.iceberg as iceberg_module
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.format.iceberg import (
    Iceberg,
    WriteResult,
    _row_count,
    cast_timestamps_to_us,
    parse_partition_by,
    parse_transform,
)

DOWNCAST_ENV = "PYICEBERG_DOWNCAST_NS_TIMESTAMP_TO_US_ON_WRITE"
SOURCE_TYPES = {"city": StringType(), "ride_id": LongType(), "pickup_ts": TimestampType()}


@pytest.fixture(autouse=True)
def _no_downcast_env(monkeypatch):
    """Prove the explicit cast works: pyiceberg's downcast switch must not be set."""
    monkeypatch.delenv(DOWNCAST_ENV, raising=False)


def _ids(df: pa.Table) -> list[int]:
    return sorted(df.column("ride_id").to_pylist())


def _fares(df: pa.Table) -> dict[int, float]:
    return dict(zip(df.column("ride_id").to_pylist(), df.column("fare").to_pylist()))


def _can_write(transform_text: str, column: str) -> bool:
    """Whether this pyiceberg can compute the transform on write (some need pyiceberg-core)."""
    try:
        parse_transform(transform_text).pyarrow_transform(SOURCE_TYPES[column])
    except (NotInstalledError, ModuleNotFoundError):
        return False
    return True


# --------------------------------------------------------------------------- construction


def test_identifier_namespace_and_warehouse(catalog_config):
    table = Iceberg("rides", catalog_config)

    assert table.identifier == "test_ns.rides"
    assert table.namespace == table.catalog_identifier == "test_ns"
    assert table.format == "ICEBERG"
    assert table.path == Path(catalog_config["warehouse_path"])
    assert table.path.is_dir()
    assert isinstance(table.catalog, LocalIcebergCatalog)
    assert ("test_ns",) in table.catalog.get_dbs()
    assert table.exists() is False
    assert "test_ns.rides" in repr(table)


def test_namespace_uses_public_create_namespace_if_not_exists(catalog_config, monkeypatch):
    calls = []
    original = LocalIcebergCatalog.create_namespace_if_not_exists

    def spy(self, namespace, *args, **kwargs):
        calls.append(namespace)
        return original(self, namespace, *args, **kwargs)

    monkeypatch.setattr(LocalIcebergCatalog, "create_namespace_if_not_exists", spy)

    Iceberg("rides", catalog_config)
    Iceberg("rides", catalog_config)

    assert calls == ["test_ns", "test_ns"]
    # pyiceberg itself may use its private helpers; our module must not.
    source = Path(iceberg_module.__file__).read_text()
    assert "_namespace_exists" not in source
    assert "os.environ" not in source


def test_legacy_catalog_kwarg_is_an_alias(catalog_config, sample_table):
    legacy = Iceberg(name="rides", catalog=catalog_config)
    legacy.put(sample_table)

    current = Iceberg(name="rides", config=catalog_config)

    assert legacy.identifier == current.identifier
    assert current.row_count() == sample_table.num_rows


def test_same_config_and_catalog_is_accepted(catalog_config):
    assert Iceberg("rides", catalog_config, catalog=dict(catalog_config)).identifier == "test_ns.rides"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "needs a catalog config"),
        ({"config": {"identifier": "a", "warehouse_path": "x"}, "catalog": {"identifier": "b", "warehouse_path": "y"}},
         "not both"),
        ({"config": "warehouse"}, "must be an object"),
        ({"config": {"warehouse_path": "x"}}, "identifier"),
        ({"config": {"identifier": "ns"}}, "warehouse_path"),
        # 0.2.0 (C1): REST is a supported type, so a REST block without its 'uri' is the error now;
        # 0.1.1 rejected every non-local type with "not supported".
        ({"config": {"identifier": "ns", "warehouse_path": "x", "type": "REST"}}, "uri"),
        ({"config": {"identifier": "ns", "warehouse_path": "x", "type": "hive"}}, "catalog type"),
    ],
)
def test_bad_catalog_config_is_a_config_error(tmp_path, kwargs, message):
    with pytest.raises(ConfigError, match=message):
        Iceberg("rides", base_dir=tmp_path, **kwargs)


def test_local_catalog_type_is_accepted(catalog_config):
    assert Iceberg("rides", {**catalog_config, "type": "LocalIceberg"}).exists() is False


@pytest.mark.parametrize("name", ["", "a.b"])
def test_bad_table_name_is_a_config_error(catalog_config, name):
    with pytest.raises(ConfigError):
        Iceberg(name, catalog_config)


def test_legacy_path_and_format_options_are_ignored(catalog_config):
    table = Iceberg("rides", catalog_config, path="old.parquet", format="ICEBERG")

    assert table.format == "ICEBERG"
    assert table.path.name == "warehouse"


def test_relative_warehouse_path_resolves_against_base_dir(tmp_path):
    base = tmp_path / "config_dir"

    table = Iceberg("rides", {"identifier": "ns", "warehouse_path": "warehouse"}, base_dir=base)

    assert table.path == (base / "warehouse").resolve()
    assert (base / "warehouse" / "ns_catalog.db").is_file()


def test_relative_warehouse_path_without_base_dir_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    table = Iceberg("rides", {"identifier": "ns", "warehouse_path": "warehouse"})

    assert table.path == (tmp_path / "warehouse").resolve()


def test_absolute_warehouse_path_is_kept(tmp_path, sample_table):
    warehouse = tmp_path / "abs" / "warehouse"

    table = Iceberg("rides", {"identifier": "ns", "warehouse_path": str(warehouse)}, base_dir=tmp_path / "other")
    table.put(sample_table)

    assert table.path == warehouse
    assert list(warehouse.rglob("*.parquet"))
    assert not (tmp_path / "other").exists()


def test_legacy_leading_slash_warehouse_path_warns(tmp_path):
    name = f"ldp_legacy_wh_{uuid.uuid4().hex}"
    (tmp_path / name).mkdir()
    assert not os.path.exists(f"/{name}")

    with pytest.warns(DeprecationWarning, match="leading slash"):
        table = Iceberg("rides", {"identifier": "ns", "warehouse_path": f"/{name}"}, base_dir=tmp_path)

    assert table.path == (tmp_path / name).resolve()


# --------------------------------------------------------------------------- write modes


def test_write_result_fields_match_contract():
    # 0.2.0 (C3) adds branch, idempotency_key, attempts and skipped_duplicate, all defaulted, so
    # WriteResult(...) with the six 0.1.1 fields still builds the same value.
    assert [f.name for f in dataclasses.fields(WriteResult)] == [
        "table_identifier", "mode", "rows_written", "rows_before", "rows_after", "snapshot_id",
        "branch", "idempotency_key", "attempts", "skipped_duplicate",
    ]


def test_append_twice_doubles_rows(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)

    first = table.put(sample_table)
    second = table.put(sample_table)

    assert first == WriteResult("test_ns.rides", "append", 6, 0, 6, first.snapshot_id)
    assert (second.mode, second.rows_written, second.rows_before, second.rows_after) == ("append", 6, 6, 12)
    assert second.snapshot_id != first.snapshot_id
    assert table.row_count() == 12
    assert table.get().num_rows == 12


def test_rerunning_overwrite_keeps_row_count_stable(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode="overwrite")

    results = [table.put(sample_table) for _ in range(3)]

    assert [r.rows_after for r in results] == [6, 6, 6]
    assert [r.rows_before for r in results] == [0, 6, 6]
    assert all(r.mode == "overwrite" and r.rows_written == 6 for r in results)
    assert len({r.snapshot_id for r in results}) == 3
    assert table.row_count() == 6
    assert _ids(table.get()) == _ids(sample_table)


def test_overwrite_on_new_table_is_a_single_append(catalog_config, sample_table, recwarn):
    table = Iceberg("rides", catalog_config, write_mode="overwrite")

    result = table.put(sample_table)

    assert (result.mode, result.rows_before, result.rows_after) == ("overwrite", 0, 6)
    assert [s["operation"] for s in table.snapshots()] == ["append"]
    assert not [w for w in recwarn if "Delete operation" in str(w.message)]


def test_overwrite_replaces_all_current_rows(catalog_config, make_table):
    table = Iceberg("rides", catalog_config)
    table.put(make_table(n=6))
    table.put(make_table(n=6))

    result = table.put(make_table(n=2, start_id=100), mode="overwrite")

    assert (result.rows_before, result.rows_after) == (12, 2)
    assert _ids(table.get()) == [100, 101]


def test_mode_argument_overrides_default_and_is_case_insensitive(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode="OVERWRITE")
    table.put(sample_table)

    assert table.put(sample_table, mode="Append").rows_after == 12
    assert table.put(sample_table).rows_after == 6


def test_write_mode_none_means_append(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode=None)

    assert table.write_mode == "append"
    assert table.put(sample_table).mode == "append"


def test_unknown_write_mode_is_a_config_error(catalog_config, sample_table):
    with pytest.raises(ConfigError, match="unknown write mode"):
        Iceberg("rides", catalog_config, write_mode="merge")
    table = Iceberg("rides", catalog_config)
    with pytest.raises(ConfigError, match="unknown write mode"):
        table.put(sample_table, mode="replace")
    assert not table.exists()


def test_upsert_updates_changed_rows_and_inserts_new_ones(catalog_config, make_table):
    table = Iceberg("rides", catalog_config, write_mode="upsert", join_cols=["ride_id"])
    original = make_table(n=6, start_id=1)
    table.put(original)

    changes = make_table(n=6, start_id=4, fare_offset=100.0)
    result = table.put(changes)

    assert result.mode == "upsert"
    assert (result.rows_before, result.rows_after, result.rows_written) == (6, 9, 6)
    stored = table.get()
    assert _ids(stored) == list(range(1, 10))
    fares = _fares(stored)
    assert {i: fares[i] for i in (1, 2, 3)} == {i: _fares(original)[i] for i in (1, 2, 3)}
    assert {i: fares[i] for i in range(4, 10)} == _fares(changes)


def test_upsert_does_not_rewrite_unchanged_rows(catalog_config, make_table):
    table = Iceberg("rides", catalog_config, join_cols="ride_id")
    table.put(make_table(n=6))

    unchanged_plus_new = pa.concat_tables([make_table(n=3, start_id=1), make_table(n=1, start_id=50)])
    result = table.put(unchanged_plus_new, mode="upsert")

    assert (result.rows_written, result.rows_before, result.rows_after) == (1, 6, 7)


def test_upsert_with_composite_key(catalog_config, make_table):
    table = Iceberg("rides", catalog_config, join_cols=["ride_id", "city"], write_mode="upsert")
    table.put(make_table(n=3))

    moved = make_table(n=1, start_id=1).set_column(1, "city", pa.array(["ZZZ"]))
    result = table.put(moved)

    assert result.rows_after == 4
    assert sorted(table.get().column("city").to_pylist()).count("ZZZ") == 1


def test_upsert_on_new_table_acts_as_append(catalog_config, sample_table, monkeypatch):
    def no_upsert(*args, **kwargs):
        raise AssertionError("upsert on a new table must append")

    monkeypatch.setattr(PyIcebergTable, "upsert", no_upsert)
    table = Iceberg("rides", catalog_config, write_mode="upsert", join_cols=["ride_id"])

    result = table.put(sample_table)

    assert (result.mode, result.rows_written, result.rows_before, result.rows_after) == ("upsert", 6, 0, 6)
    assert [s["operation"] for s in table.snapshots()] == ["append"]


def test_upsert_on_empty_existing_table_acts_as_append(catalog_config, sample_table, monkeypatch):
    table = Iceberg("rides", catalog_config, write_mode="upsert", join_cols=["ride_id"])
    table.catalog.create_table(table.identifier, schema=cast_timestamps_to_us(sample_table).schema)
    assert table.exists() and table.row_count() == 0

    def no_upsert(*args, **kwargs):
        raise AssertionError("upsert on an empty table must append")

    monkeypatch.setattr(PyIcebergTable, "upsert", no_upsert)
    result = table.put(sample_table)

    assert (result.rows_before, result.rows_after, result.rows_written) == (0, 6, 6)


def test_upsert_without_join_cols_is_a_config_error(catalog_config, sample_table):
    with pytest.raises(ConfigError, match="join_cols"):
        Iceberg("rides", catalog_config, write_mode="upsert")

    table = Iceberg("rides", catalog_config)
    with pytest.raises(ConfigError, match="join_cols"):
        table.put(sample_table, mode="upsert")
    assert not table.exists()


def test_upsert_join_col_missing_from_data_is_a_config_error(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, join_cols=["trip_id"])

    with pytest.raises(ConfigError, match="trip_id"):
        table.put(sample_table, mode="upsert")
    assert not table.exists()


def test_upsert_batch_missing_a_table_column_is_a_config_error_and_writes_nothing(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode="upsert", join_cols=["ride_id"])
    table.put(sample_table)
    before = [s["snapshot_id"] for s in table.snapshots()]
    partial = sample_table.drop_columns(["city"])

    with pytest.raises(ConfigError, match=r"missing columns \['city'\]"):
        table.put(partial)

    assert [s["snapshot_id"] for s in table.snapshots()] == before
    assert table.get().column("city").null_count == 0


def test_upsert_batch_missing_a_column_still_appends_into_an_empty_table(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode="upsert", join_cols=["ride_id"])
    table.catalog.create_table(table.identifier, schema=cast_timestamps_to_us(sample_table).schema)

    result = table.put(sample_table.drop_columns(["city"]))

    assert (result.rows_before, result.rows_after) == (0, 6)
    assert table.get().column("city").null_count == 6


def test_upsert_rejects_duplicate_keys_even_on_first_load(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode="upsert", join_cols=["ride_id"])

    with pytest.raises(ValueError, match="duplicate"):
        table.put(pa.concat_tables([sample_table, sample_table]))
    assert not table.exists()


def test_upsert_rejects_null_keys(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode="upsert", join_cols=["ride_id"])
    with_null = sample_table.set_column(0, "ride_id", pa.array([None, 2, 3, 4, 5, 6], pa.int64()))

    with pytest.raises(ValueError, match="null"):
        table.put(with_null)


@pytest.mark.parametrize("bad", [None, "empty"])
def test_put_none_or_empty_raises_value_error(catalog_config, sample_table, bad):
    table = Iceberg("rides", catalog_config)
    df = None if bad is None else sample_table.slice(0, 0)

    with pytest.raises(ValueError):
        table.put(df)
    assert not table.exists()


def test_put_accepts_record_batch(catalog_config, sample_table):
    assert Iceberg("rides", catalog_config).put(sample_table.to_batches()[0]).rows_after == 6


@pytest.mark.parametrize("join_cols", [5, [""], ["ok", 3]])
def test_bad_join_cols_is_a_config_error(catalog_config, join_cols):
    with pytest.raises(ConfigError, match="join_cols"):
        Iceberg("rides", catalog_config, join_cols=join_cols)


# --------------------------------------------------------------------------- timestamps


def test_ns_timestamps_are_cast_explicitly_without_env_or_properties(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)

    table.put(sample_table)

    stored = table.get()
    assert stored.schema.field("pickup_ts").type == pa.timestamp("us")
    expected = sample_table.column("pickup_ts").cast(pa.timestamp("us"), safe=False)
    assert sorted(stored.column("pickup_ts").to_pylist()) == sorted(expected.to_pylist())
    assert DOWNCAST_ENV not in os.environ
    assert "downcast-ns-timestamp-to-us-on-write" not in table.table().properties
    assert isinstance(table.table().schema().find_field("pickup_ts").field_type, TimestampType)


def test_tz_aware_ns_timestamps_are_stored_as_utc_timestamptz(catalog_config, sample_table_tz):
    table = Iceberg("rides", catalog_config)

    table.put(sample_table_tz)

    stored = table.get()
    assert stored.schema.field("pickup_ts").type == pa.timestamp("us", tz="UTC")
    assert isinstance(table.table().schema().find_field("pickup_ts").field_type, TimestamptzType)
    as_int = pc.cast(stored.column("pickup_ts"), pa.int64()).to_pylist()
    expected = [v // 1000 for v in pc.cast(sample_table_tz.column("pickup_ts"), pa.int64()).to_pylist()]
    assert sorted(as_int) == sorted(expected)


def test_ns_timestamps_can_be_appended_to_an_existing_table(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)

    assert table.put(sample_table).rows_after == 12


def test_cast_timestamps_to_us_handles_nested_types():
    ns = pa.timestamp("ns")
    df = pa.table({
        "s": pa.array([{"t": 1_001, "x": 1}], pa.struct([("t", ns), ("x", pa.int64())])),
        "l": pa.array([[2_002]], pa.list_(ns)),
        "ll": pa.array([[3_003]], pa.large_list(ns)),
        "m": pa.array([[("k", 4_004)]], pa.map_(pa.string(), ns)),
        "tz": pa.array([5_005], pa.timestamp("ns", tz="America/New_York")),
        "ms": pa.array([6], pa.timestamp("ms")),
        "time": pa.array([7_007], pa.time64("ns")),
        "keep": pa.array(["x"]),
    })

    out = cast_timestamps_to_us(df)

    us = pa.timestamp("us")
    assert out.schema.field("s").type == pa.struct([("t", us), ("x", pa.int64())])
    assert out.schema.field("l").type == pa.list_(us)
    assert out.schema.field("ll").type == pa.large_list(us)
    assert out.schema.field("m").type == pa.map_(pa.string(), us)
    assert out.schema.field("tz").type == pa.timestamp("us", tz="UTC")
    assert out.schema.field("ms").type == us
    assert out.schema.field("time").type == pa.time64("us")
    assert out.schema.field("keep").type == pa.string()
    assert out.column("s").to_pylist()[0]["x"] == 1
    assert pc.cast(out.column("tz"), pa.int64()).to_pylist() == [5]
    assert pc.cast(out.column("ms"), pa.int64()).to_pylist() == [6_000]


def test_cast_timestamps_to_us_returns_input_when_nothing_to_cast():
    df = pa.table({"t": pa.array([1], pa.timestamp("us")), "tz": pa.array([1], pa.timestamp("us", tz="UTC"))})

    assert cast_timestamps_to_us(df) is df


def test_importing_the_module_sets_no_environment_variables():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYICEBERG_")}
    code = (
        "import os, local_data_platform.format.iceberg, local_data_platform.store.target.iceberg; "
        "leaked = sorted(k for k in os.environ if k.startswith('PYICEBERG_')); "
        "assert not leaked, leaked"
    )

    subprocess.run([sys.executable, "-c", code], check=True, env=env, timeout=120)


# --------------------------------------------------------------------------- partitioning


@pytest.mark.parametrize(
    "column, transform, expected",
    [
        ("city", "identity", "identity"),
        ("pickup_ts", "year", "year"),
        ("pickup_ts", "month", "month"),
        ("pickup_ts", "day", "day"),
        ("pickup_ts", "hour", "hour"),
        ("ride_id", "bucket[4]", "bucket[4]"),
        ("city", "truncate[2]", "truncate[2]"),
        ("pickup_ts", " DAY ", "day"),
    ],
)
def test_partition_spec_is_present_on_the_created_table(catalog_config, sample_table, column, transform, expected):
    table = Iceberg("rides", catalog_config, partition_by=[{"column": column, "transform": transform}])

    if _can_write(transform, column):
        assert table.put(sample_table).rows_after == 6
    else:
        # Newer pyiceberg computes this transform with the optional pyiceberg-core package. The table
        # (and its spec) is created before the write fails.
        with pytest.raises((NotInstalledError, ModuleNotFoundError)):
            table.put(sample_table)

    iceberg_table = table.table()
    fields = iceberg_table.spec().fields
    assert len(fields) == 1
    assert str(fields[0].transform) == expected
    assert iceberg_table.schema().find_column_name(fields[0].source_id) == column


def test_partition_field_name_is_honoured(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, partition_by=[{"column": "city", "transform": "identity",
                                                            "name": "city_part"}])
    table.put(sample_table)

    assert [f.name for f in table.table().spec().fields] == ["city_part"]


def test_multiple_partition_fields_keep_their_order(catalog_config, sample_table):
    partition_by = [{"column": "city", "transform": "identity"}, {"column": "ride_id", "transform": "identity"}]
    table = Iceberg("rides", catalog_config, partition_by=partition_by)
    table.put(sample_table)

    schema = table.table().schema()
    assert [schema.find_column_name(f.source_id) for f in table.table().spec().fields] == ["city", "ride_id"]


def test_identity_partitioned_writes_split_files_and_stay_idempotent(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, partition_by=[{"column": "city", "transform": "identity"}],
                    write_mode="overwrite")

    table.put(sample_table)
    result = table.put(sample_table)

    assert result.rows_after == 6
    paths = table.table().inspect.files().column("file_path").to_pylist()
    assert sorted(os.path.basename(os.path.dirname(p)) for p in paths) == ["city=BKK", "city=LDN", "city=NYC"]


def test_day_partitioned_write_splits_rows_by_day(catalog_config, sample_table):
    if not _can_write("day", "pickup_ts"):
        pytest.skip("this pyiceberg needs the optional pyiceberg-core package to write day partitions")
    table = Iceberg("rides", catalog_config, partition_by=[{"column": "pickup_ts", "transform": "day"}])

    table.put(sample_table)

    assert table.table().inspect.files().num_rows == 2
    assert table.row_count() == 6


def test_upsert_can_move_a_row_between_identity_partitions(catalog_config, make_table):
    table = Iceberg("rides", catalog_config, partition_by=[{"column": "city", "transform": "identity"}],
                    join_cols=["ride_id"], write_mode="upsert")
    table.put(make_table(n=6))

    moved = make_table(n=1, start_id=1).set_column(1, "city", pa.array(["BKK"]))
    table.put(moved)

    stored = table.get()
    assert stored.num_rows == 6
    assert dict(zip(stored.column("ride_id").to_pylist(), stored.column("city").to_pylist()))[1] == "BKK"


@pytest.mark.parametrize("transform", ["days", "bucket", "bucket[0]", "bucket[x]", "truncate[-1]", "void", 5])
def test_unknown_transform_is_a_config_error(catalog_config, transform):
    with pytest.raises(ConfigError, match="transform"):
        Iceberg("rides", catalog_config, partition_by=[{"column": "city", "transform": transform}])


def test_unknown_partition_column_is_a_config_error_and_creates_nothing(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, partition_by=[{"column": "dropoff_ts", "transform": "day"}])

    with pytest.raises(ConfigError, match="dropoff_ts"):
        table.put(sample_table)
    assert not table.exists()


def test_transform_that_does_not_fit_the_column_type_is_a_config_error(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, partition_by=[{"column": "city", "transform": "day"}])

    with pytest.raises(ConfigError, match="cannot apply"):
        table.put(sample_table)
    assert not table.exists()


def test_conflicting_partition_fields_are_a_config_error(catalog_config, sample_table):
    partition_by = [{"column": "pickup_ts", "transform": "day"}, {"column": "pickup_ts", "transform": "hour"}]
    table = Iceberg("rides", catalog_config, partition_by=partition_by)

    with pytest.raises(ConfigError, match="partition"):
        table.put(sample_table)
    assert not table.exists()


@pytest.mark.parametrize(
    "partition_by, message",
    [
        ({"column": "city", "transform": "identity"}, "must be a list"),
        (["city"], "must be an object"),
        ([{"transform": "identity"}], "column"),
        ([{"column": "city"}], "transform"),
        ([{"column": "city", "transform": "identity", "tranform": "day"}], "unknown keys"),
        ([{"column": "city", "transform": "identity", "name": ""}], "name"),
    ],
)
def test_malformed_partition_by_is_a_config_error(catalog_config, partition_by, message):
    with pytest.raises(ConfigError, match=message):
        Iceberg("rides", catalog_config, partition_by=partition_by)


def test_parse_partition_by_returns_items_in_order():
    items = parse_partition_by([
        {"column": "a", "transform": "bucket[16]"},
        {"column": "b", "transform": "truncate[3]", "name": "b3"},
    ])

    assert [(i.column, str(i.transform), i.name) for i in items] == [
        ("a", "bucket[16]", None),
        ("b", "truncate[3]", "b3"),
    ]
    assert parse_partition_by(None) == []


def test_existing_table_with_different_spec_logs_a_warning(catalog_config, sample_table, caplog):
    Iceberg("rides", catalog_config).put(sample_table)
    table = Iceberg("rides", catalog_config, partition_by=[{"column": "city", "transform": "identity"}])

    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        table.put(sample_table)

    assert "only applies at creation" in caplog.text
    assert table.table().spec().fields == ()


# --------------------------------------------------------------------------- schema evolution


def test_new_columns_are_added_by_name(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)
    with_tip = sample_table.append_column("tip", pa.array([1.0] * 6))

    table.put(with_tip)

    stored = table.get()
    assert "tip" in table.table().schema().column_names
    assert stored.num_rows == 12
    assert sorted(stored.column("tip").to_pylist(), key=lambda v: (v is not None, v)) == [None] * 6 + [1.0] * 6


def test_schema_evolution_off_rejects_new_columns(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, schema_evolution=False)
    table.put(sample_table)

    with pytest.raises(ValueError):
        table.put(sample_table.append_column("tip", pa.array([1.0] * 6)))
    assert "tip" not in table.table().schema().column_names
    assert table.row_count() == 6


def test_columns_missing_from_new_data_are_null(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)

    table.put(sample_table.drop_columns(["fare"]))

    assert table.get().column("fare").null_count == 6


def test_schema_evolution_works_with_overwrite(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config, write_mode="overwrite")
    table.put(sample_table)

    table.put(sample_table.append_column("tip", pa.array([2.0] * 6)))

    assert table.get().column("tip").to_pylist() == [2.0] * 6


# --------------------------------------------------------------------------- reading, snapshots, time travel


def test_time_travel_returns_the_old_snapshots_rows(catalog_config, make_table):
    table = Iceberg("rides", catalog_config)
    first = table.put(make_table(n=6))
    table.put(make_table(n=2, start_id=100), mode="overwrite")

    assert _ids(table.get()) == [100, 101]
    assert _ids(table.get(snapshot_id=first.snapshot_id)) == [1, 2, 3, 4, 5, 6]


def test_time_travel_to_before_schema_evolution(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    first = table.put(sample_table)
    table.put(sample_table.append_column("tip", pa.array([1.0] * 6)))

    old = table.get(snapshot_id=first.snapshot_id)

    assert old.num_rows == 6


def test_get_with_filter_fields_and_limit(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)

    nyc = table.get(row_filter="city == 'NYC'", selected_fields=["ride_id", "city"])
    assert nyc.column_names == ["ride_id", "city"]
    assert set(nyc.column("city").to_pylist()) == {"NYC"}
    assert table.get(selected_fields="fare").column_names == ["fare"]
    assert table.get(limit=2).num_rows == 2


def test_get_unknown_snapshot_is_a_value_error(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)

    with pytest.raises(ValueError, match="snapshot 123"):
        table.get(snapshot_id=123)


@pytest.mark.parametrize("method", ["get", "table", "row_count", "snapshots"])
def test_missing_table_raises_table_not_found(catalog_config, method):
    table = Iceberg("rides", catalog_config)

    with pytest.raises(TableNotFound, match="test_ns.rides"):
        getattr(table, method)()


def test_snapshots_describe_the_history(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    first = table.put(sample_table)
    last = table.put(sample_table, mode="overwrite")

    snapshots = table.snapshots()

    assert all(set(s) == {"snapshot_id", "parent_id", "timestamp_ms", "operation", "summary"} for s in snapshots)
    assert snapshots[0]["snapshot_id"] == first.snapshot_id
    assert snapshots[0]["parent_id"] is None
    assert snapshots[0]["operation"] == "append"
    assert snapshots[0]["summary"]["total-records"] == "6"
    assert snapshots[-1]["snapshot_id"] == last.snapshot_id
    assert [s["parent_id"] for s in snapshots[1:]] == [s["snapshot_id"] for s in snapshots[:-1]]
    assert [s["timestamp_ms"] for s in snapshots] == sorted(s["timestamp_ms"] for s in snapshots)
    assert all(isinstance(v, str) for s in snapshots for v in s["summary"].values())


def test_table_returns_the_pyiceberg_table(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)

    assert isinstance(table.table(), PyIcebergTable)
    assert table.exists() is True


def test_row_count_matches_a_full_scan_after_every_mode(catalog_config, make_table):
    table = Iceberg("rides", catalog_config, join_cols=["ride_id"])
    for df, mode in [(make_table(n=6), "append"), (make_table(n=3), "append"),
                     (make_table(n=4, start_id=5, fare_offset=1.0), "upsert"), (make_table(n=5), "overwrite")]:
        table.put(df, mode=mode)
        assert table.row_count() == table.get().num_rows


def test_row_count_falls_back_to_scanning_without_summary(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)
    real = table.table()

    class NoSummarySnapshot:
        snapshot_id = real.current_snapshot().snapshot_id
        summary = None

    class TableWithoutSummary:
        def current_snapshot(self):
            return NoSummarySnapshot()

        def scan(self, **kwargs):
            return real.scan(**kwargs)

    assert _row_count(TableWithoutSummary()) == 6


# --------------------------------------------------------------------------- 0.2.0 (C3): catalogs and direct-mode fixes


def test_catalog_is_built_by_the_provider(catalog_config, monkeypatch):
    from local_data_platform.catalog import provider

    calls = []
    real = provider.create_catalog

    def spy(spec, *, base_dir=None):
        calls.append((dict(spec), base_dir))
        return real(spec, base_dir=base_dir)

    monkeypatch.setattr(provider, "create_catalog", spy)

    table = Iceberg("rides", catalog_config, base_dir="/somewhere")

    assert len(calls) == 1 and calls[0][0]["identifier"] == "test_ns"
    assert calls[0][0]["warehouse_path"] == str(table.path) and calls[0][1] == "/somewhere"
    assert table.catalog_spec == catalog_config


def test_a_registered_catalog_type_is_used_with_its_namespace(tmp_path, sample_table):
    from pyiceberg.catalog.sql import SqlCatalog

    from local_data_platform.catalog.provider import register_catalog_type

    @register_catalog_type("c3_test_plugin")
    def _plugin(spec, base_dir):
        return SqlCatalog("plugin", uri=f"sqlite:///{spec['db']}", warehouse=f"file://{tmp_path}")

    table = Iceberg("rides", {"type": "c3_test_plugin", "namespace": "plug", "db": str(tmp_path / "p.db")})

    assert isinstance(table.catalog, SqlCatalog) and not isinstance(table.catalog, LocalIcebergCatalog)
    assert (table.identifier, table.path, table.lock_path()) == ("plug.rides", None, None)
    assert table.warehouse == f"file://{tmp_path}"
    assert table.put(sample_table, mode="overwrite").rows_after == 6  # no lock file on non-local catalogs
    assert "plug.rides" in repr(table)
    table.catalog.engine.dispose()


def test_catalog_obj_is_used_as_is(catalog_config, sample_table):
    catalog = LocalIcebergCatalog("objns", path=catalog_config["warehouse_path"])

    table = Iceberg("rides", catalog_obj=catalog)
    named = Iceberg("rides", {"namespace": "other", "type": "sql", "uri": "unused"}, catalog_obj=catalog)

    assert table.catalog is catalog and table.identifier == "objns.rides"
    assert table.path == catalog.warehouse_path and table.catalog_spec is None
    assert named.identifier == "other.rides"
    assert table.put(sample_table).rows_after == 6


def test_direct_schema_union_and_write_are_one_commit(catalog_config, sample_table):
    table = Iceberg("rides", catalog_config)
    table.put(sample_table)
    before = len(table.table().metadata.metadata_log)

    table.put(sample_table.append_column("tip", pa.array([1.0] * 6)))

    after = table.table()
    assert len(after.metadata.metadata_log) == before + 1
    assert "tip" in after.schema().column_names and table.row_count() == 12


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_direct_counts_come_from_snapshot_summaries(catalog_config, make_table, monkeypatch, mode):
    table = Iceberg("rides", catalog_config)
    table.put(make_table(n=6))

    def no_scan(*args, **kwargs):
        raise AssertionError("rows_before and rows_after must come from snapshot summaries")

    monkeypatch.setattr(PyIcebergTable, "scan", no_scan)
    result = table.put(make_table(n=2, start_id=50), mode=mode)

    assert (result.rows_before, result.rows_after) == ((6, 8) if mode == "append" else (6, 2))


def test_direct_append_rows_before_is_the_parent_of_the_new_snapshot(catalog_config, make_table):
    table = Iceberg("rides", catalog_config)
    table.put(make_table(n=6))
    result = table.put(make_table(n=3, start_id=10))

    snapshot = table.table().snapshot_by_id(result.snapshot_id)
    parent = table.table().snapshot_by_id(snapshot.parent_snapshot_id)
    assert result.rows_before == int(parent.summary.additional_properties["total-records"]) == 6


def test_direct_overwrite_filter_replaces_only_matching_rows(catalog_config, make_table):
    table = Iceberg("rides", catalog_config)
    table.put(make_table(n=6))

    result = table.put(make_table(n=1, start_id=100).set_column(1, "city", pa.array(["NYC"])),
                       mode="overwrite", overwrite_filter="city == 'NYC'")

    stored = table.get()
    assert sorted(stored.filter(pc.equal(stored.column("city"), "NYC")).column("ride_id").to_pylist()) == [100]
    assert result.rows_after == stored.num_rows == 5
    with pytest.raises(ConfigError, match="overwrite_filter"):
        table.put(make_table(n=1), mode="append", overwrite_filter="city == 'NYC'")


@pytest.mark.parametrize("mode, blocks", [("overwrite", True), ("upsert", True), ("append", False)])
def test_direct_overwrite_and_upsert_wait_for_the_table_lock(catalog_config, sample_table, mode, blocks):
    import fcntl
    import threading

    table = Iceberg("rides", catalog_config, join_cols=["ride_id"])
    table.put(sample_table)
    lock = table.lock_path()
    assert lock == table.path / ".ldp" / "locks" / "test_ns.rides.lock"
    assert not lock.exists()  # the setup write is an append, which never touches the lock

    done = threading.Event()
    errors: list[BaseException] = []

    def write():
        try:
            table.put(sample_table, mode=mode)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)
        else:
            done.set()

    # Stand in for another process holding the lock, as exclusive_lock() would (it makes the folder).
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        writer = threading.Thread(target=write)
        writer.start()
        # A locked mode must still be waiting after 0.5s; an append must finish while the lock is held.
        finished_while_locked = done.wait(0.5 if blocks else 30)
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    writer.join(30)

    assert not errors, errors
    assert finished_while_locked is (not blocks)
    assert done.is_set()


def test_exclusive_lock_is_a_no_op_off_local_catalogs(tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog

    catalog = SqlCatalog("plain", uri=f"sqlite:///{tmp_path}/plain.db", warehouse=f"file://{tmp_path}")
    table = Iceberg("rides", {"type": "sql", "namespace": "ns"}, catalog_obj=catalog)

    with table.exclusive_lock() as path:
        assert path is None
    catalog.engine.dispose()
