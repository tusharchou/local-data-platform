"""Tests for the DuckDB SQL engine (design section C).

Iceberg tables are created directly with pyiceberg's ``SqlCatalog`` in ``tmp_path``
so these tests don't depend on ``local_data_platform.format.iceberg``.
"""

import subprocess
import sys
import warnings

import pyarrow as pa
import pyarrow.compute  # noqa: F401
import pytest

pytest.importorskip("duckdb")

import duckdb  # noqa: E402
from pyiceberg.catalog.sql import SqlCatalog  # noqa: E402
from pyiceberg.expressions import EqualTo  # noqa: E402

from local_data_platform.engine import Engine  # noqa: E402
from local_data_platform.engine.duckdb import INSTALL_HINT, DuckDBEngine  # noqa: E402
from local_data_platform.exceptions import EngineNotFound  # noqa: E402

BATCH_1 = pa.table({
    "ride_id": pa.array([1, 2, 3, 4], pa.int64()),
    "city": pa.array(["NYC", "NYC", "NYC", "NYC"]),
    "fare": pa.array([10.0, 20.0, 5.0, 15.0]),
})
BATCH_2 = pa.table({
    "ride_id": pa.array([5, 6, 7], pa.int64()),
    "city": pa.array(["BKK", "BKK", "BKK"]),
    "fare": pa.array([100.0, 200.0, 300.0]),
})


@pytest.fixture
def catalog(tmp_path):
    cat = SqlCatalog("test", uri=f"sqlite:///{tmp_path}/catalog.db", warehouse=f"file://{tmp_path}")
    cat.create_namespace("nyc")
    yield cat
    cat.engine.dispose()  # close the pooled SQLite connections (no ResourceWarning)


@pytest.fixture
def rides(catalog):
    """An Iceberg table with two snapshots: BATCH_1, then BATCH_1 + BATCH_2 (two data files)."""
    table = catalog.create_table("nyc.rides", schema=BATCH_1.schema)
    table.append(BATCH_1)
    table.append(BATCH_2)
    return table


class FakeIcebergFormat:
    """Stands in for ``local_data_platform.format.iceberg.Iceberg``: ``table()`` returns the pyiceberg Table."""

    def __init__(self, table):
        self._table = table

    def table(self):
        return self._table


def test_sql_aggregation_over_iceberg(rides):
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "rides")
        result = engine.query(
            "SELECT city, count(*) AS n, sum(fare) AS total FROM rides GROUP BY city ORDER BY city"
        )

    assert isinstance(result, pa.Table)
    assert result.to_pylist() == [
        {"city": "BKK", "n": 3, "total": 600.0},
        {"city": "NYC", "n": 4, "total": 50.0},
    ]


def test_register_iceberg_accepts_format_object(rides):
    with DuckDBEngine() as engine:
        engine.register_iceberg(FakeIcebergFormat(rides), "rides")
        assert engine.query("SELECT count(*) AS n FROM rides").to_pylist() == [{"n": 7}]


def test_time_travel_via_snapshot_id(rides):
    first, second = [snapshot.snapshot_id for snapshot in rides.snapshots()]

    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "rides_v1", snapshot_id=first)
        engine.register_iceberg(rides, "rides_v2", snapshot_id=second)
        engine.register_iceberg(rides, "rides_now")
        counts = engine.query(
            "SELECT (SELECT count(*) FROM rides_v1) AS v1, (SELECT count(*) FROM rides_v2) AS v2, "
            "(SELECT count(*) FROM rides_now) AS now"
        ).to_pylist()
        cities_v1 = engine.query("SELECT DISTINCT city FROM rides_v1").column("city").to_pylist()

    assert counts == [{"v1": 4, "v2": 7, "now": 7}]
    assert cities_v1 == ["NYC"]


def test_unknown_snapshot_id_raises(rides):
    with DuckDBEngine() as engine:
        with pytest.raises(ValueError, match="Snapshot 123 not found"):
            engine.register_iceberg(rides, "rides", snapshot_id=123)
        assert engine.aliases == []


def test_row_filter_is_pushed_into_the_iceberg_scan(rides, monkeypatch):
    # The in-memory path (the original one, forced with native=False): pyiceberg applies the filter
    # while scanning. The native path is covered by test_native_row_filter_is_pushed_into_iceberg_scan.
    scans = []
    original_scan = rides.scan

    def spy_scan(**kwargs):
        scan = original_scan(**kwargs)
        scans.append((kwargs, scan))
        return scan

    monkeypatch.setattr(rides, "scan", spy_scan)

    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "bkk", row_filter="city = 'BKK'", native=False)
        # No WHERE clause: every row DuckDB sees already passed the Iceberg filter.
        result = engine.query("SELECT count(*) AS n, min(fare) AS lo FROM bkk").to_pylist()

    assert result == [{"n": 3, "lo": 100.0}]
    (kwargs, scan), = scans
    assert kwargs == {"row_filter": "city = 'BKK'"}
    # The filter prunes data files at plan time: only BATCH_2's file can hold BKK rows.
    assert len(list(scan.plan_files())) == 1
    assert len(list(original_scan().plan_files())) == 2


def test_row_filter_expression_with_snapshot(rides):
    first = rides.snapshots()[0].snapshot_id
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "cheap", snapshot_id=first, row_filter=EqualTo("fare", 5.0))
        assert engine.query("SELECT ride_id FROM cheap").to_pylist() == [{"ride_id": 3}]


def test_register_arrow_and_join_with_iceberg(rides):
    cities = pa.table({"city": ["NYC", "BKK"], "country": ["US", "TH"]})
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "rides").register_arrow(cities, "cities")
        result = engine.query(
            "SELECT c.country, count(*) AS n FROM rides r JOIN cities c USING (city) "
            "GROUP BY c.country ORDER BY c.country"
        )
        assert engine.aliases == ["rides", "cities"]

    assert result.to_pylist() == [{"country": "TH", "n": 3}, {"country": "US", "n": 4}]


def test_register_arrow_record_batch_and_replace_alias():
    with DuckDBEngine() as engine:
        engine.register_arrow(pa.record_batch({"a": [1, 2]}), "t")
        assert engine.query("SELECT sum(a) AS s FROM t").to_pylist() == [{"s": 3}]
        engine.register_arrow(pa.table({"a": [10]}), "t")
        assert engine.query("SELECT sum(a) AS s FROM t").to_pylist() == [{"s": 10}]
        assert engine.aliases == ["t"]


def test_query_with_params():
    with DuckDBEngine() as engine:
        engine.register_arrow(pa.table({"a": [1, 2, 3]}), "t")
        assert engine.query("SELECT count(*) AS n FROM t WHERE a >= ?", [2]).to_pylist() == [{"n": 2}]


def test_query_result_is_pyarrow_table_without_deprecation_warning():
    with DuckDBEngine() as engine, warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        result = engine.query("SELECT 42 AS answer")
    assert isinstance(result, pa.Table)
    assert result.to_pylist() == [{"answer": 42}]


def test_bad_inputs_raise(rides):
    with DuckDBEngine() as engine:
        with pytest.raises(TypeError, match="pyiceberg Table"):
            engine.register_iceberg(pa.table({"a": [1]}), "x")
        with pytest.raises(TypeError, match="returned"):
            engine.register_iceberg(FakeIcebergFormat("not a table"), "x")
        with pytest.raises(ValueError, match="alias"):
            engine.register_iceberg(rides, "")
        with pytest.raises(ValueError, match="None"):
            engine.register_arrow(None, "x")
        with pytest.raises(TypeError, match="pyarrow.Table"):
            engine.register_arrow({"a": [1]}, "x")
        with pytest.raises(duckdb.Error):
            engine.query("SELECT * FROM does_not_exist")


def test_close_owned_connection_and_context_manager():
    with DuckDBEngine() as engine:
        engine.register_arrow(pa.table({"a": [1]}), "t")
        connection = engine.connection
    with pytest.raises(RuntimeError, match="closed"):
        engine.query("SELECT 1")
    with pytest.raises(duckdb.ConnectionException):
        connection.execute("SELECT 1")
    engine.close()  # idempotent


def test_passed_connection_stays_open_and_views_are_dropped():
    connection = duckdb.connect()
    connection.execute("CREATE TABLE mine AS SELECT 1 AS a")
    engine = DuckDBEngine(connection=connection)
    engine.register_arrow(pa.table({"b": [2]}), "theirs")
    assert engine.query("SELECT a, b FROM mine, theirs").to_pylist() == [{"a": 1, "b": 2}]

    engine.close()

    assert connection.execute("SELECT a FROM mine").fetchall() == [(1,)]
    with pytest.raises(duckdb.CatalogException):
        connection.execute("SELECT * FROM theirs")
    connection.close()


def test_missing_duckdb_raises_engine_not_found(monkeypatch):
    # A None entry in sys.modules makes ``import duckdb`` raise ImportError.
    monkeypatch.setitem(sys.modules, "duckdb", None)
    with pytest.raises(EngineNotFound) as error:
        DuckDBEngine()
    assert INSTALL_HINT in str(error.value)
    assert INSTALL_HINT == 'pip install "local-data-platform[duckdb]"'
    assert isinstance(error.value.__cause__, ImportError)


def test_importing_engine_module_does_not_import_duckdb():
    code = (
        "import sys; import local_data_platform.engine.duckdb; "
        "sys.exit(1 if 'duckdb' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_engine_base_still_works():
    engine = Engine("spark")
    assert engine.name == "spark"
    with pytest.raises(EngineNotFound, match="spark"):
        engine.get()
    with pytest.raises(EngineNotFound, match="spark"):
        engine.put()


def test_duckdb_engine_get_and_put_follow_base_interface():
    with DuckDBEngine() as engine:
        assert isinstance(engine, Engine)
        assert engine.name == "duckdb"
        engine.put(pa.table({"a": [1, 2]}), "t")
        assert engine.get("SELECT max(a) AS m FROM t").to_pylist() == [{"m": 2}]


# ---------------------------------------------------------------------------------------------------
# Native iceberg_scan views (the 0.1.1 platform contract, C5)
# ---------------------------------------------------------------------------------------------------

import datetime as dt  # noqa: E402
import gc  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
from decimal import Decimal  # noqa: E402

from pyiceberg.expressions import And, In, IsNull, Not, NotIn, Or, StartsWith  # noqa: E402
from pyiceberg.partitioning import PartitionField, PartitionSpec  # noqa: E402
from pyiceberg.schema import Schema  # noqa: E402
from pyiceberg.transforms import BucketTransform, DayTransform, IdentityTransform  # noqa: E402
from pyiceberg import types as iceberg_types  # noqa: E402
from pyiceberg.types import (BinaryType, BooleanType, DateType, DecimalType, DoubleType, FloatType,  # noqa: E402
                             ListType, LongType, NestedField, StringType, StructType, TimestampType,
                             TimestamptzType)

from local_data_platform.engine.duckdb import ICEBERG_EXTENSION_HINT  # noqa: E402
from local_data_platform.engine.duckdb.row_filter import (  # noqa: E402
    UntranslatableFilter, literal_sql, row_filter_to_sql)
from local_data_platform.exceptions import ConfigError  # noqa: E402


def _iceberg_extension_loads() -> bool:
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT installed FROM duckdb_extensions() WHERE extension_name = 'iceberg'").fetchone()
        if not row or not row[0]:
            return False
        connection.execute("LOAD iceberg")
        return True
    except duckdb.Error:
        return False
    finally:
        connection.close()


requires_iceberg_extension = pytest.mark.skipif(
    not _iceberg_extension_loads(),
    reason="DuckDB's iceberg extension is not installed; run duckdb.connect().execute('INSTALL iceberg') once",
)

LOGGER = "local_data_platform.engine.duckdb"
UTC = dt.timezone.utc

RICH = pa.table({
    "id": pa.array(range(1, 9), pa.int64()),
    "city": pa.array(["NYC", "BKK", None, "NYC", "LDN", "O'Hare", "BKK", None]),
    "fare": pa.array([10.0, float("nan"), 5.5, None, 30.0, 12.25, 200.0, 7.0]),
    "small": pa.array([1.5, 0.1, None, 2.5, float("nan"), 3.0, 0.1, 4.0], pa.float32()),
    "paid": pa.array([True, False, None, True, False, True, None, False]),
    "day": pa.array([dt.date(2026, 9, 1) + dt.timedelta(days=i % 3) for i in range(8)], pa.date32()),
    "ts": pa.array([dt.datetime(2026, 9, 1, 8) + dt.timedelta(hours=i) for i in range(8)], pa.timestamp("us")),
    "tstz": pa.array([dt.datetime(2026, 9, 1, 8, tzinfo=UTC) + dt.timedelta(hours=i) for i in range(8)],
                     pa.timestamp("us", tz="UTC")),
    "amount": pa.array([Decimal(f"{10 + i}.{i}5") for i in range(8)], pa.decimal128(10, 2)),
    "loc": pa.array([{"lat": 40.5, "zone": "north"}, {"lat": 13.7, "zone": "south"}, None,
                     {"lat": 40.9, "zone": "north"}, {"lat": 51.5, "zone": None}, {"lat": 41.9, "zone": "west"},
                     {"lat": 13.8, "zone": "south"}, {"lat": None, "zone": "north"}],
                    pa.struct([("lat", pa.float64()), ("zone", pa.string())])),
    "code": pa.array([bytes([i, i + 1]) for i in range(8)], pa.binary()),
})

PARITY_FILTERS = [
    "city = 'NYC'",
    "city != 'NYC'",
    "city IN ('NYC', 'BKK')",
    "city NOT IN ('NYC', 'BKK')",
    "NOT (city IN ('NYC', 'BKK'))",
    "NOT (city NOT IN ('NYC', 'BKK'))",
    "city IS NULL",
    "city IS NOT NULL",
    "city LIKE 'N%'",
    "city NOT LIKE 'N%'",
    "city = 'O''Hare'",
    "fare > 10",
    "fare >= 12.25",
    "fare < 10",
    "fare <= 5.5",
    "fare <> 10.0",
    "NOT (fare > 10)",
    "fare IS NAN",
    "fare IS NOT NAN",
    "small > 0.1",
    "small < 2.5",
    "paid = true",
    "paid = false",
    "day = '2026-09-02'",
    "ts >= '2026-09-01T11:00:00'",
    "tstz < '2026-09-01T12:00:00+00:00'",
    "amount > 12.50",
    "loc.zone = 'north'",
    "loc.lat BETWEEN 40 AND 41",
    "id BETWEEN 2 AND 5 AND NOT (city = 'NYC')",
    "city = 'NYC' OR fare > 100",
    "true",
    "false",
    EqualTo("code", b"\x03\x04"),
    And(In("city", ["NYC", "LDN"]), Not(IsNull("fare"))),
    Or(NotIn("id", [1, 2, 3]), StartsWith("city", "B")),
    Not(Or(EqualTo("paid", True), IsNull("paid"))),
]


def _rows(table: pa.Table) -> list[dict]:
    """``table.to_pylist()`` with NaN replaced by a marker, so rows compare equal."""
    def clean(value):
        if isinstance(value, float) and math.isnan(value):
            return "NaN"
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value
    return [{key: clean(value) for key, value in row.items()} for row in table.to_pylist()]


@pytest.fixture
def rich(catalog):
    table = catalog.create_table("nyc.rich", schema=RICH.schema)
    table.append(RICH.slice(0, 5))
    table.append(RICH.slice(5))
    return table


def _both_paths(table, sql="SELECT * FROM t ORDER BY id", **kwargs):
    with DuckDBEngine() as engine:
        engine.register_iceberg(table, "t", native=True, **kwargs)
        assert engine.is_native("t")
        native = _rows(engine.query(sql))
        engine.register_iceberg(table, "t", native=False, **kwargs)
        assert not engine.is_native("t")
        memory = _rows(engine.query(sql))
    return native, memory


@requires_iceberg_extension
def test_native_is_the_default_when_the_extension_loads(rides):
    with DuckDBEngine() as engine:
        assert engine.iceberg_extension
        engine.register_iceberg(rides, "rides")
        assert engine.is_native("rides")
        views = engine.query("SELECT sql FROM duckdb_views() WHERE view_name = 'rides'").column("sql").to_pylist()
    assert len(views) == 1 and "iceberg_scan(" in views[0]
    assert rides.metadata_location in views[0]


@requires_iceberg_extension
@pytest.mark.parametrize("row_filter", PARITY_FILTERS, ids=str)
def test_native_row_filter_matches_the_pyiceberg_scan(rich, row_filter):
    native, memory = _both_paths(rich, row_filter=row_filter)
    assert native == memory
    expected = _rows(rich.scan(row_filter=row_filter).to_arrow().sort_by("id"))
    assert memory == expected


@requires_iceberg_extension
def test_native_null_and_nan_semantics_follow_pyiceberg(rich):
    def ids(row_filter):
        native, memory = _both_paths(rich, "SELECT id FROM t ORDER BY id", row_filter=row_filter)
        assert native == memory
        return [row["id"] for row in native]

    # NOT IN keeps null cities (pyarrow is_in() is false for a null); != drops them.
    assert ids("city NOT IN ('NYC', 'BKK')") == [3, 5, 6, 8]
    assert ids("city != 'NYC'") == [2, 5, 6, 7]
    # NaN is never greater than a number, although DuckDB sorts NaN above every number.
    assert ids("fare > 100") == [7]
    assert ids("fare IS NAN") == [2]
    # A null struct makes its fields null.
    assert ids("loc.zone = 'north'") == [1, 4, 8]


@requires_iceberg_extension
def test_native_time_travel_matches_every_snapshot(rides):
    first, second = [snapshot.snapshot_id for snapshot in rides.snapshots()]
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "v1", snapshot_id=first, native=True)
        engine.register_iceberg(rides, "v2", snapshot_id=second, native=True)
        engine.register_iceberg(rides, "now", native=True)
        counts = engine.query("SELECT (SELECT count(*) FROM v1) AS v1, (SELECT count(*) FROM v2) AS v2, "
                              "(SELECT count(*) FROM now) AS now").to_pylist()
        cities = engine.query("SELECT DISTINCT city FROM v1").column("city").to_pylist()
        cheap = engine.register_iceberg(rides, "cheap", snapshot_id=first, row_filter=EqualTo("fare", 5.0),
                                        native=True).query("SELECT ride_id FROM cheap").to_pylist()
    assert counts == [{"v1": 4, "v2": 7, "now": 7}]
    assert cities == ["NYC"]
    assert cheap == [{"ride_id": 3}]


@requires_iceberg_extension
def test_native_time_travel_uses_the_snapshot_schema(catalog):
    table = catalog.create_table("nyc.evolving", schema=pa.schema([("id", pa.int64()), ("b", pa.string())]))
    table.append(pa.table({"id": pa.array([1, 2], pa.int64()), "b": ["x", None]}))
    with table.update_schema() as update:
        update.add_column("c", DoubleType())
        update.rename_column("b", "bb")
    table = catalog.load_table("nyc.evolving")
    table.append(pa.table({"id": pa.array([3], pa.int64()), "bb": ["z"], "c": [1.5]}))
    table.overwrite(pa.table({"id": pa.array([9], pa.int64()), "bb": ["q"], "c": [2.5]}), overwrite_filter="id = 1")
    table = catalog.load_table("nyc.evolving")

    for snapshot in table.snapshots():
        native, memory = _both_paths(table, snapshot_id=snapshot.snapshot_id)
        assert native == memory, snapshot.snapshot_id
    first = table.snapshots()[0].snapshot_id
    # pyiceberg binds a row filter to the current schema, also when time travelling, and the rows come
    # back with the snapshot's column names.
    native, memory = _both_paths(table, snapshot_id=first, row_filter="bb = 'x'")
    assert native == memory == [{"id": 1, "b": "x"}]
    for native_flag in (True, False):
        with DuckDBEngine() as engine, pytest.raises(ValueError, match="name b"):
            engine.register_iceberg(table, "t", snapshot_id=first, row_filter="b = 'x'", native=native_flag)
    # Column c doesn't exist at the first snapshot: its predicates are constants, as in pyiceberg.
    for row_filter in ["c IS NULL", "c > 1", "c IS NOT NULL OR id = 2", "NOT (c = 1.0)"]:
        native, memory = _both_paths(table, snapshot_id=first, row_filter=row_filter)
        assert native == memory, row_filter
    with DuckDBEngine() as engine:  # pyiceberg versions disagree on != for a missing column
        engine.register_iceberg(table, "t", snapshot_id=first, row_filter="c != 1.0")
        assert not engine.is_native("t")
    native, _ = _both_paths(table, row_filter="bb IS NULL OR c > 2")
    assert native == [{"id": 2, "bb": None, "c": None}, {"id": 9, "bb": "q", "c": 2.5}]


@requires_iceberg_extension
def test_filters_on_evolved_columns_stay_exact(catalog):
    # Data files written before column c existed don't contain it. pyiceberg makes such a file's
    # predicate a constant (which one depends on its version: 0.12 says None != 1.5 is true), while
    # DuckDB sees NULL. Filters where they could disagree fall back to pyiceberg; the rest stay native.
    table = catalog.create_table("nyc.evolved", schema=pa.schema([("id", pa.int64())]))
    table.append(pa.table({"id": pa.array([1, 2], pa.int64())}))
    with table.update_schema() as update:
        update.add_column("c", DoubleType())
    table = catalog.load_table("nyc.evolved")
    table.append(pa.table({"id": pa.array([3, 4], pa.int64()), "c": [1.5, None]}))

    with DuckDBEngine() as engine:
        for row_filter, native_expected in [("c > 1", True), ("c IS NULL", True), ("c IN (1.5, 2.0)", True),
                                            ("NOT (c IN (1.5, 2.0))", True), ("NOT (c IS NULL)", True),
                                            ("id = 1 OR c = 1.5", True), ("c != 1.5", False),
                                            ("c NOT IN (1.5, 2.0)", False), ("NOT (c > 1)", False),
                                            ("c IS NOT NAN", False)]:
            engine.register_iceberg(table, "t", row_filter=row_filter)
            assert engine.is_native("t") is native_expected, row_filter
            got = sorted(engine.query("SELECT id FROM t").column("id").to_pylist())
            assert got == sorted(table.scan(row_filter=row_filter).to_arrow().column("id").to_pylist()), row_filter
        with pytest.raises(UntranslatableFilter, match="schema evolution"):
            engine.register_iceberg(table, "t", row_filter="c != 1.5", native=True)


@requires_iceberg_extension
def test_float_equality_is_exact_in_32_bits_natively(rich):
    # pyiceberg's planner compares a float column's 32-bit statistics with the 64-bit literal 0.1 and
    # skips the files holding 0.1f; the native path compares in 32 bits, as pyarrow does per row.
    with DuckDBEngine() as engine:
        engine.register_iceberg(rich, "t", row_filter="small = 0.1", native=True)
        native = engine.query("SELECT id FROM t ORDER BY id").column("id").to_pylist()
    in_memory = rich.scan().to_arrow()
    expected = in_memory.filter(pa.compute.equal(in_memory["small"], pa.scalar(0.1, pa.float32())))
    assert native == sorted(expected.column("id").to_pylist()) == [2, 7]


@requires_iceberg_extension
def test_native_matches_after_deletes_and_on_an_empty_table(catalog, rides):
    rides.delete("ride_id IN (2, 6)")
    native, memory = _both_paths(rides, "SELECT * FROM t ORDER BY ride_id")
    assert native == memory
    assert [row["ride_id"] for row in native] == [1, 3, 4, 5, 7]

    empty = catalog.create_table("nyc.empty", schema=BATCH_1.schema)
    native, memory = _both_paths(empty, "SELECT * FROM t", row_filter="fare > 1")
    assert native == memory == []


@requires_iceberg_extension
def test_native_matches_on_a_partitioned_table(catalog):
    schema = Schema(NestedField(1, "id", LongType()), NestedField(2, "city", StringType()),
                    NestedField(3, "ts", TimestampType()), NestedField(4, "fare", DoubleType()))
    spec = PartitionSpec(PartitionField(2, 1000, IdentityTransform(), "city"),
                         PartitionField(3, 1001, DayTransform(), "ts_day"),
                         PartitionField(1, 1002, BucketTransform(4), "id_bucket"))
    table = catalog.create_table("nyc.partitioned", schema=schema, partition_spec=spec)
    data = pa.table({"id": pa.array(range(60), pa.int64()),
                     "city": pa.array([["NYC", "BKK", "LDN"][i % 3] for i in range(60)]),
                     "ts": pa.array([dt.datetime(2026, 9, 1) + dt.timedelta(hours=7 * i) for i in range(60)],
                                    pa.timestamp("us")),
                     "fare": pa.array([float(i) for i in range(60)])})
    table.append(data)
    for row_filter in ["city = 'BKK'", "ts >= '2026-09-05T00:00:00' AND city != 'NYC'", "id IN (1, 2, 3, 40)",
                       "fare > 30 OR city = 'LDN'"]:
        native, memory = _both_paths(table, row_filter=row_filter)
        assert native == memory, row_filter
        assert native, row_filter


@requires_iceberg_extension
def test_native_row_filter_is_pushed_into_iceberg_scan(rides):
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "bkk", row_filter="city = 'BKK'")
        plan = "\n".join(engine.query("EXPLAIN SELECT count(*) AS n FROM bkk WHERE fare > 150")
                         .column("explain_value").to_pylist())
        result = engine.query("SELECT count(*) AS n, min(fare) AS lo FROM bkk").to_pylist()
    assert result == [{"n": 3, "lo": 100.0}]
    assert "ICEBERG_SCAN" in plan
    assert "city='BKK'" in plan  # the view's WHERE, pushed into the scan
    assert "fare>150.0" in plan  # and the query's own predicate


@requires_iceberg_extension
def test_native_registration_does_not_materialize(catalog, monkeypatch):
    from pyiceberg.table import DataScan

    n = 100_000
    table = catalog.create_table("nyc.big", schema=pa.schema([("id", pa.int64()), ("v", pa.float64()),
                                                             ("s", pa.string())]))
    table.append(pa.table({"id": pa.array(range(n), pa.int64()), "v": pa.array([i / 3 for i in range(n)]),
                           "s": pa.array([f"name-{i % 997}" for i in range(n)])}))

    def no_scan(*args, **kwargs):
        raise AssertionError("the native path must not scan the table with pyiceberg")

    with DuckDBEngine() as engine:
        gc.collect()
        before = pa.total_allocated_bytes()
        with monkeypatch.context() as patch:
            for method in ("to_arrow", "to_arrow_batch_reader", "to_pandas"):
                patch.setattr(DataScan, method, no_scan)
            engine.register_iceberg(table, "big", row_filter="id >= 10")
            count = engine.query("SELECT count(*) AS n, sum(id) AS total FROM big").to_pylist()
        native_growth = pa.total_allocated_bytes() - before

        engine.register_iceberg(table, "big_in_memory", native=False)
        memory_growth = pa.total_allocated_bytes() - before

    assert count == [{"n": n - 10, "total": sum(range(10, n))}]
    # Arrow memory stays flat natively; the in-memory path holds every row.
    assert native_growth < 64 * 1024, native_growth
    assert memory_growth > 1_000_000, memory_growth


@requires_iceberg_extension
def test_native_view_reads_data_files_at_query_time(rides):
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "lazy")
        engine.register_iceberg(rides, "eager", native=False)
        for task in rides.scan().plan_files():
            os.remove(task.file.file_path.removeprefix("file://"))
        assert engine.query("SELECT count(*) AS n FROM eager").to_pylist() == [{"n": 7}]
        with pytest.raises(duckdb.IOException):
            engine.query("SELECT sum(fare) FROM lazy")


@requires_iceberg_extension
def test_native_view_is_pinned_to_the_snapshot_at_registration(rides):
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "rides")
        rides.append(BATCH_2)
        assert engine.query("SELECT count(*) AS n FROM rides").to_pylist() == [{"n": 7}]
        engine.register_iceberg(rides, "rides")
        assert engine.query("SELECT count(*) AS n FROM rides").to_pylist() == [{"n": 10}]


def test_falls_back_to_memory_when_the_extension_is_not_installed(rides, tmp_path, caplog):
    # An empty extension folder: the iceberg extension is "not installed", and nothing is downloaded.
    extensions = tmp_path / "extensions"
    extensions.mkdir()
    connection = duckdb.connect(config={"extension_directory": str(extensions)})
    engine = DuckDBEngine(connection=connection)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        engine.register_iceberg(rides, "rides")
        engine.register_iceberg(rides, "again", row_filter="city = 'BKK'")
    assert not engine.iceberg_extension
    assert not engine.is_native("rides") and not engine.is_native("again")
    assert engine.query("SELECT count(*) AS n FROM rides").to_pylist() == [{"n": 7}]
    assert engine.query("SELECT count(*) AS n FROM again").to_pylist() == [{"n": 3}]
    warnings_ = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings_) == 1  # once per engine
    assert "in memory" in warnings_[0].getMessage() and "install_extensions" in warnings_[0].getMessage()

    with pytest.raises(EngineNotFound, match="not installed") as error:
        engine.register_iceberg(rides, "native", native=True)
    assert ICEBERG_EXTENSION_HINT in str(error.value)
    with pytest.raises(EngineNotFound, match="iceberg extension"):
        engine.attach_rest("rc", "http://127.0.0.1:9", "wh")
    engine.close()
    connection.close()
    assert list(extensions.rglob("*.duckdb_extension")) == []


@requires_iceberg_extension
def test_falls_back_when_duckdb_cannot_read_the_metadata(rides, tmp_path, caplog):
    rides.metadata_location = str(tmp_path / "gone" / "v1.metadata.json")
    with DuckDBEngine() as engine:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            engine.register_iceberg(rides, "rides")
        assert not engine.is_native("rides")
        assert engine.query("SELECT count(*) AS n FROM rides").to_pylist() == [{"n": 7}]
        assert "iceberg_scan can't read it" in caplog.text
        with pytest.raises(duckdb.IOException):
            engine.register_iceberg(rides, "native", native=True)
        assert "native" not in engine.aliases


@requires_iceberg_extension
def test_falls_back_when_the_row_filter_cannot_be_rendered(rides, monkeypatch, caplog):
    import local_data_platform.engine.duckdb as duck

    def untranslatable(*args, **kwargs):
        raise UntranslatableFilter("not expressible")

    monkeypatch.setattr(duck, "row_filter_to_sql", untranslatable)
    with DuckDBEngine() as engine:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            engine.register_iceberg(rides, "bkk", row_filter="city = 'BKK'")
        assert not engine.is_native("bkk")
        assert engine.query("SELECT count(*) AS n FROM bkk").to_pylist() == [{"n": 3}]
        assert "not expressible" in caplog.text
        with pytest.raises(UntranslatableFilter):
            engine.register_iceberg(rides, "strict", row_filter="city = 'BKK'", native=True)


@requires_iceberg_extension
def test_row_filter_errors_match_the_pyiceberg_scan(rides):
    with DuckDBEngine() as engine:
        for native in (True, False):
            with pytest.raises(ValueError, match="no_such_column"):
                engine.register_iceberg(rides, "t", row_filter="no_such_column = 1", native=native)
            # Raw DuckDB SQL is not a pyiceberg row filter on either path; it is never spliced into a view.
            with pytest.raises(Exception, match="Expected"):
                engine.register_iceberg(rides, "t", row_filter="fare * 2 > 10", native=native)
        with pytest.raises(TypeError, match="native"):
            engine.register_iceberg(rides, "t", native="yes")
        assert engine.aliases == []


@requires_iceberg_extension
def test_replacing_an_alias_across_paths_and_cleanup_on_a_passed_connection(rides):
    connection = duckdb.connect()
    connection.execute("CREATE TABLE mine AS SELECT 1 AS a")
    engine = DuckDBEngine(connection=connection)
    engine.register_iceberg(rides, "t")
    assert engine.is_native("t")
    engine.register_arrow(pa.table({"x": [1, 2]}), "t")
    assert engine.query("SELECT sum(x) AS s FROM t").to_pylist() == [{"s": 3}]
    engine.register_iceberg(rides, "t", row_filter="city = 'NYC'")
    assert engine.query("SELECT count(*) AS n FROM t").to_pylist() == [{"n": 4}]
    engine.register_iceberg(rides, "t", native=False)
    engine.register_iceberg(rides, "u")
    assert engine.aliases == ["t", "u"]
    with pytest.raises(KeyError, match="nope"):
        engine.is_native("nope")

    engine.close()

    assert connection.execute("SELECT a FROM mine").fetchall() == [(1,)]
    for alias in ("t", "u"):
        with pytest.raises(duckdb.CatalogException):
            connection.execute(f"SELECT * FROM {alias}")
    connection.close()


@requires_iceberg_extension
def test_snapshots_and_files_agree_on_both_paths(rides):
    rides.delete("ride_id = 1")
    snapshot_ids = [snapshot.snapshot_id for snapshot in rides.snapshots()]
    with DuckDBEngine() as engine:
        engine.register_iceberg(rides, "native", native=True)
        engine.register_iceberg(rides, "memory", native=False)
        engine.register_iceberg(rides, "old", snapshot_id=snapshot_ids[0], native=True)
        engine.register_iceberg(rides, "old_memory", snapshot_id=snapshot_ids[0], native=False)
        snapshots = {alias: engine.snapshots(alias) for alias in ("native", "memory")}
        files = {alias: engine.files(alias) for alias in ("native", "memory", "old", "old_memory")}
        engine.register_arrow(pa.table({"a": [1]}), "arrow")
        with pytest.raises(ValueError, match="not an Iceberg table"):
            engine.snapshots("arrow")
        with pytest.raises(KeyError):
            engine.files("missing")

    for table in snapshots.values():
        assert table.column("snapshot_id").to_pylist() == snapshot_ids
        assert table.column("operation").to_pylist() == ["append", "append", "overwrite"]
        assert table.schema.names == ["sequence_number", "snapshot_id", "timestamp_ms", "manifest_list", "operation"]
    assert snapshots["native"].column("manifest_list").to_pylist() == \
        snapshots["memory"].column("manifest_list").to_pylist()

    def entries(table):
        return sorted(zip(table.column("file_path").to_pylist(), table.column("status").to_pylist(),
                          table.column("record_count").to_pylist()))

    assert entries(files["native"]) == entries(files["memory"])
    assert "DELETED" in files["memory"].column("status").to_pylist()  # the rewritten file, as DuckDB lists it
    assert entries(files["old"]) == entries(files["old_memory"])
    assert len(files["old"]) == 1
    assert files["memory"].schema.names == files["native"].schema.names


# ---------------------------------------------------------------------------------------------------
# attach_rest
# ---------------------------------------------------------------------------------------------------

class RecordingConnection:
    """A stand-in DuckDB connection that records SQL and reports the iceberg extension as loaded."""

    def __init__(self, fail_on: str | None = None):
        self.statements: list[str] = []
        self.fail_on = fail_on

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise duckdb.IOException("simulated failure")
        return self

    def fetchone(self):
        return (True, True)

    def unregister(self, name):
        self.statements.append(f"UNREGISTER {name}")


def test_attach_rest_builds_a_secret_and_attach_statement(monkeypatch, caplog):
    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "t0k'en-value")
    connection = RecordingConnection()
    engine = DuckDBEngine(connection=connection)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        engine.attach_rest("lake", "http://localhost:8181", "warehouse", token_env="LDP_TEST_REST_TOKEN",
                           options={"access_delegation_mode": "none", "support_nested_namespaces": True})
    secret, attach = connection.statements[-2:]
    assert secret == ("CREATE OR REPLACE TEMPORARY SECRET \"ldp_rest_lake\" "
                      "(TYPE iceberg, TOKEN 't0k''en-value')")
    assert attach == ("ATTACH 'warehouse' AS \"lake\" (TYPE iceberg, ENDPOINT 'http://localhost:8181', "
                      "SECRET \"ldp_rest_lake\", ACCESS_DELEGATION_MODE 'none', SUPPORT_NESTED_NAMESPACES true)")
    assert "t0k" not in caplog.text and "LDP_TEST_REST_TOKEN" in caplog.text
    assert "t0k" not in repr(engine)

    engine.close()
    assert connection.statements[-2:] == ['DETACH DATABASE IF EXISTS "lake"',
                                          'DROP TEMPORARY SECRET IF EXISTS "ldp_rest_lake"']


def test_attach_rest_without_a_token_and_bad_inputs(monkeypatch):
    connection = RecordingConnection()
    engine = DuckDBEngine(connection=connection)
    engine.attach_rest("open", "http://localhost:8181", "wh")
    assert connection.statements[-1] == ("ATTACH 'wh' AS \"open\" (TYPE iceberg, ENDPOINT 'http://localhost:8181', "
                                         "AUTHORIZATION_TYPE 'none')")
    monkeypatch.delenv("LDP_TEST_UNSET_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="LDP_TEST_UNSET_TOKEN"):
        engine.attach_rest("x", "http://localhost:8181", "wh", token_env="LDP_TEST_UNSET_TOKEN")
    with pytest.raises(ConfigError, match="option name"):
        engine.attach_rest("x", "http://localhost:8181", "wh", options={"BAD OPTION); DROP": "x"})
    with pytest.raises(ValueError, match="alias"):
        engine.attach_rest("", "http://localhost:8181", "wh")


def test_attach_rest_drops_the_secret_when_attach_fails(monkeypatch):
    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "abc")
    connection = RecordingConnection(fail_on="ATTACH")
    engine = DuckDBEngine(connection=connection)
    with pytest.raises(duckdb.IOException):
        engine.attach_rest("lake", "http://localhost:8181", "wh", token_env="LDP_TEST_REST_TOKEN")
    assert connection.statements[-1] == 'DROP TEMPORARY SECRET IF EXISTS "ldp_rest_lake"'
    engine.close()
    assert "DETACH" not in " ".join(connection.statements)


@requires_iceberg_extension
def test_attach_rest_sql_is_accepted_by_duckdb(monkeypatch):
    # Offline: autoinstall is off, and port 9 on localhost refuses connections. DuckDB validates the
    # ATTACH options before it needs httpfs or the network, so an error here that isn't about the
    # options or the syntax proves the statement is well formed.
    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "abc")
    connection = duckdb.connect(config={"autoinstall_known_extensions": False})
    engine = DuckDBEngine(connection=connection)
    for token_env in (None, "LDP_TEST_REST_TOKEN"):
        with pytest.raises(duckdb.Error) as error:
            engine.attach_rest("lake", "http://127.0.0.1:9", "wh", token_env=token_env,
                               options={"ACCESS_DELEGATION_MODE": "none"})
        assert not isinstance(error.value, (duckdb.ParserException, duckdb.BinderException))
        assert "Unhandled options" not in str(error.value)
    assert connection.execute("SELECT count(*) FROM duckdb_secrets()").fetchone() == (0,)
    with pytest.raises(duckdb.Error, match="Unhandled options"):
        engine.attach_rest("lake", "http://127.0.0.1:9", "wh", options={"NOT_AN_OPTION": "x"})
    engine.close()
    connection.close()


@pytest.mark.rest
@pytest.mark.skipif(not os.environ.get("LDP_REST_URI"),
                    reason="REST integration test: set LDP_REST_URI (and optionally LDP_REST_WAREHOUSE) to a running "
                           "Iceberg REST catalog, e.g. tools/rest_fixture; DuckDB also needs its httpfs extension "
                           "(LDP_DUCKDB_EXTENSION_DIR picks the extension folder)")
def test_attach_rest_queries_a_live_catalog(monkeypatch):
    from pyiceberg.catalog import load_catalog

    uri, warehouse = os.environ["LDP_REST_URI"], os.environ.get("LDP_REST_WAREHOUSE", "")
    catalog = load_catalog("rest", type="rest", uri=uri, **({"warehouse": warehouse} if warehouse else {}))
    catalog.create_namespace_if_not_exists("ldp_c5")
    identifier = "ldp_c5.attach_rest_rides"
    if catalog.table_exists(identifier):
        catalog.drop_table(identifier)
    table = catalog.create_table(identifier, schema=BATCH_1.schema)
    table.append(BATCH_1)
    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "not-a-real-token")  # the fixture ignores tokens
    extension_dir = os.environ.get("LDP_DUCKDB_EXTENSION_DIR")
    connection = duckdb.connect(config={"extension_directory": extension_dir} if extension_dir else {})
    try:
        for token_env in (None, "LDP_TEST_REST_TOKEN"):
            engine = DuckDBEngine(connection=connection)
            engine.attach_rest("lake", uri, warehouse, token_env=token_env, options={"ACCESS_DELEGATION_MODE": "none"})
            rows = engine.query("SELECT count(*) AS n, sum(fare) AS total FROM lake.ldp_c5.attach_rest_rides")
            assert rows.to_pylist() == [{"n": 4, "total": 50.0}]
            engine.register_iceberg(catalog.load_table(identifier), "rides")
            assert engine.query("SELECT count(*) AS n FROM rides").to_pylist() == [{"n": 4}]
            engine.close()
            assert ("lake",) not in connection.execute("SELECT database_name FROM duckdb_databases()").fetchall()
            assert connection.execute("SELECT count(*) FROM duckdb_secrets()").fetchone() == (0,)
    finally:
        connection.close()
        catalog.drop_table(identifier)


# ---------------------------------------------------------------------------------------------------
# row_filter_to_sql
# ---------------------------------------------------------------------------------------------------

FILTER_SCHEMA = Schema(
    NestedField(1, "id", LongType()), NestedField(2, "city", StringType()), NestedField(3, "fare", DoubleType()),
    NestedField(4, "small", FloatType()), NestedField(5, "paid", BooleanType()), NestedField(6, "day", DateType()),
    NestedField(7, "ts", TimestampType()), NestedField(8, "tstz", TimestamptzType()),
    NestedField(9, "amount", DecimalType(10, 2)),
    NestedField(10, "loc", StructType(NestedField(11, "lat", DoubleType()), NestedField(12, "zone", StringType()))),
    NestedField(13, "code", BinaryType()), NestedField(14, "tags", ListType(15, StringType())),
    NestedField(16, "odd name", StringType()),
)


@pytest.mark.parametrize("row_filter, sql", [
    ("city = 'NYC'", '("_ldp_scan"."city" = \'NYC\')'),
    ("city NOT IN ('NYC', 'BKK')",
     '("_ldp_scan"."city" IS NULL OR "_ldp_scan"."city" NOT IN (\'BKK\', \'NYC\'))'),
    ("id IN (3, 1)", '("_ldp_scan"."id" IS NOT NULL AND "_ldp_scan"."id" IN (1, 3))'),
    ("city LIKE 'N%'", 'starts_with("_ldp_scan"."city", \'N\')'),
    ("fare > 10", '(("_ldp_scan"."fare" > CAST(\'10.0\' AS DOUBLE)) AND NOT isnan("_ldp_scan"."fare"))'),
    ("fare IS NOT NAN", '(NOT isnan("_ldp_scan"."fare"))'),
    ("small = 0.1", '("_ldp_scan"."small" = CAST(\'0.1\' AS FLOAT))'),
    ("paid = true", '("_ldp_scan"."paid" = TRUE)'),
    ("day = '2026-09-02'", '("_ldp_scan"."day" = CAST(\'2026-09-02\' AS DATE))'),
    ("ts >= '2026-09-01T11:00:00.5'", '("_ldp_scan"."ts" >= CAST(\'2026-09-01 11:00:00.500000\' AS TIMESTAMP))'),
    ("tstz < '2026-09-01T12:00:00+02:00'",
     '("_ldp_scan"."tstz" < CAST(\'2026-09-01 10:00:00+00:00\' AS TIMESTAMPTZ))'),
    ("amount > 12", '("_ldp_scan"."amount" > CAST(\'12.00\' AS DECIMAL(10, 2)))'),
    ("loc.zone = 'north'", '("_ldp_scan"."loc"."zone" = \'north\')'),
    ("city = 'it''s' OR NOT (id = 1)", '(("_ldp_scan"."city" = \'it\'\'s\') OR (NOT ("_ldp_scan"."id" = 1)))'),
    (IsNull("odd name"), '("_ldp_scan"."odd name" IS NULL)'),
    (EqualTo("code", b"\x00\xff"), '("_ldp_scan"."code" = from_hex(\'00ff\'))'),
    ("true", None),
    ("false", "FALSE"),
])
def test_row_filter_to_sql(row_filter, sql):
    assert row_filter_to_sql(row_filter, FILTER_SCHEMA) == sql


def test_row_filter_to_sql_quotes_values_and_can_skip_the_relation():
    injected = "x'); DROP TABLE t; --"
    sql = row_filter_to_sql(EqualTo("city", injected), FILTER_SCHEMA, relation=None)
    assert sql == "(\"city\" = 'x''); DROP TABLE t; --')"
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t AS SELECT 'x''); DROP TABLE t; --' AS city UNION ALL SELECT 'y'")
    assert connection.execute(f"SELECT count(*) FROM t WHERE {sql}").fetchone() == (1,)
    connection.close()


def test_row_filter_to_sql_rejects_what_it_cannot_render():
    with pytest.raises(ValueError, match="accessor"):
        row_filter_to_sql("tags.element = 'a'", FILTER_SCHEMA)  # pyiceberg's own bind error
    with pytest.raises(ValueError, match="Could not find field"):
        row_filter_to_sql("nope = 1", FILTER_SCHEMA)
    with pytest.raises(TypeError, match="row_filter"):
        row_filter_to_sql(42, FILTER_SCHEMA)
    nanos = getattr(iceberg_types, "TimestampNanoType", None)  # Iceberg v3, pyiceberg >= 0.10
    if nanos is not None:
        with pytest.raises(UntranslatableFilter):
            literal_sql(1, nanos())
    with pytest.raises(UntranslatableFilter, match="NaN"):
        literal_sql(float("nan"), DoubleType())
    assert issubclass(UntranslatableFilter, ValueError)
