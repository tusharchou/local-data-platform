"""Tests for local_data_platform.engine.spark: the Scala Spark job and the PySpark engine.

The unit tests always run and need neither a JDK, scala-cli nor pyspark. The integration tests are
marked ``spark`` and run only when ``LDP_RUN_SPARK=1`` and scala-cli is installed, because the first
run downloads a JDK, Spark and Iceberg (about 500 MB):

    LDP_RUN_SPARK=1 pytest tests/test_spark_interop.py -m spark

The PySpark integration test also needs ``pyspark`` installed (``pip install "local-data-platform[spark]"``).
"""

import math
import os
import random
import subprocess
import sys
import types
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import SupportedEngine
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from local_data_platform.engine import spark as spark_engine
from local_data_platform.engine.spark import (CatalogLocation, ScalaSparkJob, SparkEngine, SparkJobError,
                                              find_java_home, find_scala_cli, parse_result, spark_catalog_conf,
                                              spark_packages)
from local_data_platform.exceptions import ConfigError, EngineNotFound

# Registered in pyproject.toml's [tool.pytest.ini_options] markers.
spark_mark = pytest.mark.spark

REPO_ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as a fake scala-cli")


def _scala_cli_available() -> bool:
    try:
        find_scala_cli()
    except EngineNotFound:
        return False
    return True


requires_spark = pytest.mark.skipif(
    os.environ.get("LDP_RUN_SPARK") != "1" or not _scala_cli_available(),
    reason="Spark integration test: set LDP_RUN_SPARK=1 and install scala-cli",
)


def _fake_scala_cli(folder: Path, body: str) -> Path:
    path = folder / "scala-cli"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _fake_project(folder: Path) -> Path:
    project = folder / "spark"
    project.mkdir()
    (project / "IcebergJob.scala").write_text("// stand-in\n")
    return project


# ---------------------------------------------------------------------------------------------------
# Spark settings
# ---------------------------------------------------------------------------------------------------

def test_spark_catalog_conf_registers_a_jdbc_iceberg_catalog(tmp_path):
    conf = spark_catalog_conf("nyc", tmp_path / "nyc_catalog.db", tmp_path)
    warehouse = tmp_path.resolve()

    assert conf == {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "spark.sql.catalog.nyc": "org.apache.iceberg.spark.SparkCatalog",
        "spark.sql.catalog.nyc.catalog-impl": "org.apache.iceberg.jdbc.JdbcCatalog",
        "spark.sql.catalog.nyc.uri": f"jdbc:sqlite:{warehouse / 'nyc_catalog.db'}",
        "spark.sql.catalog.nyc.warehouse": f"file://{warehouse}",
        "spark.sql.catalog.nyc.jdbc.schema-version": "V1",
        "spark.sql.defaultCatalog": "nyc",
        "spark.sql.session.timeZone": "UTC",
    }
    assert all(isinstance(value, str) for value in conf.values())


def test_spark_catalog_conf_accepts_a_file_uri_and_relative_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conf = spark_catalog_conf("nyc", "wh/nyc_catalog.db", "file:///data/wh/")

    assert conf["spark.sql.catalog.nyc.warehouse"] == "file:///data/wh"
    assert conf["spark.sql.catalog.nyc.uri"] == f"jdbc:sqlite:{tmp_path.resolve() / 'wh' / 'nyc_catalog.db'}"


@pytest.mark.parametrize("name", ["", "a.b", "has space", None])
def test_spark_catalog_conf_rejects_names_spark_cannot_use(tmp_path, name):
    with pytest.raises(ValueError, match="catalog name"):
        spark_catalog_conf(name, tmp_path / "x.db", tmp_path)


def test_catalog_location_follows_the_local_iceberg_catalog_layout(tmp_path):
    location = CatalogLocation.from_config({"identifier": "nyc", "warehouse_path": "warehouse"}, base_dir=tmp_path)
    warehouse = (tmp_path / "warehouse").resolve()

    assert location == CatalogLocation("nyc", warehouse / "nyc_catalog.db", f"file://{warehouse}")
    assert location.conf() == spark_catalog_conf("nyc", warehouse / "nyc_catalog.db", warehouse)


def test_catalog_location_matches_a_real_local_iceberg_catalog(tmp_path):
    catalog = LocalIcebergCatalog("nyc", tmp_path / "warehouse")

    from_catalog = CatalogLocation.from_catalog(catalog)
    from_config = CatalogLocation.from_config({"identifier": "nyc", "warehouse_path": str(tmp_path / "warehouse")})

    assert from_catalog == from_config
    assert catalog.properties["uri"] == f"sqlite:///{from_catalog.catalog_db}"


def test_catalog_location_catalog_name_overrides_the_identifier(tmp_path):
    location = CatalogLocation.from_config({"identifier": "nyc", "warehouse_path": str(tmp_path)}, "lake")
    assert location.name == "lake"
    assert location.catalog_db.name == "lake_catalog.db"


@pytest.mark.parametrize("config", [{}, {"identifier": "nyc"}, {"warehouse_path": "wh"}, "nyc"])
def test_catalog_location_rejects_incomplete_configs(config):
    with pytest.raises(ConfigError):
        CatalogLocation.from_config(config)


def test_catalog_location_rejects_non_sqlite_catalogs():
    class RestCatalog:
        name = "remote"
        properties = {"uri": "http://localhost:8181", "warehouse": "s3://bucket"}

    with pytest.raises(ValueError, match="SQLite"):
        CatalogLocation.from_catalog(RestCatalog())


@pytest.mark.parametrize("version, runtime", [
    ("4.1.3", "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:"),
    ("4.0.1", "org.apache.iceberg:iceberg-spark-runtime-4.0_2.13:"),
    ("3.5.6", "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:"),
])
def test_spark_packages_match_the_spark_minor_and_scala_version(version, runtime):
    iceberg, sqlite = spark_packages(version)
    assert iceberg == runtime + spark_engine.ICEBERG_VERSION
    assert sqlite == f"org.xerial:sqlite-jdbc:{spark_engine.SQLITE_JDBC_VERSION}"


def test_python_versions_match_the_scala_project():
    project = REPO_ROOT / "spark" / "project.scala"
    if not project.is_file():
        pytest.skip("spark/ project not in this checkout")
    text = project.read_text()
    minor = ".".join(spark_engine.SPARK_VERSION.split(".")[:2])
    assert f"org.apache.spark::spark-sql:{spark_engine.SPARK_VERSION}" in text
    assert f"iceberg-spark-runtime-{minor}_2.13:{spark_engine.ICEBERG_VERSION}" in text
    assert f"org.xerial:sqlite-jdbc:{spark_engine.SQLITE_JDBC_VERSION}" in text


# ---------------------------------------------------------------------------------------------------
# Scala job: locating scala-cli and building the command
# ---------------------------------------------------------------------------------------------------

def test_scala_job_command(tmp_path):
    scala_cli = _fake_scala_cli(tmp_path, "exit 0")
    project = _fake_project(tmp_path)
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": "warehouse"}, base_dir=tmp_path,
                        project_dir=project, scala_cli=scala_cli, conf={"spark.driver.memory": "1g"}, show_rows=5)
    warehouse = (tmp_path / "warehouse").resolve()

    assert job.command("rides", output_table="rides_by_city_day") == [
        str(scala_cli), "run", str(project), "-q", "--suppress-outdated-dependency-warning", "--",
        "--catalog-name", "nyc",
        "--catalog-db", str(warehouse / "nyc_catalog.db"),
        "--warehouse", f"file://{warehouse}",
        "--namespace", "nyc",
        "--table", "rides",
        "--output-table", "rides_by_city_day",
        "--show-rows", "5",
        "--conf", "spark.driver.memory=1g",
    ]


def test_scala_job_leaves_the_output_table_to_the_job_by_default(tmp_path):
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)})
    args = job.job_args("rides")
    assert "--output-table" not in args
    assert args[args.index("--table") + 1] == "rides"


def test_scala_job_rejects_bad_arguments(tmp_path):
    with pytest.raises(ValueError, match="show_rows"):
        ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}, show_rows=0)
    with pytest.raises(ValueError, match="table"):
        ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}).job_args("")


def test_default_project_dir_is_the_repo_spark_folder():
    assert spark_engine.SPARK_PROJECT_DIR == REPO_ROOT / "spark"


def test_find_scala_cli_raises_engine_not_found_with_install_hint(monkeypatch):
    monkeypatch.setattr(spark_engine.shutil, "which", lambda name: None)
    monkeypatch.setattr(spark_engine, "SCALA_CLI_LOCATIONS", ())

    with pytest.raises(EngineNotFound, match="brew install Virtuslab/scala-cli/scala-cli"):
        find_scala_cli()


def test_scala_job_run_raises_engine_not_found_without_scala_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(spark_engine.shutil, "which", lambda name: None)
    monkeypatch.setattr(spark_engine, "SCALA_CLI_LOCATIONS", ())
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}, project_dir=_fake_project(tmp_path))

    with pytest.raises(EngineNotFound, match="scala-cli"):
        job.run("rides")


@posix_only
def test_find_scala_cli_searches_known_locations(tmp_path, monkeypatch):
    fake = _fake_scala_cli(tmp_path, "exit 0")
    monkeypatch.setattr(spark_engine.shutil, "which", lambda name: None)
    monkeypatch.setattr(spark_engine, "SCALA_CLI_LOCATIONS", (tmp_path / "missing", tmp_path))

    assert find_scala_cli() == str(fake)


def test_find_scala_cli_rejects_a_missing_explicit_path(tmp_path):
    with pytest.raises(EngineNotFound, match="not found at"):
        find_scala_cli(tmp_path / "nope" / "scala-cli")


@posix_only
def test_scala_job_run_raises_engine_not_found_without_the_project(tmp_path):
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}, project_dir=tmp_path / "gone",
                        scala_cli=_fake_scala_cli(tmp_path, "exit 0"))
    with pytest.raises(EngineNotFound, match="project not found"):
        job.run("rides")


@posix_only
def test_scala_job_run_returns_the_completed_process(tmp_path):
    scala_cli = _fake_scala_cli(tmp_path, 'echo "args: $*"\n'
                                          'echo "LDP_SPARK_RESULT source=a.b.rides source_rows=3 target=a.b.out '
                                          'target_rows=1 mode=row_count"')
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}, project_dir=_fake_project(tmp_path),
                        scala_cli=scala_cli)

    result = job.run("rides", output_table="out")

    assert result.returncode == 0
    assert "--table rides --output-table out" in result.stdout
    assert parse_result(result.stdout) == {"source": "a.b.rides", "source_rows": "3", "target": "a.b.out",
                                           "target_rows": "1", "mode": "row_count"}


@posix_only
def test_scala_job_run_raises_spark_job_error_with_stderr(tmp_path):
    scala_cli = _fake_scala_cli(tmp_path, 'echo "error: Table nyc.nyc.rides not found" >&2\nexit 1')
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}, project_dir=_fake_project(tmp_path),
                        scala_cli=scala_cli)

    with pytest.raises(SparkJobError, match="exit code 1: error: Table nyc.nyc.rides not found") as caught:
        job.run("rides")
    assert caught.value.returncode == 1
    assert job.run("rides", check=False).returncode == 1


@posix_only
def test_scala_job_print_conf_parses_key_values(tmp_path):
    scala_cli = _fake_scala_cli(tmp_path, 'echo "spark.sql.defaultCatalog=nyc"\necho "a.b=c=d"')
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}, project_dir=_fake_project(tmp_path),
                        scala_cli=scala_cli)
    assert job.print_conf("rides") == {"spark.sql.defaultCatalog": "nyc", "a.b": "c=d"}


def test_parse_result_without_a_result_line():
    assert parse_result("nothing to see\n") == {}


# ---------------------------------------------------------------------------------------------------
# PySpark engine: optional dependency handling
# ---------------------------------------------------------------------------------------------------

def test_spark_engine_is_the_pyspark_engine():
    assert SparkEngine.engine is SupportedEngine.PYSPARK


def test_spark_engine_raises_engine_not_found_without_pyspark(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyspark", None)
    monkeypatch.setitem(sys.modules, "pyspark.sql", None)

    with pytest.raises(EngineNotFound, match=r'pip install "local-data-platform\[spark\]"'):
        SparkEngine({"identifier": "nyc", "warehouse_path": str(tmp_path)})


@pytest.mark.parametrize("version", ["4.2.0", "3.4.4"])
def test_spark_engine_rejects_a_pyspark_without_an_iceberg_runtime(tmp_path, monkeypatch, version):
    # PyPI's newest PySpark can be ahead of Iceberg; fail with a hint, not a JVM JAVA_GATEWAY_EXITED error.
    fake_pyspark, fake_sql = types.ModuleType("pyspark"), types.ModuleType("pyspark.sql")
    fake_pyspark.__version__ = version
    fake_sql.SparkSession = object
    monkeypatch.setitem(sys.modules, "pyspark", fake_pyspark)
    monkeypatch.setitem(sys.modules, "pyspark.sql", fake_sql)
    monkeypatch.setattr(spark_engine, "find_java_home", lambda java_home=None: pytest.fail("checked the JDK first"))

    with pytest.raises(EngineNotFound, match=rf"PySpark {version} has no Iceberg .* pyspark>=4.0,<4.2"):
        SparkEngine({"identifier": "nyc", "warehouse_path": str(tmp_path)})


def test_importing_the_module_does_not_import_pyspark():
    code = "import sys, local_data_platform.engine.spark; print('pyspark' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"


def test_find_java_home_prefers_an_explicit_jdk(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "java").write_text("")
    assert find_java_home(tmp_path) == str(tmp_path)
    with pytest.raises(EngineNotFound, match="Java 17"):
        find_java_home(tmp_path / "missing")


def test_find_java_home_uses_java_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JAVA_HOME", str(tmp_path))
    assert find_java_home() == str(tmp_path)


# ---------------------------------------------------------------------------------------------------
# Integration: pyiceberg -> Scala Spark -> pyiceberg, and PySpark on the same catalog
# ---------------------------------------------------------------------------------------------------

RIDES_SCHEMA = pa.schema([("ride_id", pa.int64()), ("pickup_ts", pa.timestamp("us")), ("city", pa.string()),
                          ("distance_km", pa.float64()), ("fare", pa.float64())])


def _rides(n: int = 1000, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    start = datetime(2026, 9, 1)
    rows = []
    for ride_id in range(1, n + 1):
        distance = round(rng.uniform(0.5, 30.0), 2)
        rows.append({"ride_id": ride_id, "pickup_ts": start + timedelta(seconds=rng.randrange(7 * 86400)),
                     "city": rng.choice(["NYC", "BKK", "SFO", "LON"]), "distance_km": distance,
                     "fare": round(2.5 + distance * rng.uniform(1.2, 2.8), 2)})
    return rows


def _revenue_by_city_day(rides: list[dict]) -> dict:
    """The expected aggregate, in plain Python."""
    groups = defaultdict(lambda: [0, 0.0])
    for ride in rides:
        group = groups[(ride["pickup_ts"].date(), ride["city"])]
        group[0] += 1
        group[1] += ride["fare"]
    return {key: tuple(value) for key, value in groups.items()}


@pytest.fixture
def rides_catalog(tmp_path):
    """A LocalIcebergCatalog with nyc.rides written by pyiceberg in two appends."""
    rides = _rides()
    config = {"identifier": "nyc", "warehouse_path": str(tmp_path / "warehouse")}
    catalog = LocalIcebergCatalog(config["identifier"], config["warehouse_path"])
    catalog.create_namespace_if_not_exists("nyc")
    table = catalog.create_table("nyc.rides", schema=RIDES_SCHEMA)
    data = pa.Table.from_pylist(rides, schema=RIDES_SCHEMA)
    table.append(data.slice(0, 600))
    table.append(data.slice(600))
    return config, rides


def _assert_matches(rows: list[dict], expected: dict) -> None:
    got = {(row["day"], row["city"]): (row["row_count"], row["revenue"]) for row in rows}
    assert set(got) == set(expected)
    for key, (count, revenue) in expected.items():
        assert got[key][0] == count, key
        assert math.isclose(got[key][1], revenue, rel_tol=1e-9), key


@spark_mark
@requires_spark
def test_scala_spark_job_reads_and_writes_the_pyiceberg_catalog(rides_catalog):
    config, rides = rides_catalog
    job = ScalaSparkJob(config)

    assert job.print_conf("rides") == job.catalog.conf()

    result = job.run("rides", output_table="rides_by_city_day")
    summary = parse_result(result.stdout)
    assert summary["source_rows"] == "1000"
    assert summary["mode"] == "revenue_by_city_day"
    assert "append" in result.stdout  # the two pyiceberg snapshots, from the snapshots metadata table

    catalog = LocalIcebergCatalog(config["identifier"], config["warehouse_path"])
    assert ("nyc", "rides_by_city_day") in catalog.list_tables("nyc")
    rows = catalog.load_table("nyc.rides_by_city_day").scan().to_arrow().to_pylist()
    expected = _revenue_by_city_day(rides)
    assert summary["target_rows"] == str(len(expected)) == str(len(rows))
    _assert_matches(rows, expected)


@spark_mark
@requires_spark
def test_scala_spark_job_reports_a_missing_table(rides_catalog):
    config, _ = rides_catalog
    with pytest.raises(SparkJobError, match="not found") as caught:
        ScalaSparkJob(config).run("no_such_table")
    assert "rides" in caught.value.stderr  # lists the tables that do exist


@spark_mark
@requires_spark
def test_scala_spark_job_reports_a_missing_namespace(rides_catalog):
    # SHOW TABLES on a missing namespace raises Iceberg's NoSuchNamespaceException, not a Spark
    # AnalysisException; the job must still explain the problem rather than print the bare exception.
    config, _ = rides_catalog
    job = ScalaSparkJob(config)
    job.namespace = "nope"
    with pytest.raises(SparkJobError, match="Namespace nope does not exist in catalog nyc") as caught:
        job.run("rides")
    assert caught.value.returncode == 1
    assert "--catalog-name" in caught.value.stderr


@spark_mark
@requires_spark
def test_pyspark_engine_queries_and_writes_the_pyiceberg_catalog(rides_catalog):
    pytest.importorskip("pyspark")
    config, rides = rides_catalog
    java_home = os.environ.get("JAVA_HOME") or spark_engine.scala_cli_java_home()

    with SparkEngine(config, java_home=java_home) as engine:
        assert engine.query("SELECT count(*) AS n FROM rides").column("n")[0].as_py() == 1000
        rows = engine.query("""
            SELECT CAST(pickup_ts AS DATE) AS day, city, count(*) AS row_count, sum(fare) AS revenue
            FROM nyc.nyc.rides GROUP BY 1, 2""").to_pylist()
        _assert_matches(rows, _revenue_by_city_day(rides))
        snapshots = engine.query(f"SELECT operation FROM {engine.qualified('rides')}.snapshots")
        assert snapshots.column("operation").to_pylist() == ["append", "append"]

        extra = pa.Table.from_pylist([{"ride_id": 5001, "pickup_ts": datetime(2026, 9, 8, 9, 30), "city": "NYC",
                                       "distance_km": 4.2, "fare": 12.75}], schema=RIDES_SCHEMA)
        assert engine.put(extra, "rides") == 1
        engine.query("INSERT INTO rides VALUES (5002, TIMESTAMP_NTZ'2026-09-08 10:00:00', 'BKK', 1.0, 4.0)")

    table = LocalIcebergCatalog(config["identifier"], config["warehouse_path"]).load_table("nyc.rides")
    assert table.scan().to_arrow().num_rows == 1002
    assert len(table.snapshots()) == 4
    added = table.scan(row_filter="ride_id >= 5001").to_arrow().sort_by("ride_id").to_pylist()
    assert [(row["ride_id"], row["pickup_ts"]) for row in added] == [
        (5001, datetime(2026, 9, 8, 9, 30)), (5002, datetime(2026, 9, 8, 10, 0))]


# ---------------------------------------------------------------------------------------------------
# spark_catalog_conf_for, SQLite sql catalogs in the Scala job, and `ldp spark` (platform contract C5)
# ---------------------------------------------------------------------------------------------------

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402

from pyiceberg.catalog.sql import SqlCatalog  # noqa: E402

from local_data_platform.engine.spark import (add_cli, catalog_spec_type, jdbc_url, redact_conf,  # noqa: E402
                                              spark_catalog_conf_for)
from local_data_platform.exceptions import TableNotFound  # noqa: E402


@pytest.mark.parametrize("kind", [None, "local", "LocalIceberg", "local_iceberg"])
def test_spark_catalog_conf_for_a_local_spec_is_spark_catalog_conf(tmp_path, kind):
    spec = {"identifier": "nyc", "warehouse_path": "warehouse", **({"type": kind} if kind else {})}
    warehouse = (tmp_path / "warehouse").resolve()
    assert catalog_spec_type(spec) == "local"
    assert spark_catalog_conf_for(spec, base_dir=tmp_path) == spark_catalog_conf(
        "nyc", warehouse / "nyc_catalog.db", warehouse)


def test_spark_catalog_conf_for_a_sqlite_sql_spec(tmp_path):
    spec = {"type": "sql", "name": "lake", "uri": "sqlite:///meta/catalog.db", "warehouse": "wh",
            "namespace": "sales", "properties": {"s3.endpoint": "http://localhost:9000", "py-io-impl": "x"}}
    conf = spark_catalog_conf_for(spec, base_dir=tmp_path)
    base = tmp_path.resolve()
    assert conf == {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "spark.sql.defaultCatalog": "lake",
        "spark.sql.session.timeZone": "UTC",
        "spark.sql.catalog.lake": "org.apache.iceberg.spark.SparkCatalog",
        "spark.sql.catalog.lake.catalog-impl": "org.apache.iceberg.jdbc.JdbcCatalog",
        "spark.sql.catalog.lake.uri": f"jdbc:sqlite:{base / 'meta' / 'catalog.db'}",
        "spark.sql.catalog.lake.warehouse": f"file://{base / 'wh'}",
        "spark.sql.catalog.lake.jdbc.schema-version": "V1",
        "spark.sql.catalog.lake.s3.endpoint": "http://localhost:9000",
    }


def test_spark_catalog_conf_for_matches_a_real_pyiceberg_sql_catalog(tmp_path):
    uri = f"sqlite:///{tmp_path}/shared.db"
    catalog = SqlCatalog("lake", uri=uri, warehouse=f"file://{tmp_path}/wh")
    catalog.create_namespace("sales")
    catalog.engine.dispose()
    location = CatalogLocation.from_spec({"type": "sql", "name": "lake", "uri": uri,
                                          "warehouse": f"file://{tmp_path}/wh"})
    assert location.catalog_db == (tmp_path / "shared.db").resolve()
    assert location.catalog_db.is_file()
    assert location.warehouse == f"file://{tmp_path}/wh"
    assert location.conf()["spark.sql.catalog.lake.uri"] == f"jdbc:sqlite:{location.catalog_db}"


def test_spark_catalog_conf_for_a_postgres_sql_spec_keeps_the_password_out_of_the_url():
    spec = {"type": "sql", "name": "lake", "warehouse": "s3://bucket/wh",
            "uri": "postgresql+psycopg://ldp:p%40ss@db.internal:5432/iceberg?sslmode=require&sslpassword=q"}
    conf = spark_catalog_conf_for(spec)
    assert conf["spark.sql.catalog.lake.uri"] == "jdbc:postgresql://db.internal:5432/iceberg?sslmode=require"
    assert conf["spark.sql.catalog.lake.jdbc.user"] == "ldp"
    assert conf["spark.sql.catalog.lake.jdbc.password"] == "p@ss"
    assert conf["spark.sql.catalog.lake.jdbc.sslpassword"] == "q"  # secret-looking parameters leave the URL too
    assert conf["spark.sql.catalog.lake.warehouse"] == "s3://bucket/wh"
    redacted = redact_conf(conf)
    assert redacted["spark.sql.catalog.lake.jdbc.password"] == redacted["spark.sql.catalog.lake.jdbc.sslpassword"]
    assert redacted["spark.sql.catalog.lake.jdbc.password"] == "***"
    assert "p@ss" not in json.dumps(redacted) and '"q"' not in json.dumps(redacted)
    assert jdbc_url("mysql+pymysql://u@h/db") == ("jdbc:mysql://h/db", {"user": "u"})


@pytest.mark.parametrize("spec, message", [
    ({"type": "sql", "uri": "sqlite:///x.db", "warehouse": "wh"}, "missing 'name'"),
    ({"type": "sql", "name": "lake", "warehouse": "wh"}, "missing 'uri'"),
    ({"type": "sql", "name": "lake", "uri": "sqlite:///x.db"}, "missing 'warehouse'"),
    ({"type": "sql", "name": "lake", "uri": "sqlite://", "warehouse": "wh"}, "in-memory"),
    ({"type": "sql", "name": "lake", "uri": "oracle://h/db", "warehouse": "wh"}, "no JDBC mapping"),
    ({"type": "sql", "name": "lake", "uri": "not a uri", "warehouse": "wh"}, "not a SQLAlchemy URI"),
    ({"type": "rest", "warehouse": "wh"}, "missing 'uri'"),
    ({"type": "glue", "name": "g", "warehouse": "s3://b"}, "supports catalog types local, sql, rest"),
    ({"type": "sql", "name": "a.b", "uri": "sqlite:///x.db", "warehouse": "wh"}, "catalog name"),
    ({"type": "rest", "uri": "http://x", "properties": ["a"]}, "'properties' must be an object"),
    ({"type": "rest", "uri": "http://x", "properties": {"s3.secret-access-key": "k"}}, "properties_env"),
    ("nyc", "must be an object"),
])
def test_spark_catalog_conf_for_rejects_bad_specs(spec, message):
    with pytest.raises((ConfigError, ValueError), match=message):
        spark_catalog_conf_for(spec)


def test_spark_catalog_conf_for_a_rest_spec_reads_secrets_from_the_environment(monkeypatch):
    monkeypatch.setenv("LDP_TEST_REST_TOKEN", "tok-123")
    monkeypatch.setenv("LDP_TEST_REST_CREDENTIAL", "client:secret")
    spec = {"type": "rest", "name": "polaris", "uri": "http://localhost:8181/api/catalog", "warehouse": "lake",
            "token_env": "LDP_TEST_REST_TOKEN", "credential_env": "LDP_TEST_REST_CREDENTIAL",
            "properties": {"header.X-Iceberg-Access-Delegation": "vended-credentials"}}
    conf = spark_catalog_conf_for(spec)
    assert conf == {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "spark.sql.defaultCatalog": "polaris",
        "spark.sql.session.timeZone": "UTC",
        "spark.sql.catalog.polaris": "org.apache.iceberg.spark.SparkCatalog",
        "spark.sql.catalog.polaris.type": "rest",
        "spark.sql.catalog.polaris.uri": "http://localhost:8181/api/catalog",
        "spark.sql.catalog.polaris.warehouse": "lake",
        "spark.sql.catalog.polaris.header.X-Iceberg-Access-Delegation": "vended-credentials",
        "spark.sql.catalog.polaris.token": "tok-123",
        "spark.sql.catalog.polaris.credential": "client:secret",
    }
    assert set(redact_conf(conf).values()) & {"tok-123", "client:secret"} == set()
    assert spark_catalog_conf_for({"type": "rest", "uri": "http://x"})["spark.sql.defaultCatalog"] == "rest"

    monkeypatch.delenv("LDP_TEST_REST_TOKEN")
    with pytest.raises(ConfigError, match="LDP_TEST_REST_TOKEN, which is not set") as error:
        spark_catalog_conf_for(spec)
    assert "client:secret" not in str(error.value)


def test_spark_catalog_conf_for_reads_password_env_and_properties_env_as_create_catalog_does(monkeypatch):
    monkeypatch.setenv("LDP_TEST_PG_PASSWORD", "pg-s3cr3t")
    monkeypatch.setenv("LDP_TEST_S3_SECRET", "s3-s3cr3t")
    spec = {"type": "sql", "name": "lake", "uri": "postgresql+psycopg://ldp@db.internal:5432/iceberg",
            "password_env": "LDP_TEST_PG_PASSWORD", "warehouse": "s3://bucket/wh",
            "properties": {"s3.region": "eu-west-1"},
            "properties_env": {"s3.secret-access-key": "LDP_TEST_S3_SECRET", "py-io-impl": "LDP_TEST_S3_SECRET"}}
    conf = spark_catalog_conf_for(spec)
    assert conf["spark.sql.catalog.lake.uri"] == "jdbc:postgresql://db.internal:5432/iceberg"
    assert conf["spark.sql.catalog.lake.jdbc.user"] == "ldp"
    assert conf["spark.sql.catalog.lake.jdbc.password"] == "pg-s3cr3t"
    assert conf["spark.sql.catalog.lake.s3.region"] == "eu-west-1"
    assert conf["spark.sql.catalog.lake.s3.secret-access-key"] == "s3-s3cr3t"
    assert "spark.sql.catalog.lake.py-io-impl" not in conf
    redacted = json.dumps(redact_conf(conf))
    assert "pg-s3cr3t" not in redacted and "s3-s3cr3t" not in redacted

    rest = {"type": "rest", "uri": "http://localhost:8181",
            "properties_env": {"s3.secret-access-key": "LDP_TEST_S3_SECRET"}}
    assert spark_catalog_conf_for(rest)["spark.sql.catalog.rest.s3.secret-access-key"] == "s3-s3cr3t"

    monkeypatch.delenv("LDP_TEST_S3_SECRET")
    with pytest.raises(ConfigError, match="'LDP_TEST_S3_SECRET', which is not set") as error:
        spark_catalog_conf_for(spec)
    assert "pg-s3cr3t" not in str(error.value)
    monkeypatch.delenv("LDP_TEST_PG_PASSWORD")
    with pytest.raises(ConfigError, match="'LDP_TEST_PG_PASSWORD', which is not set"):
        spark_catalog_conf_for({**spec, "properties_env": {}})


def test_scala_job_runs_on_a_sqlite_sql_spec(tmp_path):
    spec = {"type": "sql", "name": "lake", "uri": "sqlite:///cat.db", "warehouse": "wh", "namespace": "sales",
            "properties": {"s3.endpoint": "http://minio:9000"}}
    job = ScalaSparkJob(spec, base_dir=tmp_path, conf={"spark.driver.memory": "1g"})
    base = tmp_path.resolve()
    args = job.job_args("orders")
    assert args[:10] == ["--catalog-name", "lake", "--catalog-db", str(base / "cat.db"),
                         "--warehouse", f"file://{base / 'wh'}", "--namespace", "sales", "--table", "orders"]
    assert args[-4:] == ["--conf", "spark.sql.catalog.lake.s3.endpoint=http://minio:9000",
                         "--conf", "spark.driver.memory=1g"]
    assert job.namespace == "sales"


@pytest.mark.parametrize("spec, message", [
    ({"type": "rest", "uri": "http://localhost:8181"}, "not 'rest' catalogs"),
    ({"type": "sql", "name": "lake", "uri": "postgresql://h/db", "warehouse": "wh"}, "only the SQLite JDBC driver"),
    ({"type": "sql", "name": "lake", "uri": "sqlite:///x.db", "warehouse": "s3://bucket/wh"}, "local warehouse"),
])
def test_scala_job_rejects_catalogs_it_cannot_open(spec, message):
    with pytest.raises(ConfigError, match=message):
        ScalaSparkJob(spec)


@posix_only
def test_scala_job_never_logs_secret_settings(tmp_path, caplog):
    job = ScalaSparkJob({"identifier": "nyc", "warehouse_path": str(tmp_path)}, project_dir=_fake_project(tmp_path),
                        scala_cli=_fake_scala_cli(tmp_path, "exit 0"),
                        conf={"spark.sql.catalog.nyc.token": "s3cr3t", "spark.hadoop.fs.s3a.secret.key": "k3y",
                              "spark.driver.memory": "1g"})
    with caplog.at_level(logging.INFO, logger="local_data_platform.engine.spark"):
        job.run("rides")
    assert "spark.driver.memory=1g" in caplog.text
    assert "spark.sql.catalog.nyc.token=***" in caplog.text
    assert "s3cr3t" not in caplog.text and "k3y" not in caplog.text


@posix_only
def test_scala_job_passes_properties_env_and_never_logs_it(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("LDP_TEST_S3_SECRET", "s3-s3cr3t")
    monkeypatch.setenv("LDP_TEST_ADLS_KEY", "adls-k3y")
    # adls.account-key isn't named like a secret; it is masked because it came from properties_env.
    spec = {"type": "sql", "name": "lake", "uri": "sqlite:///cat.db", "warehouse": "wh", "namespace": "sales",
            "properties_env": {"s3.secret-access-key": "LDP_TEST_S3_SECRET", "adls.account-key": "LDP_TEST_ADLS_KEY"}}
    job = ScalaSparkJob(spec, base_dir=tmp_path, project_dir=_fake_project(tmp_path),
                        scala_cli=_fake_scala_cli(tmp_path, "exit 0"))
    assert job.job_args("orders")[-4:] == ["--conf", "spark.sql.catalog.lake.s3.secret-access-key=s3-s3cr3t",
                                           "--conf", "spark.sql.catalog.lake.adls.account-key=adls-k3y"]
    assert job.secret_keys == {"spark.sql.catalog.lake.s3.secret-access-key", "spark.sql.catalog.lake.adls.account-key"}
    with caplog.at_level(logging.INFO, logger="local_data_platform.engine.spark"):
        job.run("orders")
    assert "spark.sql.catalog.lake.s3.secret-access-key=***" in caplog.text
    assert "spark.sql.catalog.lake.adls.account-key=***" in caplog.text
    assert "s3cr3t" not in caplog.text + repr(job) and "adls-k3y" not in caplog.text + repr(job)
    assert redact_conf(dict(job.conf), job.secret_keys)["spark.sql.catalog.lake.adls.account-key"] == "***"
    assert redact_conf({"a.b": "visible"}, job.secret_keys) == {"a.b": "visible"}


# ---------------------------------------------------------------------------------------------------
# ldp spark CONFIG
# ---------------------------------------------------------------------------------------------------

def _spark_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ldp")
    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("-v", "--verbose", action="store_true")
    add_cli(parser.add_subparsers(dest="command"), parents=[verbose])
    return parser


def _write_config(tmp_path: Path, target: dict | None = None) -> Path:
    config = {
        "identifier": "rides_to_iceberg", "who": "tests", "what": "rides", "where": "local", "when": "now",
        "how": "cli", "metadata": {
            "source": {"name": "rides_csv", "format": "CSV", "path": "rides.csv"},
            "target": target or {"name": "rides", "format": "ICEBERG",
                                 "catalog": {"identifier": "nyc", "warehouse_path": "warehouse"}},
        }}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def test_ldp_spark_parses_its_arguments(tmp_path):
    args = _spark_parser().parse_args(["spark", "cfg.json", "--output-table", "summary", "--show-rows", "3",
                                       "--conf", "a=b", "--conf", "c=d=e", "-v"])
    assert (args.config, args.output_table, args.show_rows, args.conf) == ("cfg.json", "summary", 3,
                                                                           ["a=b", "c=d=e"])
    assert args.verbose and callable(args.handler)
    with pytest.raises(SystemExit):
        _spark_parser().parse_args(["spark", "cfg.json", "--show-rows", "0"])


@posix_only
def test_ldp_spark_dry_run_prints_the_command(tmp_path, capsys):
    config = _write_config(tmp_path)
    scala_cli = _fake_scala_cli(tmp_path, "exit 3")
    args = _spark_parser().parse_args(["spark", str(config), "--dry-run", "--scala-cli", str(scala_cli),
                                       "--conf", "spark.sql.catalog.nyc.token=s3cr3t", "--output-table", "out"])
    assert args.handler(args) == 0
    printed = capsys.readouterr().out
    warehouse = (tmp_path / "warehouse").resolve()
    assert printed.startswith(f"{scala_cli} run ")
    assert f"--catalog-db {warehouse / 'nyc_catalog.db'}" in printed
    assert "--namespace nyc --table rides --output-table out" in printed
    assert "spark.sql.catalog.nyc.token=***" in printed and "s3cr3t" not in printed


@posix_only
def test_ldp_spark_runs_the_job_on_the_config_target(tmp_path, capsys):
    config = _write_config(tmp_path)
    LocalIcebergCatalog("nyc", tmp_path / "warehouse").close()  # the catalog file exists
    scala_cli = _fake_scala_cli(tmp_path, 'echo "== args $*"\n'
                                          'echo "LDP_SPARK_RESULT source=nyc.nyc.rides source_rows=6 '
                                          'target=nyc.nyc.out target_rows=2 mode=revenue_by_city_day"')
    args = _spark_parser().parse_args(["spark", str(config), "--scala-cli", str(scala_cli), "--output-table", "out"])
    assert args.handler(args) == 0
    printed = capsys.readouterr().out
    assert "--table rides --output-table out" in printed
    assert parse_result(printed)["target_rows"] == "2"


@posix_only
def test_ldp_spark_print_conf_redacts_secrets(tmp_path, capsys):
    config = _write_config(tmp_path)
    LocalIcebergCatalog("nyc", tmp_path / "warehouse").close()
    scala_cli = _fake_scala_cli(tmp_path, 'echo "spark.sql.defaultCatalog=nyc"\necho "spark.sql.catalog.nyc.token=t0k"')
    args = _spark_parser().parse_args(["spark", str(config), "--scala-cli", str(scala_cli), "--print-conf"])
    assert args.handler(args) == 0
    assert capsys.readouterr().out.splitlines() == ["spark.sql.catalog.nyc.token=***", "spark.sql.defaultCatalog=nyc"]


@posix_only
def test_ldp_spark_redacts_secrets_read_from_properties_env(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LDP_TEST_S3_SECRET", "s3-s3cr3t")
    monkeypatch.setenv("LDP_TEST_AUTH", "Bearer h3ader")
    config = _write_config(tmp_path, target={
        "name": "orders", "format": "ICEBERG",
        "catalog": {"type": "sql", "name": "lake", "uri": "sqlite:///cat.db", "warehouse": "wh", "namespace": "sales",
                    "properties_env": {"s3.secret-access-key": "LDP_TEST_S3_SECRET",
                                       "header.Authorization": "LDP_TEST_AUTH"}}})
    (tmp_path / "cat.db").touch()
    # Like the Scala job's --print-conf: print each --conf setting it was given.
    scala_cli = _fake_scala_cli(tmp_path, 'while [ $# -gt 0 ]; do [ "$1" = --conf ] && echo "$2"; shift; done')
    for flag in ("--dry-run", "--print-conf"):
        args = _spark_parser().parse_args(["spark", str(config), "--scala-cli", str(scala_cli), flag])
        assert args.handler(args) == 0
        printed = capsys.readouterr().out
        assert "spark.sql.catalog.lake.s3.secret-access-key=***" in printed and "s3-s3cr3t" not in printed
        assert "spark.sql.catalog.lake.header.Authorization=***" in printed and "h3ader" not in printed


def test_ldp_spark_reports_a_missing_catalog_and_bad_configs(tmp_path):
    config = _write_config(tmp_path)
    args = _spark_parser().parse_args(["spark", str(config)])
    with pytest.raises(TableNotFound, match="no catalog at"):
        args.handler(args)
    assert not (tmp_path / "warehouse").exists()  # nothing was created

    args = _spark_parser().parse_args(["spark", str(config), "--conf", "novalue"])
    with pytest.raises(ConfigError, match="key=value"):
        args.handler(args)

    no_iceberg = _write_config(tmp_path, target={"name": "out", "format": "PARQUET", "path": "out.parquet"})
    args = _spark_parser().parse_args(["spark", str(no_iceberg)])
    with pytest.raises(ConfigError, match="no Iceberg table"):
        args.handler(args)

    rest = _write_config(tmp_path, target={"name": "rides", "format": "ICEBERG",
                                           "catalog": {"type": "rest", "uri": "http://localhost:8181"}})
    args = _spark_parser().parse_args(["spark", str(rest)])
    with pytest.raises(ConfigError, match="rest"):
        args.handler(args)


@spark_mark
@requires_spark
def test_ldp_spark_end_to_end(rides_catalog, tmp_path, capsys):
    config, rides = rides_catalog
    path = _write_config(tmp_path, target={"name": "rides", "format": "ICEBERG", "catalog": config})
    args = _spark_parser().parse_args(["spark", str(path), "--output-table", "rides_by_city_day"])
    assert args.handler(args) == 0
    summary = parse_result(capsys.readouterr().out)
    assert summary["source_rows"] == "1000" and summary["mode"] == "revenue_by_city_day"
    rows = LocalIcebergCatalog("nyc", config["warehouse_path"]).load_table("nyc.rides_by_city_day").scan().to_arrow()
    _assert_matches(rows.to_pylist(), _revenue_by_city_day(rides))


@spark_mark
@requires_spark
def test_scala_spark_job_reads_a_pyiceberg_sql_catalog(tmp_path):
    rides = _rides(200)
    spec = {"type": "sql", "name": "lake", "uri": f"sqlite:///{tmp_path}/lake.db",
            "warehouse": f"file://{tmp_path}/wh", "namespace": "sales"}
    catalog = SqlCatalog("lake", uri=spec["uri"], warehouse=spec["warehouse"])
    catalog.create_namespace("sales")
    catalog.create_table("sales.rides", schema=RIDES_SCHEMA).append(pa.Table.from_pylist(rides, schema=RIDES_SCHEMA))
    catalog.engine.dispose()

    job = ScalaSparkJob(spec)
    assert job.print_conf("rides") == {key: value for key, value in spark_catalog_conf_for(spec).items()}
    summary = parse_result(job.run("rides", output_table="by_day").stdout)
    assert summary["source_rows"] == "200"

    catalog = SqlCatalog("lake", uri=spec["uri"], warehouse=spec["warehouse"])
    rows = catalog.load_table("sales.by_day").scan().to_arrow().to_pylist()
    catalog.engine.dispose()
    _assert_matches(rows, _revenue_by_city_day(rides))
