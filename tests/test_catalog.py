"""Tests for LocalIcebergCatalog."""

import subprocess
import sys

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchNamespaceError, NoSuchTableError

from local_data_platform.catalog import Catalog
from local_data_platform.catalog.local import LocalCatalog
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog


def test_creates_missing_warehouse_folder(tmp_path):
    warehouse = tmp_path / "does" / "not" / "exist"

    catalog = LocalIcebergCatalog("demo", str(warehouse))

    assert warehouse.is_dir()
    assert (warehouse / "demo_catalog.db").is_file()
    assert catalog.warehouse_path == warehouse.resolve()
    assert isinstance(catalog, SqlCatalog)


def test_uri_and_warehouse_are_absolute(tmp_path):
    catalog = LocalIcebergCatalog("demo", tmp_path)
    root = tmp_path.resolve()

    assert catalog.uri == f"sqlite:///{root}/demo_catalog.db"
    assert catalog.uri.startswith("sqlite:////")
    assert catalog.warehouse == f"file://{root}"
    assert catalog.properties["uri"] == catalog.uri
    assert catalog.properties["warehouse"] == catalog.warehouse


def test_relative_path_resolves_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    catalog = LocalIcebergCatalog("demo", "warehouse")

    assert catalog.warehouse_path == (tmp_path / "warehouse").resolve()
    assert catalog.uri == f"sqlite:///{(tmp_path / 'warehouse').resolve()}/demo_catalog.db"


def test_catalog_is_independent_of_cwd_after_creation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = LocalIcebergCatalog("demo", "warehouse")
    catalog.create_namespace("ns")
    catalog.create_table("ns.t", schema=pa.schema([("id", pa.int64())])).append(pa.table({"id": [1, 2]}))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    reopened = LocalIcebergCatalog("demo", tmp_path / "warehouse")

    assert reopened.load_table("ns.t").scan().to_arrow().num_rows == 2
    assert not (elsewhere / "warehouse").exists()


def test_get_dbs_and_get_tables(tmp_path):
    catalog = LocalIcebergCatalog("demo", tmp_path)
    catalog.create_namespace("ns")
    catalog.create_table("ns.rides", schema=pa.schema([("id", pa.int64())]))

    assert catalog.get_dbs() == [("ns",)]
    assert catalog.get_tables("ns") == [("ns", "rides")]


def test_errors_keep_their_original_type(tmp_path):
    catalog = LocalIcebergCatalog("demo", tmp_path)
    catalog.create_namespace("ns")

    with pytest.raises(NamespaceAlreadyExistsError):
        catalog.create_namespace("ns")
    with pytest.raises(NoSuchTableError):
        catalog.load_table("ns.missing")
    with pytest.raises(NoSuchNamespaceError):
        catalog.get_tables("missing")


def test_unwritable_path_raises_original_os_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")

    with pytest.raises(OSError):
        LocalIcebergCatalog("demo", blocker / "warehouse")


def test_extra_properties_are_passed_to_sql_catalog(tmp_path):
    catalog = LocalIcebergCatalog("demo", tmp_path, echo="false", custom="value")

    assert catalog.properties["custom"] == "value"
    assert catalog.properties["echo"] == "false"


def test_path_with_spaces_writes_data_under_that_path(tmp_path):
    warehouse = tmp_path / "my warehouse"
    catalog = LocalIcebergCatalog("demo", warehouse)
    catalog.create_namespace("ns")
    table = catalog.create_table("ns.t", schema=pa.schema([("id", pa.int64())]))

    table.append(pa.table({"id": [1, 2, 3]}))

    assert catalog.load_table("ns.t").scan().to_arrow().num_rows == 3
    assert list(warehouse.rglob("*.parquet"))


@pytest.mark.parametrize("name, path", [("", "wh"), ("demo", ""), ("demo", None)])
def test_rejects_empty_name_or_path(name, path):
    with pytest.raises(ValueError):
        LocalIcebergCatalog(name, path)


def test_base_catalog_classes_are_importable():
    assert issubclass(LocalCatalog, Catalog)


def test_database_path_names_the_sqlite_file_without_creating_it(tmp_path):
    path = LocalIcebergCatalog.database_path("demo", tmp_path / "wh")
    assert path == (tmp_path / "wh").resolve() / "demo_catalog.db"
    assert not (tmp_path / "wh").exists()
    assert LocalIcebergCatalog("demo", tmp_path / "wh").uri == f"sqlite:///{path}"


def test_close_releases_connections_and_the_catalog_reconnects(tmp_path):
    with LocalIcebergCatalog("demo", tmp_path) as catalog:
        catalog.create_namespace("ns")
    assert ("ns",) in catalog.get_dbs()
    catalog.close()


def test_no_unclosed_database_resource_warning(tmp_path):
    """Pooled SQLite connections are closed when the catalog is garbage collected."""
    script = (
        "import gc, pyarrow as pa\n"
        "from local_data_platform.format.iceberg import Iceberg\n"
        f"table = Iceberg('rides', {{'identifier': 'ns', 'warehouse_path': {str(tmp_path / 'wh')!r}}})\n"
        "table.put(pa.table({'id': [1, 2, 3]}))\n"
        "assert table.row_count() == 3\n"
        "del table\n"
        "gc.collect()\n"
    )
    done = subprocess.run([sys.executable, "-X", "dev", "-W", "default::ResourceWarning", "-c", script],
                          capture_output=True, text=True, check=True)
    assert "unclosed database" not in done.stderr, done.stderr
