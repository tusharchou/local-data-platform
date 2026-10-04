"""Tests for local_data_platform.engine.router: scan-size estimates and engine choice (design v0_2_0.md, C5)."""

import logging
import os
import sys
import types

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import DoubleType, LongType, NestedField, StringType

from local_data_platform import SupportedEngine
from local_data_platform.engine import router
from local_data_platform.engine.router import (DUCKDB_MAX_BYTES, PYARROW_MAX_BYTES, available_engines, choose_engine,
                                               estimate_scan_bytes)
from local_data_platform.exceptions import EngineNotFound

GiB = 2**30
ALL = {SupportedEngine.PYARROW, SupportedEngine.DUCKDB, SupportedEngine.PYSPARK}


@pytest.fixture
def catalog(tmp_path):
    cat = SqlCatalog("test", uri=f"sqlite:///{tmp_path}/catalog.db", warehouse=f"file://{tmp_path}")
    cat.create_namespace("nyc")
    yield cat
    cat.engine.dispose()


def _rides(start: int, n: int, city: str | None = None) -> pa.Table:
    ids = list(range(start, start + n))
    return pa.table({"ride_id": pa.array(ids, pa.int64()),
                     "city": pa.array([city or ["NYC", "BKK", "LDN"][i % 3] for i in ids]),
                     "fare": pa.array([float(i) for i in ids])})


def _local_path(uri: str) -> str:
    return uri.removeprefix("file://")


@pytest.fixture
def two_files(catalog):
    """An unpartitioned table with two snapshots: ride_id 1-40, then ride_id 41-100."""
    table = catalog.create_table("nyc.rides", schema=_rides(1, 1).schema)
    table.append(_rides(1, 40))
    table.append(_rides(41, 60))
    return table


@pytest.fixture
def by_city(catalog):
    schema = Schema(NestedField(1, "ride_id", LongType()), NestedField(2, "city", StringType()),
                    NestedField(3, "fare", DoubleType()))
    spec = PartitionSpec(PartitionField(2, 1000, IdentityTransform(), "city"))
    table = catalog.create_table("nyc.by_city", schema=schema, partition_spec=spec)
    for start, city in ((1, "NYC"), (100, "BKK"), (200, "LDN")):
        table.append(_rides(start, 50, city))
    return table


def _file_sizes(table, **scan_kwargs) -> dict[str, int]:
    return {task.file.file_path: os.path.getsize(_local_path(task.file.file_path))
            for task in table.scan(**scan_kwargs).plan_files()}


# ---------------------------------------------------------------------------------------------------
# estimate_scan_bytes
# ---------------------------------------------------------------------------------------------------

def test_estimate_is_the_on_disk_size_of_the_data_files(two_files):
    sizes = _file_sizes(two_files)
    assert len(sizes) == 2
    assert estimate_scan_bytes(two_files) == sum(sizes.values()) > 0


def test_estimate_prunes_partitions(by_city):
    everything = estimate_scan_bytes(by_city)
    nyc = estimate_scan_bytes(by_city, row_filter="city = 'NYC'")
    two = estimate_scan_bytes(by_city, row_filter="city IN ('NYC', 'BKK')")
    nyc_files = [path for path in _file_sizes(by_city) if "city=NYC" in path]

    assert len(nyc_files) == 1
    assert nyc == os.path.getsize(_local_path(nyc_files[0]))
    assert 0 < nyc < two < everything
    assert estimate_scan_bytes(by_city, row_filter="city = 'nowhere'") == 0


def test_estimate_prunes_by_column_statistics(two_files):
    second_file = estimate_scan_bytes(two_files, row_filter="ride_id > 40")
    assert 0 < second_file < estimate_scan_bytes(two_files)
    assert estimate_scan_bytes(two_files, row_filter="ride_id > 1000") == 0


def test_estimate_time_travel(two_files):
    first, second = [snapshot.snapshot_id for snapshot in two_files.snapshots()]
    assert estimate_scan_bytes(two_files, snapshot_id=first) == sum(_file_sizes(two_files, snapshot_id=first).values())
    assert estimate_scan_bytes(two_files, snapshot_id=first) < estimate_scan_bytes(two_files, snapshot_id=second)
    with pytest.raises(ValueError, match="Snapshot 42 not found"):
        estimate_scan_bytes(two_files, snapshot_id=42)


def test_estimate_on_an_empty_table_and_a_format_object(catalog, two_files):
    empty = catalog.create_table("nyc.empty", schema=_rides(1, 1).schema)
    assert estimate_scan_bytes(empty) == 0
    assert estimate_scan_bytes(empty, row_filter="fare > 1") == 0

    class FakeIcebergFormat:
        def table(self):
            return two_files

    assert estimate_scan_bytes(FakeIcebergFormat()) == estimate_scan_bytes(two_files)
    with pytest.raises(TypeError, match="estimate_scan_bytes"):
        estimate_scan_bytes(pa.table({"a": [1]}))


def test_estimate_counts_each_delete_file_once(two_files, monkeypatch):
    shared = types.SimpleNamespace(file_path="file:///d/pos-deletes.parquet", file_size_in_bytes=7)
    tasks = [types.SimpleNamespace(file=types.SimpleNamespace(file_size_in_bytes=100), delete_files=[shared]),
             types.SimpleNamespace(file=types.SimpleNamespace(file_size_in_bytes=50), delete_files=[shared])]
    monkeypatch.setattr(two_files, "scan", lambda **kwargs: types.SimpleNamespace(plan_files=lambda: tasks))
    assert estimate_scan_bytes(two_files) == 157


def test_estimate_reads_no_data_files(two_files, monkeypatch):
    from pyiceberg.table import DataScan

    def no_read(*args, **kwargs):
        raise AssertionError("estimating must not read data")

    for method in ("to_arrow", "to_arrow_batch_reader"):
        monkeypatch.setattr(DataScan, method, no_read)
    assert estimate_scan_bytes(two_files, row_filter="city = 'NYC'") > 0


# ---------------------------------------------------------------------------------------------------
# choose_engine
# ---------------------------------------------------------------------------------------------------

def test_defaults_are_the_contract_thresholds():
    assert DUCKDB_MAX_BYTES == 50 * GiB
    assert PYARROW_MAX_BYTES == GiB


@pytest.mark.parametrize("scan_bytes, expected", [
    (0, SupportedEngine.DUCKDB),
    (DUCKDB_MAX_BYTES, SupportedEngine.DUCKDB),
    (DUCKDB_MAX_BYTES + 1, SupportedEngine.PYSPARK),
    (10 * DUCKDB_MAX_BYTES, SupportedEngine.PYSPARK),
])
def test_sql_goes_to_duckdb_until_its_threshold_then_spark(scan_bytes, expected):
    assert choose_engine(scan_bytes, "sql", ALL) is expected


@pytest.mark.parametrize("scan_bytes, expected", [
    (0, SupportedEngine.PYARROW),
    (PYARROW_MAX_BYTES, SupportedEngine.PYARROW),
    (PYARROW_MAX_BYTES + 1, SupportedEngine.DUCKDB),
    (DUCKDB_MAX_BYTES, SupportedEngine.DUCKDB),
    (DUCKDB_MAX_BYTES + 1, SupportedEngine.PYSPARK),
])
def test_arrow_reads_go_to_pyarrow_then_duckdb_then_spark(scan_bytes, expected):
    assert choose_engine(scan_bytes, "arrow", ALL) is expected


def test_custom_thresholds_and_engine_names():
    assert choose_engine(11, "sql", ["duckdb", "pyspark"], duckdb_max_bytes=10) is SupportedEngine.PYSPARK
    assert choose_engine(10, "sql", ["DUCKDB", "PySpark"], duckdb_max_bytes=10) is SupportedEngine.DUCKDB
    assert choose_engine(5, "arrow", {SupportedEngine.PYARROW}, pyarrow_max_bytes=4) is SupportedEngine.PYARROW
    assert choose_engine(5, "ARROW", "pyarrow") is SupportedEngine.PYARROW


def test_over_the_threshold_without_a_bigger_engine_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="local_data_platform.engine.router"):
        assert choose_engine(DUCKDB_MAX_BYTES + 1, "sql", ["duckdb", "pyarrow"]) is SupportedEngine.DUCKDB
    assert "over the DUCKDB limit" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="local_data_platform.engine.router"):
        assert choose_engine(PYARROW_MAX_BYTES + 1, "arrow", ["pyarrow"]) is SupportedEngine.PYARROW
    assert "over the PYARROW limit" in caplog.text


def test_sql_without_a_sql_engine_raises_with_install_hints():
    with pytest.raises(EngineNotFound, match=r'local-data-platform\[duckdb\].*local-data-platform\[spark\]'):
        choose_engine(1, "sql", [SupportedEngine.PYARROW, SupportedEngine.BIGQUERY])


def test_bigquery_is_never_chosen():
    assert choose_engine(1, "arrow", ["bigquery", "pyarrow"]) is SupportedEngine.PYARROW
    with pytest.raises(EngineNotFound):
        choose_engine(1, "arrow", ["bigquery"])


@pytest.mark.parametrize("kwargs, message", [
    ({"scan_bytes": -1}, "non-negative"),
    ({"scan_bytes": 1.5}, "non-negative"),
    ({"scan_bytes": True}, "non-negative"),
    ({"scan_bytes": 1, "how": "graph"}, "how must be one of"),
    ({"scan_bytes": 1, "available": ["flink"]}, "unknown engine 'flink'"),
])
def test_bad_arguments(kwargs, message):
    with pytest.raises(ValueError, match=message):
        choose_engine(**kwargs)


def test_available_engines_detects_installed_packages_without_importing_them(monkeypatch):
    found = {"duckdb": True, "pyspark": False}
    monkeypatch.setattr(router.importlib.util, "find_spec", lambda name: object() if found.get(name) else None)
    assert available_engines() == {SupportedEngine.PYARROW, SupportedEngine.DUCKDB}
    assert choose_engine(DUCKDB_MAX_BYTES + 1) is SupportedEngine.DUCKDB  # the default `available`
    found["pyspark"] = True
    assert choose_engine(DUCKDB_MAX_BYTES + 1) is SupportedEngine.PYSPARK
    found.update(duckdb=False, pyspark=False)
    with pytest.raises(EngineNotFound):
        choose_engine(1)


def test_estimate_feeds_choose_engine(by_city):
    size = estimate_scan_bytes(by_city, row_filter="city = 'NYC'")
    assert choose_engine(size, "sql", ALL) is SupportedEngine.DUCKDB
    assert choose_engine(size, "sql", ALL, duckdb_max_bytes=size - 1) is SupportedEngine.PYSPARK


def test_importing_the_router_imports_no_engine():
    code = ("import sys, local_data_platform.engine.router; "
            "sys.exit(1 if {'duckdb', 'pyspark'} & set(sys.modules) else 0)")
    import subprocess
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
