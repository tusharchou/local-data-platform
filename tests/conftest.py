"""Shared pytest fixtures.

Every fixture is deterministic and offline, and writes only under ``tmp_path``.

Fixtures:
    make_table: A factory ``make_table(n=6, start_id=1, fare_offset=0.0, tz=None)``
        that builds a small rides table.
    sample_table: ``make_table()``, six rides with a naive ``timestamp[ns]`` column.
    sample_table_tz: ``make_table(tz="Asia/Bangkok")``, the same rides with a tz-aware
        ``timestamp[ns, tz=Asia/Bangkok]`` column.
    catalog_config: An Iceberg catalog config dict whose warehouse is under ``tmp_path``.

Tests marked ``dbt`` need the ``dbt`` extra (dbt-core and the dbt-duckdb adapter). They skip when
dbt cannot be imported, so ``make test`` passes on a plain install, and the ``dbt`` CI job runs
them with ``pytest -m dbt``.
"""

import datetime as dt
import importlib.util
import os
import socket
import threading

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


def dbt_is_installed() -> bool:
    """Whether dbt-core and the dbt-duckdb adapter can be imported, which is what the ``dbt`` extra installs."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("dbt.cli.main", "dbt.adapters.duckdb"))
    except ModuleNotFoundError:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip every test marked ``dbt`` when the dbt extra is not installed."""
    if dbt_is_installed():
        return
    skip = pytest.mark.skip(reason="needs the dbt extra: pip install \"local-data-platform[dbt]\"")
    for item in items:
        if "dbt" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def network_trap():
    """A fake HTTP proxy on localhost. Yields ``(environment, connections)``.

    Run a subprocess with ``environment`` and ``connections`` gets one item per connection it opens,
    so an empty list means it made no network request that honours the proxy variables (requests,
    urllib and httpx do). The ``dbt`` tests use it to prove that dbt runs offline.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    connections: list[bytes] = []

    def serve() -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return  # the fixture closed the socket
            connections.append(client.recv(200))
            client.close()

    threading.Thread(target=serve, daemon=True).start()
    proxy = f"http://127.0.0.1:{server.getsockname()[1]}"
    env = dict(os.environ, NO_PROXY="", no_proxy="")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[name] = proxy
    try:
        yield env, connections
    finally:
        server.close()


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
