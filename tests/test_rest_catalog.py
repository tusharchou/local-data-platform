"""The ``rest`` catalog type against the real Apache Iceberg REST catalog server.

Opt-in: set ``LDP_RUN_REST=1``. The module starts ``tools/rest_fixture`` (Iceberg's
``RESTCatalogServer`` from ``org.apache.iceberg:iceberg-open-api:<ver>:test-fixtures``) with
scala-cli on a free port, backed by a SQLite ``JdbcCatalog`` and a local file warehouse under
``tmp_path``. The first run downloads a JDK and the jars, which can take a few minutes; set
``LDP_REST_STARTUP_TIMEOUT`` (seconds, default 900) to wait longer. ``LDP_SCALA_CLI`` points at a
``scala-cli`` binary that is not on ``PATH``.
"""

import inspect
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform.catalog.provider import catalog_namespace, create_catalog

pytestmark = [
    pytest.mark.rest,
    pytest.mark.skipif(os.environ.get("LDP_RUN_REST") != "1",
                       reason="REST catalog tests are opt-in: set LDP_RUN_REST=1 (needs scala-cli; "
                              "the first run downloads a JDK and the Iceberg jars)"),
]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tools" / "rest_fixture"
SCALA_CLI_LOCATIONS = (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"))


def _scala_cli() -> str:
    explicit = os.environ.get("LDP_SCALA_CLI")
    if explicit:
        return explicit
    found = shutil.which("scala-cli")
    if found:
        return found
    for folder in SCALA_CLI_LOCATIONS:
        candidate = folder / "scala-cli"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    pytest.skip("scala-cli not found; install it (https://scala-cli.virtuslab.org/install) or set LDP_SCALA_CLI")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=30)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)


@pytest.fixture(scope="module")
def rest_server(tmp_path_factory):
    """The Iceberg REST fixture on a free port; yields ``{"uri", "warehouse", "startup_s"}``."""
    scala_cli = _scala_cli()
    root = tmp_path_factory.mktemp("rest_fixture")
    warehouse = root / "warehouse"
    port = _free_port()
    log_path = root / "fixture.log"
    command = [scala_cli, "run", str(FIXTURE_DIR), "-q", "--suppress-outdated-dependency-warning", "--",
               "--port", str(port), "--warehouse", str(warehouse)]
    timeout = float(os.environ.get("LDP_REST_STARTUP_TIMEOUT", "900"))
    uri = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    with open(log_path, "w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        while True:
            if process.poll() is not None:
                pytest.fail(f"the REST fixture exited with {process.returncode}:\n{log_path.read_text()[-4000:]}")
            try:
                with urllib.request.urlopen(f"{uri}/v1/config", timeout=2) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            if time.monotonic() - started > timeout:
                pytest.fail(f"the REST fixture did not answer on {uri} within {timeout:.0f}s:\n"
                            f"{log_path.read_text()[-4000:]}")
            time.sleep(0.5)
        yield {"uri": uri, "warehouse": warehouse, "startup_s": round(time.monotonic() - started, 1)}
    finally:
        _stop(process)


@pytest.fixture
def rest_spec(rest_server, monkeypatch):
    """A ``rest`` catalog spec for the fixture. The fixture ignores the token; it exercises ``token_env``."""
    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "not-a-real-token")
    return {"type": "rest", "uri": rest_server["uri"], "name": "ldp_rest", "namespace": "ldp_rest_demo",
            "token_env": "LDP_TEST_REST_TOKEN"}


def test_rest_catalog_with_pyiceberg_directly(rest_server, rest_spec):
    from pyiceberg.catalog.rest import RestCatalog

    catalog = create_catalog(rest_spec)
    namespace = catalog_namespace(rest_spec)
    catalog.create_namespace_if_not_exists(namespace)
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "city": pa.array(["NYC", "BKK", "LDN"])})
    table = catalog.create_table(f"{namespace}.direct", schema=rows.schema)
    table.append(rows)

    assert isinstance(catalog, RestCatalog)
    assert (namespace,) in catalog.list_namespaces()
    fresh = create_catalog(rest_spec).load_table(f"{namespace}.direct")
    assert fresh.scan().to_arrow().sort_by("id").equals(rows)
    assert fresh.current_snapshot().summary.additional_properties["total-records"] == "3"
    # The server wrote the table metadata into its local file warehouse.
    metadata = Path(fresh.metadata_location.removeprefix("file:"))
    assert metadata.is_file() and rest_server["warehouse"].resolve() in metadata.resolve().parents


def test_rest_catalog_through_the_iceberg_format(rest_server, rest_spec, sample_table):
    from local_data_platform.format.iceberg import Iceberg

    if "catalog_obj" not in inspect.signature(Iceberg.__init__).parameters:
        pytest.skip("Iceberg(..., catalog_obj=) is not available yet (contract C1/C3)")

    table = Iceberg("rides", rest_spec, write_mode="append")
    first = table.put(sample_table)
    second = Iceberg("rides", rest_spec, catalog_obj=create_catalog(rest_spec)).put(sample_table)

    assert table.identifier == "ldp_rest_demo.rides"
    assert (first.rows_before, first.rows_after) == (0, 6)
    assert (second.rows_before, second.rows_after) == (6, 12)
    df = Iceberg("rides", rest_spec).get()
    assert df.num_rows == 12
    assert sorted(set(df.column("ride_id").to_pylist())) == sorted(sample_table.column("ride_id").to_pylist())
    assert len(table.snapshots()) == 2

    replaced = table.put(sample_table.slice(0, 2), mode="overwrite")
    assert (replaced.rows_before, replaced.rows_after) == (12, 2)
    assert Iceberg("rides", rest_spec).row_count() == 2
