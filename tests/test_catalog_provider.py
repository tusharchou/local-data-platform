"""Tests for ``local_data_platform.catalog.provider``: local, sql, rest and glue catalogs.

Everything is offline. ``rest`` runs against a tiny fake Iceberg REST server in this process
(the real Apache Iceberg REST fixture is ``tests/test_rest_catalog.py``, opt-in). ``glue`` runs
against a moto server on 127.0.0.1 and skips if ``moto[server]`` or ``boto3`` is missing.
"""

import argparse
import contextlib
import json
import logging
import sqlite3
import sys
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pyarrow as pa
import pytest

from local_data_platform.catalog import provider
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from local_data_platform.catalog.provider import (
    add_cli,
    catalog_database_file,
    catalog_name,
    catalog_namespace,
    catalog_type,
    create_catalog,
    redact,
    register_catalog_type,
    registered_catalog_types,
    require_catalog_database,
)
from local_data_platform.exceptions import ConfigError, EngineNotFound, TableNotFound

ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "city": pa.array(["NYC", "BKK", "LDN"])})


# ---------------------------------------------------------------------- registry and local


def test_builtin_types_are_registered():
    assert {"local", "sql", "rest", "glue"} <= set(registered_catalog_types())


def test_default_type_is_local_and_matches_0_1_1(tmp_path):
    catalog = create_catalog({"identifier": "demo", "warehouse_path": str(tmp_path / "wh")})

    assert isinstance(catalog, LocalIcebergCatalog)
    assert catalog.name == "demo"
    assert catalog.uri == f"sqlite:///{(tmp_path / 'wh').resolve()}/demo_catalog.db"


@pytest.mark.parametrize("kind", ["LocalIceberg", "local_iceberg", "LOCAL", "local-iceberg", None, ""])
def test_local_aliases(tmp_path, kind):
    spec = {"type": kind, "identifier": "demo", "warehouse_path": str(tmp_path / "wh")}

    assert catalog_type(spec) == "local"
    assert isinstance(create_catalog(spec), LocalIcebergCatalog)


def test_local_relative_warehouse_resolves_against_base_dir(tmp_path):
    catalog = create_catalog({"identifier": "demo", "warehouse_path": "wh"}, base_dir=tmp_path)

    assert catalog.warehouse_path == (tmp_path / "wh").resolve()


@pytest.mark.parametrize("missing", ["identifier", "warehouse_path"])
def test_local_spec_needs_both_keys(tmp_path, missing):
    spec = {"identifier": "demo", "warehouse_path": str(tmp_path)}
    del spec[missing]

    with pytest.raises(ConfigError, match=missing):
        create_catalog(spec)


def test_unknown_type_and_bad_spec_are_config_errors():
    with pytest.raises(ConfigError, match="unknown catalog type 'hive'"):
        create_catalog({"type": "hive"})
    with pytest.raises(ConfigError, match="must be an object"):
        create_catalog(["local"])


def test_register_catalog_type_adds_a_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "_REGISTRY", dict(provider._REGISTRY))
    seen = {}

    @register_catalog_type("Memory")
    def factory(spec, base_dir):
        seen.update(spec=spec, base_dir=base_dir)
        return "a catalog"

    assert create_catalog({"type": "memory", "x": 1}, base_dir=str(tmp_path)) == "a catalog"
    assert seen == {"spec": {"type": "memory", "x": 1}, "base_dir": tmp_path}
    assert "memory" in registered_catalog_types()
    with pytest.raises(ValueError):
        register_catalog_type(" ")


@pytest.mark.parametrize("spec, namespace", [
    ({"identifier": "ns"}, "ns"),
    ({"identifier": "ns", "namespace": "other"}, "ns"),
    ({"type": "sql", "namespace": "a", "identifier": "b"}, "a"),
    ({"type": "rest", "identifier": "b"}, "b"),
    ({"type": "glue", "namespace": "g"}, "g"),
])
def test_catalog_namespace(spec, namespace):
    assert catalog_namespace(spec) == namespace


def test_catalog_namespace_needs_a_key():
    with pytest.raises(ConfigError, match="namespace"):
        catalog_namespace({"type": "rest"})


@pytest.mark.parametrize("spec, name", [
    ({"identifier": "demo"}, "demo"),
    ({"type": "sql", "name": "cat", "namespace": "ns"}, "cat"),
    ({"type": "sql", "namespace": "ns"}, "ns"),
    ({"type": "sql"}, "sql"),
    ({"type": "rest", "namespace": "ns"}, "rest"),
    ({"type": "glue", "name": "g"}, "g"),
])
def test_catalog_name_defaults(spec, name):
    assert catalog_name(spec) == name


# ---------------------------------------------------------------------- sql


def test_sql_sqlite_catalog_round_trip(tmp_path):
    spec = {"type": "sql", "uri": "sqlite:///meta/catalog.db", "warehouse": "wh", "name": "shared",
            "namespace": "demo"}

    catalog = create_catalog(spec, base_dir=tmp_path)
    catalog.create_namespace(catalog_namespace(spec))
    table = catalog.create_table("demo.rides", schema=ROWS.schema)
    table.append(ROWS)

    database = (tmp_path / "meta" / "catalog.db").resolve()
    assert type(catalog).__name__ == "SqlCatalog"
    assert catalog.properties["uri"] == f"sqlite:///{database}"
    assert catalog.properties["warehouse"] == f"file://{(tmp_path / 'wh').resolve()}"
    assert catalog.load_table("demo.rides").scan().to_arrow().num_rows == 3
    assert table.metadata_location.startswith(f"file://{(tmp_path / 'wh').resolve()}/")
    # The iceberg_tables layout Java's JdbcCatalog (jdbc.schema-version=V1) reads.
    with contextlib.closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT catalog_name, table_namespace, table_name FROM iceberg_tables").fetchall()
    assert rows == [("shared", "demo", "rides")]


def test_sql_absolute_sqlite_uri_is_taken_literally(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "meta").mkdir()
    absolute = tmp_path / "abs" / "meta" / "catalog.db"

    catalog = create_catalog({"type": "sql", "uri": f"sqlite:///{absolute}", "namespace": "demo"})

    assert catalog.properties["uri"] == f"sqlite:///{absolute}"
    assert absolute.is_file()


def test_sql_catalog_opens_a_local_catalog_file(tmp_path):
    local = create_catalog({"identifier": "demo", "warehouse_path": str(tmp_path)})
    local.create_namespace("demo")
    local.create_table("demo.rides", schema=ROWS.schema).append(ROWS)

    sql = create_catalog({"type": "sql", "uri": f"sqlite:///{tmp_path}/demo_catalog.db",
                          "warehouse": str(tmp_path), "identifier": "demo"})

    assert sql.name == "demo"
    assert sql.load_table("demo.rides").scan().to_arrow().num_rows == 3


@pytest.mark.parametrize("kind", ["sql", "sqlite"])
def test_legacy_local_block_with_sql_type_still_works(tmp_path, kind):
    catalog = create_catalog({"type": kind, "identifier": "demo", "warehouse_path": str(tmp_path)})

    assert isinstance(catalog, LocalIcebergCatalog)


def test_sql_needs_a_uri():
    with pytest.raises(ConfigError, match="sql catalog spec is missing 'uri'"):
        create_catalog({"type": "sql", "warehouse": "wh"})


class _FakeSqlCatalog:
    """Stands in for pyiceberg's SqlCatalog so a Postgres URI needs no server."""

    instances: list = []

    def __init__(self, name, **properties):
        self.name, self.properties = name, properties
        self.engine = type("Engine", (), {"dispose": lambda self: None})()
        _FakeSqlCatalog.instances.append(self)


@pytest.fixture
def fake_sql(monkeypatch):
    import pyiceberg.catalog.sql

    _FakeSqlCatalog.instances = []
    monkeypatch.setattr(pyiceberg.catalog.sql, "SqlCatalog", _FakeSqlCatalog)
    return _FakeSqlCatalog


def test_sql_password_env_is_injected_and_never_logged(fake_sql, monkeypatch, caplog):
    monkeypatch.setenv("LDP_TEST_PG_PASSWORD", "pg-s3cret-value")
    spec = {"type": "sql", "uri": "postgresql+psycopg://ldp@db.internal:5432/catalog", "name": "prod",
            "password_env": "LDP_TEST_PG_PASSWORD", "warehouse": "s3://lake/warehouse"}

    with caplog.at_level(logging.DEBUG, logger="local_data_platform"):
        catalog = create_catalog(spec)

    assert catalog.properties["uri"] == "postgresql+psycopg://ldp:pg-s3cret-value@db.internal:5432/catalog"
    assert catalog.properties["warehouse"] == "s3://lake/warehouse"
    assert "pg-s3cret-value" not in caplog.text
    assert "LDP_TEST_PG_PASSWORD" not in caplog.text  # the *_env key is redacted too


def test_password_in_uri_is_rejected_without_echoing_it():
    with pytest.raises(ConfigError, match="embeds a password") as info:
        create_catalog({"type": "sql", "uri": "postgresql://ldp:hunter2@db/catalog"})

    assert "hunter2" not in str(info.value)


def test_missing_env_var_is_a_config_error(monkeypatch):
    monkeypatch.delenv("LDP_TEST_UNSET_VAR", raising=False)

    with pytest.raises(ConfigError, match="LDP_TEST_UNSET_VAR.*not set"):
        create_catalog({"type": "sql", "uri": "postgresql://ldp@db/c", "password_env": "LDP_TEST_UNSET_VAR"})


def test_properties_pass_through_and_properties_env_read_the_environment(fake_sql, monkeypatch):
    monkeypatch.setenv("LDP_TEST_S3_SECRET", "s3-s3cret")

    catalog = create_catalog({"type": "sql", "uri": "postgresql://ldp@db/c",
                              "properties": {"s3.endpoint": "http://minio:9000", "echo": "false"},
                              "properties_env": {"s3.secret-access-key": "LDP_TEST_S3_SECRET"}})

    assert catalog.properties["s3.endpoint"] == "http://minio:9000"
    assert catalog.properties["echo"] == "false"
    assert catalog.properties["s3.secret-access-key"] == "s3-s3cret"


@pytest.mark.parametrize("spec", [
    {"type": "rest", "uri": "http://x", "token": "literal"},
    {"type": "rest", "uri": "http://x", "credential": "id:secret"},
    {"type": "sql", "uri": "sqlite:///x.db", "password": "p"},
    {"type": "glue", "properties": {"glue.secret-access-key": "literal"}},
    {"type": "rest", "uri": "http://x", "properties": {"token": "literal"}},
])
def test_literal_secrets_are_rejected(spec):
    with pytest.raises(ConfigError, match="secret|password") as info:
        create_catalog(spec)

    assert "literal" not in str(info.value)


def test_redact_masks_secret_keys_and_uri_passwords():
    spec = {
        "type": "rest", "uri": "https://user:pw@catalog.example/api", "token_env": "TOKEN_VAR",
        "properties": {"s3.secret-access-key": "x", "s3.endpoint": "http://minio:9000",
                       "nested": [{"password": "p"}, "postgresql://a:b@h/db"]},
        "ClientCredential": "c",
    }

    assert redact(spec) == {
        "type": "rest", "uri": "https://user:***@catalog.example/api", "token_env": "***",
        "properties": {"s3.secret-access-key": "***", "s3.endpoint": "http://minio:9000",
                       "nested": [{"password": "***"}, "postgresql://a:***@h/db"]},
        "ClientCredential": "***",
    }
    assert spec["token_env"] == "TOKEN_VAR"  # the input is not modified


# ---------------------------------------------------------------------- rest (fake server)


class _FakeRestHandler(BaseHTTPRequestHandler):
    """Answers the few Iceberg REST calls ``RestCatalog`` makes to start and list namespaces."""

    def log_message(self, *args):  # keep test output quiet
        pass

    def _reply(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self) -> urllib.parse.SplitResult:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        self.server.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                     "body": body})
        return urllib.parse.urlsplit(self.path)

    def do_GET(self):  # noqa: N802 - http.server naming
        url = self._record()
        if url.path == "/v1/config":
            self._reply({"defaults": {}, "overrides": {}})
        elif url.path == "/v1/namespaces":
            self._reply({"namespaces": [["demo"], ["other"]]})
        elif url.path == "/v1/namespaces/demo/tables":
            self._reply({"identifiers": [{"namespace": ["demo"], "name": "rides"}]})
        else:
            self._reply({"error": {"message": "not found", "type": "NoSuchTableException", "code": 404}}, 404)

    def do_POST(self):  # noqa: N802 - http.server naming
        url = self._record()
        if url.path == "/v1/oauth/tokens":
            self._reply({"access_token": "exchanged-access-token", "token_type": "bearer", "expires_in": 3600,
                         "issued_token_type": "urn:ietf:params:oauth:token-type:access_token"})
        else:
            self._reply({"error": {"message": "unsupported", "type": "BadRequestException", "code": 400}}, 400)


@pytest.fixture
def fake_rest():
    """A fake Iceberg REST server on a free port of 127.0.0.1; ``.requests`` records every call."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRestHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _uri(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def test_rest_uses_the_token_from_its_env_var_and_never_logs_it(fake_rest, monkeypatch, caplog):
    from pyiceberg.catalog.rest import RestCatalog

    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "tok-s3cr3t-123")
    spec = {"type": "rest", "uri": _uri(fake_rest), "namespace": "demo", "token_env": "LDP_TEST_REST_TOKEN"}

    with caplog.at_level(logging.DEBUG):
        catalog = create_catalog(spec)
        namespaces = catalog.list_namespaces()

    assert isinstance(catalog, RestCatalog)
    assert catalog.name == "rest"
    assert namespaces == [("demo",), ("other",)]
    listing = [r for r in fake_rest.requests if r["path"] == "/v1/namespaces"][0]
    assert listing["headers"]["Authorization"] == "Bearer tok-s3cr3t-123"
    assert "tok-s3cr3t-123" not in caplog.text
    assert "tok-s3cr3t-123" not in repr(catalog)
    assert "tok-s3cr3t-123" not in json.dumps(redact(spec))


def test_rest_credential_env_runs_the_client_credentials_flow(fake_rest, monkeypatch, caplog):
    monkeypatch.setenv("LDP_TEST_REST_CREDENTIAL", "client-id:client-s3cret")
    uri = _uri(fake_rest)
    spec = {"type": "rest", "uri": uri, "name": "lake", "credential_env": "LDP_TEST_REST_CREDENTIAL",
            "properties": {"oauth2-server-uri": f"{uri}/v1/oauth/tokens"}}

    with caplog.at_level(logging.DEBUG):
        catalog = create_catalog(spec)
        catalog.list_namespaces()

    token_call = [r for r in fake_rest.requests if r["path"] == "/v1/oauth/tokens"][0]
    form = urllib.parse.parse_qs(token_call["body"])
    assert form["client_id"] == ["client-id"] and form["client_secret"] == ["client-s3cret"]
    listing = [r for r in fake_rest.requests if r["path"] == "/v1/namespaces"][-1]
    assert listing["headers"]["Authorization"] == "Bearer exchanged-access-token"
    assert catalog.name == "lake"
    assert "client-s3cret" not in caplog.text


def test_rest_passes_warehouse_and_properties_through(fake_rest):
    catalog = create_catalog({"type": "rest", "uri": _uri(fake_rest), "warehouse": "my_catalog",
                              "properties": {"header.X-Ldp-Test": "yes"}})
    catalog.list_namespaces()

    config_call = fake_rest.requests[0]
    assert config_call["path"] == "/v1/config?warehouse=my_catalog"
    assert fake_rest.requests[-1]["headers"]["X-Ldp-Test"] == "yes"


def test_rest_needs_a_uri_and_a_set_token_variable(monkeypatch):
    monkeypatch.delenv("LDP_TEST_REST_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="rest catalog spec is missing 'uri'"):
        create_catalog({"type": "rest"})
    with pytest.raises(ConfigError, match="LDP_TEST_REST_TOKEN"):
        create_catalog({"type": "rest", "uri": "http://127.0.0.1:9", "token_env": "LDP_TEST_REST_TOKEN"})


# ---------------------------------------------------------------------- glue


def test_glue_without_boto3_raises_engine_not_found(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.delitem(sys.modules, "pyiceberg.catalog.glue", raising=False)

    with pytest.raises(EngineNotFound, match=r"local-data-platform\[glue\]"):
        create_catalog({"type": "glue", "warehouse": "s3://lake/wh"})


@pytest.fixture
def moto_endpoint(monkeypatch, tmp_path):
    """A moto server on a free port of 127.0.0.1, with the AWS environment pointing at it."""
    server_module = pytest.importorskip("moto.server", reason="moto[server] is not installed")
    pytest.importorskip("boto3", reason="boto3 is not installed")
    for name in ("AWS_ENDPOINT_URL_S3", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                        "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1",
                        "AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": str(tmp_path / "no-config"),
                        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-credentials")}.items():
        monkeypatch.setenv(name, value)
    werkzeug = logging.getLogger("werkzeug")
    level = werkzeug.level
    werkzeug.setLevel(logging.ERROR)
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
    try:
        yield endpoint
    finally:
        server.stop()
        werkzeug.setLevel(level)


def test_glue_catalog_round_trip_on_moto(moto_endpoint):
    import boto3
    from pyiceberg.catalog.glue import GlueCatalog

    bucket = f"ldp-glue-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1").create_bucket(Bucket=bucket)
    spec = {"type": "glue", "name": "lake", "warehouse": f"s3://{bucket}/warehouse", "namespace": "demo",
            "properties": {"glue.endpoint": moto_endpoint, "glue.region": "us-east-1",
                           "s3.endpoint": moto_endpoint, "s3.region": "us-east-1"}}

    catalog = create_catalog(spec)
    catalog.create_namespace(catalog_namespace(spec))
    catalog.create_table("demo.rides", schema=ROWS.schema).append(ROWS)

    assert isinstance(catalog, GlueCatalog)
    assert catalog.name == "lake"
    assert ("demo",) in catalog.list_namespaces()
    table = catalog.load_table("demo.rides")
    assert table.scan().to_arrow().num_rows == 3
    assert table.metadata_location.startswith(f"s3://{bucket}/warehouse/")


# ---------------------------------------------------------------------- catalog database file


def test_catalog_database_file_is_the_sqlite_file_create_catalog_opens(tmp_path):
    local = {"identifier": "ns", "warehouse_path": "wh"}
    assert catalog_database_file(local, tmp_path) == LocalIcebergCatalog.database_path("ns", tmp_path / "wh")
    assert catalog_database_file({**local, "type": "LocalIceberg"}, tmp_path) == tmp_path / "wh" / "ns_catalog.db"
    assert catalog_database_file({**local, "type": "sqlite"}, tmp_path) == tmp_path / "wh" / "ns_catalog.db"
    sql = {"type": "sql", "uri": "sqlite:///lake/catalog.db?timeout=5", "namespace": "demo"}
    assert catalog_database_file(sql, tmp_path) == tmp_path / "lake" / "catalog.db"
    assert catalog_database_file({**sql, "uri": f"sqlite:///{tmp_path}/abs.db"}) == tmp_path / "abs.db"
    for spec in ({**sql, "uri": "sqlite:///:memory:"}, {**sql, "uri": "postgresql+psycopg://db.internal/ice"},
                 {"type": "rest", "uri": "http://127.0.0.1:1", "namespace": "demo"},
                 {"type": "glue", "namespace": "demo"}, {"warehouse_path": "wh"}, "not a spec"):
        assert catalog_database_file(spec, tmp_path) is None, spec
    assert not (tmp_path / "wh").exists() and not (tmp_path / "lake").exists(), "nothing is created"


def test_create_catalog_opens_exactly_the_file_catalog_database_file_names(tmp_path):
    for spec in ({"identifier": "ns", "warehouse_path": "wh"},
                 {"type": "sql", "uri": "sqlite:///lake/catalog.db", "warehouse": "lake/wh", "namespace": "demo"}):
        expected = catalog_database_file(spec, tmp_path)
        assert not expected.exists()
        create_catalog(spec, base_dir=tmp_path).create_namespace_if_not_exists("demo")
        assert expected.is_file()


def test_require_catalog_database_names_the_table_and_passes_remote_catalogs(tmp_path):
    with pytest.raises(TableNotFound, match=r"Iceberg table ns\.rides does not exist: there is no catalog at"):
        require_catalog_database({"identifier": "ns", "warehouse_path": "wh"}, tmp_path, table="rides")
    with pytest.raises(TableNotFound, match="^There is no catalog at"):
        require_catalog_database({"type": "sql", "uri": "sqlite:///c.db", "namespace": "d"}, tmp_path)
    require_catalog_database({"type": "rest", "uri": "http://127.0.0.1:1", "namespace": "d"}, tmp_path, table="t")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------- CLI


def _parse(argv):
    parser = argparse.ArgumentParser(prog="ldp")
    add_cli(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def test_cli_catalog_test_on_a_sql_spec_file(tmp_path, capsys):
    spec = {"type": "sql", "uri": "sqlite:///catalog.db", "warehouse": "wh", "namespace": "demo"}
    catalog = create_catalog(spec, base_dir=tmp_path)
    catalog.create_namespace("demo")
    catalog.create_table("demo.rides", schema=ROWS.schema)
    (tmp_path / "catalog.json").write_text(json.dumps(spec))

    args = _parse(["catalog", "test", str(tmp_path / "catalog.json")])

    assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "catalog type: sql" in out
    assert "catalog name: demo" in out
    assert "namespaces (1): demo" in out
    assert "tables in demo (1): demo.rides" in out
    assert out.rstrip().endswith("ok")


def test_cli_catalog_test_reads_a_dataset_config_and_redacts(tmp_path, capsys, fake_rest, monkeypatch):
    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "tok-cli-s3cret")
    config = {
        "identifier": "rides",
        "metadata": {
            "source": {"format": "CSV", "name": "rides", "path": "rides.csv"},
            "target": {"format": "ICEBERG", "name": "rides",
                       "catalog": {"type": "rest", "uri": _uri(fake_rest), "namespace": "demo",
                                   "token_env": "LDP_TEST_REST_TOKEN"}},
        },
    }
    (tmp_path / "rides.json").write_text(json.dumps(config))

    args = _parse(["catalog", "test", str(tmp_path / "rides.json")])

    assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "catalog type: rest" in out
    assert '"token_env": "***"' in out
    assert "namespaces (2): demo, other" in out
    assert "tables in demo (1): demo.rides" in out
    assert "tok-cli-s3cret" not in out


def test_cli_catalog_test_does_not_create_a_missing_local_catalog(tmp_path):
    spec_file = tmp_path / "catalog.json"
    spec_file.write_text(json.dumps({"identifier": "demo", "warehouse_path": "wh"}))

    args = _parse(["catalog", "test", str(spec_file)])

    with pytest.raises(ConfigError, match="no local catalog"):
        args.handler(args)
    assert not (tmp_path / "wh").exists()


def test_cli_catalog_test_rejects_bad_files(tmp_path):
    (tmp_path / "bad.json").write_text("{nope")
    (tmp_path / "config.json").write_text(json.dumps({
        "identifier": "x", "metadata": {"source": {"format": "CSV"}, "target": {"format": "CSV"}}}))

    for name, message in (("missing.json", "not found"), ("bad.json", "not valid JSON"),
                          ("config.json", "no 'catalog' block")):
        args = _parse(["catalog", "test", str(tmp_path / name)])
        with pytest.raises(ConfigError, match=message):
            args.handler(args)


def test_cli_catalog_without_a_subcommand_prints_usage(capsys):
    args = _parse(["catalog"])

    assert args.handler(args) == 1
    assert "test" in capsys.readouterr().err
