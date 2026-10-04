"""Shared pytest fixtures.

Every fixture is deterministic and offline, and writes only under ``tmp_path``.

Fixtures:
    make_table: A factory ``make_table(n=6, start_id=1, fare_offset=0.0, tz=None)``
        that builds a small rides table.
    sample_table: ``make_table()``, six rides with a naive ``timestamp[ns]`` column.
    sample_table_tz: ``make_table(tz="Asia/Bangkok")``, the same rides with a tz-aware
        ``timestamp[ns, tz=Asia/Bangkok]`` column.
    catalog_config: An Iceberg catalog config dict whose warehouse is under ``tmp_path``.
"""

import datetime as dt

import pyarrow as pa
import pytest

CITIES = ("NYC", "BKK", "LDN")
BASE_TS = dt.datetime(2024, 1, 1, 8, 0, 0)
BASE_TS_NS = int(BASE_TS.replace(tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000


def build_rides(n: int = 6, start_id: int = 1, fare_offset: float = 0.0, tz: str | None = None) -> pa.Table:
    """Build a deterministic rides table.

    Columns: ``ride_id`` (int64), ``city`` (string), ``fare`` (float64) and
    ``pickup_ts`` (``timestamp[ns]``, or ``timestamp[ns, tz]`` when ``tz`` is set).
    Ride ``i`` is picked up ``3 * i`` hours and ``i`` nanoseconds after 2024-01-01 08:00 UTC,
    so the default six rides span two days and every timestamp has sub-microsecond precision.

    Args:
        n: Number of rows.
        start_id: The first ``ride_id``.
        fare_offset: Added to every fare, to make changed rows for upserts.
        tz: Timezone for ``pickup_ts``; ``None`` for a naive timestamp.

    Returns:
        The table.
    """
    ids = list(range(start_id, start_id + n))
    pickup = [BASE_TS_NS + (i * 3600 * 1_000_000_000) * 3 + i for i in ids]
    return pa.table({
        "ride_id": pa.array(ids, pa.int64()),
        "city": pa.array([CITIES[i % len(CITIES)] for i in ids], pa.string()),
        "fare": pa.array([round(10.0 + i * 1.5 + fare_offset, 2) for i in ids], pa.float64()),
        "pickup_ts": pa.array(pickup, pa.timestamp("ns", tz=tz)),
    })


@pytest.fixture
def make_table():
    """Return the :func:`build_rides` factory."""
    return build_rides


@pytest.fixture
def sample_table() -> pa.Table:
    """Six rides with a naive nanosecond timestamp column."""
    return build_rides()


@pytest.fixture
def sample_table_tz() -> pa.Table:
    """Six rides with a tz-aware (Asia/Bangkok) nanosecond timestamp column."""
    return build_rides(tz="Asia/Bangkok")


@pytest.fixture
def catalog_config(tmp_path) -> dict:
    """An Iceberg catalog config ``{"identifier", "warehouse_path"}`` under ``tmp_path``."""
    return {"identifier": "test_ns", "warehouse_path": str(tmp_path / "warehouse")}
