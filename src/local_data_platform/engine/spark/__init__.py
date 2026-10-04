"""Spark engines over the local Iceberg catalog: a Scala Spark job and PySpark.

Both open the SQLite file that :class:`~local_data_platform.catalog.local.iceberg.LocalIcebergCatalog`
(pyiceberg's ``SqlCatalog``) writes, through Iceberg's ``JdbcCatalog``, so Python, Scala Spark and
PySpark read and write one set of tables. :func:`spark_catalog_conf` returns the Spark settings that
do this; the Scala job in ``spark/`` builds the same map.

:func:`spark_catalog_conf_for` generalises it to a catalog spec (a config's ``target.catalog``
block) of type ``local``, ``sql`` (SQLite, PostgreSQL or MySQL through ``JdbcCatalog``) or ``rest``,
for any Spark: this package's engines, ``spark-submit``, or a managed service.

* :class:`ScalaSparkJob` runs ``spark/IcebergJob.scala`` with `scala-cli <https://scala-cli.virtuslab.org>`_.
  It needs only ``scala-cli``, which fetches a JDK, Scala, Spark and Iceberg on first use. It works
  from a source checkout, where the ``spark/`` folder exists. ``ldp spark CONFIG`` (see
  :func:`add_cli`) runs it on a config's Iceberg table.
* :class:`SparkEngine` runs Spark SQL from Python with PySpark and returns ``pyarrow`` tables.
  ``pyspark`` is an optional dependency, imported when an engine is created, never when this module
  is imported.

The catalog is one SQLite file, so it has one writer at a time: run Python writes and Spark jobs one
after the other, not at the same time.

Example:
    >>> catalog = {"identifier": "nyc", "warehouse_path": "warehouse"}   # doctest: +SKIP
    >>> ScalaSparkJob(catalog).run("rides", output_table="rides_by_city_day")
    >>> with SparkEngine(catalog) as spark:
    ...     spark.query("SELECT city, count(*) AS n FROM rides GROUP BY city")      # or nyc.nyc.rides
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pyarrow as pa

from local_data_platform import SupportedEngine
from local_data_platform.engine import Engine
from local_data_platform.exceptions import ConfigError, EngineNotFound, LDPError, TableNotFound
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

logger = get_logger(__name__)

# Keep in step with spark/project.scala. Spark 4.1 is the newest Spark line with an Iceberg runtime.
SPARK_VERSION = "4.1.3"
ICEBERG_VERSION = "1.12.0"
SQLITE_JDBC_VERSION = "3.53.4.0"
#: Spark minor versions that ICEBERG_VERSION publishes an ``iceberg-spark-runtime`` for.
ICEBERG_SPARK_MINORS: tuple[str, ...] = ("3.5", "4.0", "4.1")

PYSPARK_INSTALL_HINT = 'pip install "local-data-platform[spark]"'
SCALA_CLI_INSTALL_HINT = ("brew install Virtuslab/scala-cli/scala-cli, or see "
                          "https://scala-cli.virtuslab.org/install")
JAVA_HINT = ("PySpark needs Java 17 or later. Set JAVA_HOME or pass java_home=. With scala-cli installed, "
             "java_home=scala_cli_java_home() uses the JDK scala-cli manages.")

#: The ``spark/`` scala-cli project, present in a source checkout.
SPARK_PROJECT_DIR = Path(__file__).resolve().parents[4] / "spark"

#: Folders searched for ``scala-cli`` when it is not on ``PATH``.
SCALA_CLI_LOCATIONS: tuple[Path, ...] = (
    Path.home() / "homebrew" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path.home() / ".local" / "bin",
    Path.home() / "Library" / "Application Support" / "Coursier" / "bin",
    Path.home() / ".local" / "share" / "coursier" / "bin",
)

SQL_EXTENSIONS = "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"

#: Settings for a small single-machine session. Not part of the catalog contract.
LOCAL_SESSION_CONF: dict[str, str] = {
    "spark.ui.enabled": "false",
    "spark.sql.shuffle.partitions": "4",
}

RESULT_PREFIX = "LDP_SPARK_RESULT "

#: Catalog spec types :func:`spark_catalog_conf_for` supports.
SPARK_CATALOG_TYPES: tuple[str, ...] = ("local", "sql", "rest")

#: Setting names containing one of these words hold secrets; :func:`redact_conf` hides their values.
SECRET_WORDS: tuple[str, ...] = ("token", "secret", "password", "credential")
REDACTED = "***"


class SparkJobError(LDPError):
    """Raised when the Scala Spark job exits with an error.

    ``returncode``, ``stdout`` and ``stderr`` hold what the job printed.
    """

    def __init__(self, message: str, returncode: int, stdout: str, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def spark_packages(spark_version: str = SPARK_VERSION) -> tuple[str, ...]:
    """Maven coordinates Spark needs for the local catalog: the Iceberg runtime and the SQLite driver.

    The Iceberg runtime is built per Spark minor version and Scala version. Spark 4 ships Scala 2.13;
    the PyPI builds of Spark 3.x ship Scala 2.12.
    """
    major, minor = spark_version.split(".")[:2]
    scala = "2.13" if int(major) >= 4 else "2.12"
    return (f"org.apache.iceberg:iceberg-spark-runtime-{major}.{minor}_{scala}:{ICEBERG_VERSION}",
            f"org.xerial:sqlite-jdbc:{SQLITE_JDBC_VERSION}")


def _check_catalog_name(name: Any) -> str:
    if not isinstance(name, str) or not name or "." in name or any(c.isspace() for c in name):
        raise ValueError(f"Spark catalog name must be a single name without dots or spaces, got {name!r}")
    return name


def _warehouse_uri(warehouse: str | os.PathLike) -> str:
    value = str(warehouse)
    if value.startswith("file:"):
        return value.removesuffix("/")
    return f"file://{Path(os.path.expanduser(value)).resolve()}"


def spark_catalog_conf(catalog_name: str, catalog_db: str | os.PathLike,
                       warehouse: str | os.PathLike) -> dict[str, str]:
    """Spark settings that register a pyiceberg SQLite catalog as an Iceberg ``SparkCatalog``.

    Args:
        catalog_name: The pyiceberg catalog name. Spark must use the same name, because Iceberg's
            ``JdbcCatalog`` only sees rows whose ``catalog_name`` column equals it.
        catalog_db: The SQLite file, ``<warehouse>/<name>_catalog.db`` for ``LocalIcebergCatalog``.
        warehouse: Warehouse folder, or a ``file://`` URI. Spark creates new tables under it.

    Returns:
        The settings, as strings. ``jdbc.schema-version=V1`` matches the ``iceberg_type`` column
        pyiceberg's ``SqlCatalog`` creates. Paths are made absolute with symlinks resolved, as
        ``LocalIcebergCatalog`` does.

    Raises:
        ValueError: If ``catalog_name`` is empty or contains a dot or whitespace.
    """
    name = _check_catalog_name(catalog_name)
    db = Path(os.path.expanduser(str(catalog_db))).resolve()
    prefix = f"spark.sql.catalog.{name}"
    return {
        "spark.sql.extensions": SQL_EXTENSIONS,
        prefix: "org.apache.iceberg.spark.SparkCatalog",
        f"{prefix}.catalog-impl": "org.apache.iceberg.jdbc.JdbcCatalog",
        f"{prefix}.uri": f"jdbc:sqlite:{db}",
        f"{prefix}.warehouse": _warehouse_uri(warehouse),
        f"{prefix}.jdbc.schema-version": "V1",
        "spark.sql.defaultCatalog": name,
        "spark.sql.session.timeZone": "UTC",
    }


def redact_conf(conf: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``conf`` with the values of secret settings (tokens, passwords, credentials) replaced by ``***``."""
    return {key: REDACTED if _is_secret(key) else value for key, value in conf.items()}


def _is_secret(key: str) -> bool:
    lowered = str(key).lower()
    return any(word in lowered for word in SECRET_WORDS)


def _redact_command(command: Sequence[str]) -> list[str]:
    """``command`` with the values of secret ``--conf key=value`` arguments replaced by ``***``."""
    redacted = []
    for part in command:
        key, sep, _ = part.partition("=")
        redacted.append(f"{key}={REDACTED}" if sep and _is_secret(key) else part)
    return redacted


def catalog_spec_type(spec: Mapping[str, Any]) -> str:
    """The normalised ``type`` of a catalog spec: ``local`` (the default, alias ``LocalIceberg``), ``sql``, ...."""
    raw = str(spec.get("type") or "local").strip().lower().replace("_", "").replace("-", "")
    return "local" if raw in ("local", "localiceberg") else raw


def _spec_value(spec: Mapping[str, Any], key: str, kind: str) -> str:
    value = spec.get(key)
    if value is None or not str(value).strip():
        raise ConfigError(f"{kind} catalog spec is missing '{key}'")
    return str(value)


def _warehouse_location(value: str | os.PathLike, base_dir: str | os.PathLike | None) -> str:
    """A warehouse as Spark needs it: object-store URIs unchanged, local folders as absolute ``file://`` URIs."""
    text = str(value)
    if text.startswith("file:"):
        return text.removesuffix("/")
    if "://" in text:
        return text
    return f"file://{resolve_path(text, base_dir).resolve()}"


def _catalog_properties(spec: Mapping[str, Any], prefix: str) -> dict[str, str]:
    """The spec's ``properties``, as catalog settings. pyiceberg-only ``py-*`` keys are left out."""
    properties = spec.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise ConfigError(f"catalog spec 'properties' must be an object, got {type(properties).__name__}")
    return {f"{prefix}.{key}": str(value) for key, value in properties.items() if not str(key).startswith("py-")}


def _session_conf(name: str) -> dict[str, str]:
    return {"spark.sql.extensions": SQL_EXTENSIONS, "spark.sql.defaultCatalog": name,
            "spark.sql.session.timeZone": "UTC"}


def jdbc_url(uri: str, base_dir: str | os.PathLike | None = None) -> tuple[str, dict[str, str]]:
    """Turn a pyiceberg ``SqlCatalog`` SQLAlchemy URI into a JDBC URL and driver properties.

    ``sqlite:///relative.db`` resolves against ``base_dir`` (or the cwd), as other config paths
    do. A user name and password move out of the URL into the ``user`` and ``password`` driver
    properties, which Iceberg's ``JdbcCatalog`` reads as ``jdbc.user`` and ``jdbc.password``.

    Args:
        uri: The SQLAlchemy URI, for example ``postgresql+psycopg://user:pw@host:5432/db``.
        base_dir: Folder a relative SQLite path resolves against.

    Returns:
        ``(jdbc_url, properties)``, for example ``("jdbc:postgresql://host:5432/db", {"user": ..., "password": ...})``.

    Raises:
        ConfigError: For an in-memory SQLite database or a database Spark has no JDBC mapping for.
    """
    from sqlalchemy.engine import make_url

    try:
        url = make_url(uri)
    except Exception as error:  # noqa: BLE001 - SQLAlchemy raises ArgumentError and ValueError
        raise ConfigError(f"sql catalog 'uri' is not a SQLAlchemy URI: {error}") from None
    backend = url.get_backend_name()
    if backend == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ConfigError("an in-memory SQLite catalog can't be shared with Spark; use sqlite:///<file>")
        return f"jdbc:sqlite:{resolve_path(url.database, base_dir).resolve()}", {}
    if backend not in ("postgresql", "mysql", "mariadb"):
        raise ConfigError(f"Spark has no JDBC mapping for {backend!r} catalogs; use sqlite, postgresql or mysql")
    properties: dict[str, str] = {}
    if url.username:
        properties["user"] = url.username
    if url.password:
        properties["password"] = str(url.password)
    query = {}
    for key, value in url.query.items():
        value = value[-1] if isinstance(value, tuple) else value
        if _is_secret(key):
            properties[key] = str(value)
        else:
            query[key] = str(value)
    host = url.host or "localhost"
    netloc = f"{host}:{url.port}" if url.port else host
    jdbc = f"jdbc:{backend}://{netloc}/{url.database or ''}"
    return (f"{jdbc}?{urlencode(query)}" if query else jdbc), properties


def spark_catalog_conf_for(spec: Mapping[str, Any], base_dir: str | os.PathLike | None = None) -> dict[str, str]:
    """Spark settings that register the catalog a spec describes as an Iceberg ``SparkCatalog``.

    ``spec`` is a config's ``target.catalog`` block (see :mod:`local_data_platform.catalog.provider`):

    * ``local`` (the default): ``{"identifier", "warehouse_path"}``, exactly :func:`spark_catalog_conf`
      for ``<warehouse>/<identifier>_catalog.db``.
    * ``sql``: ``{"name", "uri", "warehouse"}``. ``uri`` is the SQLAlchemy URI pyiceberg's
      ``SqlCatalog`` uses; Spark gets the matching JDBC URL (:func:`jdbc_url`) and
      ``jdbc.schema-version=V1``. ``name`` is required, because ``JdbcCatalog`` only sees the rows
      whose ``catalog_name`` equals it. PostgreSQL and MySQL need their JDBC driver on Spark's
      classpath (``org.postgresql:postgresql``, ``com.mysql:mysql-connector-j``).
    * ``rest``: ``{"uri", "warehouse", "name", "token_env", "credential_env"}``. The token and the
      OAuth2 credential are read from the named environment variables. ``name`` defaults to ``rest``.

    For ``sql`` and ``rest``, ``properties`` are passed through as catalog settings (``s3.endpoint``,
    ``io-impl``, ``header.*``, ...), except pyiceberg-only ``py-*`` keys. An ``s3://`` warehouse also
    needs ``org.apache.iceberg:iceberg-aws-bundle`` on Spark's classpath.

    The result can hold secrets (a database password, a token): pass it to Spark, and log it only
    through :func:`redact_conf`.

    Args:
        spec: The catalog spec.
        base_dir: Folder relative paths in ``spec`` resolve against (the config file's folder).

    Returns:
        The settings, as strings.

    Raises:
        ConfigError: If the spec is incomplete, names an unset environment variable, or has an
            unsupported type.
    """
    if not isinstance(spec, Mapping):
        raise ConfigError(f"catalog spec must be an object, got {type(spec).__name__}")
    kind = catalog_spec_type(spec)
    if kind == "local":
        return CatalogLocation.from_config(spec, base_dir=base_dir).conf()
    if kind == "sql":
        name = _check_catalog_name(_spec_value(spec, "name", "sql"))
        prefix = f"spark.sql.catalog.{name}"
        url, driver_properties = jdbc_url(_spec_value(spec, "uri", "sql"), base_dir)
        conf = {
            **_session_conf(name),
            prefix: "org.apache.iceberg.spark.SparkCatalog",
            f"{prefix}.catalog-impl": "org.apache.iceberg.jdbc.JdbcCatalog",
            f"{prefix}.uri": url,
            f"{prefix}.warehouse": _warehouse_location(_spec_value(spec, "warehouse", "sql"), base_dir),
            f"{prefix}.jdbc.schema-version": "V1",
        }
        conf.update({f"{prefix}.jdbc.{key}": value for key, value in driver_properties.items()})
        conf.update(_catalog_properties(spec, prefix))
        return conf
    if kind == "rest":
        name = _check_catalog_name(spec.get("name") or "rest")
        prefix = f"spark.sql.catalog.{name}"
        conf = {
            **_session_conf(name),
            prefix: "org.apache.iceberg.spark.SparkCatalog",
            f"{prefix}.type": "rest",
            f"{prefix}.uri": _spec_value(spec, "uri", "rest"),
        }
        if spec.get("warehouse"):
            conf[f"{prefix}.warehouse"] = str(spec["warehouse"])
        conf.update(_catalog_properties(spec, prefix))
        for key, setting in (("token_env", "token"), ("credential_env", "credential")):
            variable = spec.get(key)
            if variable:
                value = os.environ.get(str(variable))
                if not value:
                    raise ConfigError(f"rest catalog spec '{key}' names environment variable {variable}, "
                                      "which is not set")
                conf[f"{prefix}.{setting}"] = value
        return conf
    raise ConfigError(f"spark_catalog_conf_for() supports catalog types {', '.join(SPARK_CATALOG_TYPES)}; "
                      f"got {kind!r}")


def _spec_namespace(spec: Mapping[str, Any]) -> str:
    """The namespace a spec's tables live in: ``identifier`` for local catalogs, else ``namespace``/``identifier``."""
    if catalog_spec_type(spec) == "local":
        return str(spec["identifier"])
    from local_data_platform.catalog.provider import catalog_namespace

    return catalog_namespace(spec)


@dataclass(frozen=True)
class CatalogLocation:
    """Where a local Iceberg catalog lives, in the form Spark needs.

    Attributes:
        name: Catalog name, shared by pyiceberg and Spark.
        catalog_db: Absolute path of the SQLite catalog file.
        warehouse: Warehouse location as a ``file://`` URI.
    """

    name: str
    catalog_db: Path
    warehouse: str

    @classmethod
    def from_config(cls, config: Mapping[str, Any], catalog_name: str | None = None,
                    base_dir: str | os.PathLike | None = None) -> CatalogLocation:
        """Build it from a dataset config's catalog dict, ``{"identifier", "warehouse_path"}``.

        This follows ``Iceberg`` and ``LocalIcebergCatalog``: the catalog is named after
        ``identifier`` unless ``catalog_name`` is given, ``warehouse_path`` resolves against
        ``base_dir`` (the config file's folder), and the database is ``<name>_catalog.db`` in it.

        Raises:
            ConfigError: If a key is missing or empty.
        """
        if not isinstance(config, Mapping):
            raise ConfigError(f"Catalog config must be a dict with 'identifier' and 'warehouse_path', "
                              f"got {type(config).__name__}")
        missing = [key for key in ("identifier", "warehouse_path") if not config.get(key)]
        if missing:
            raise ConfigError(f"Catalog config is missing {', '.join(missing)}: {dict(config)!r}")
        name = _check_catalog_name(catalog_name or config["identifier"])
        warehouse = resolve_path(config["warehouse_path"], base_dir).resolve()
        return cls(name, warehouse / f"{name}_catalog.db", f"file://{warehouse}")

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any], catalog_name: str | None = None,
                  base_dir: str | os.PathLike | None = None) -> CatalogLocation:
        """Build it from a catalog spec whose catalog is a SQLite file: ``local``, or ``sql`` on SQLite.

        A ``local`` spec goes through :meth:`from_config`. A ``sql`` spec uses its ``name`` (or
        ``catalog_name``), the file its ``sqlite:///`` URI names and its ``warehouse``.

        Raises:
            ConfigError: If the spec is incomplete, or its catalog is not a SQLite file (a REST
                catalog, or a sql catalog on PostgreSQL): the Scala job and :class:`SparkEngine`
                share only SQLite catalogs. Use :func:`spark_catalog_conf_for` with your own Spark
                for those.
        """
        if not isinstance(spec, Mapping):
            raise ConfigError(f"catalog spec must be an object, got {type(spec).__name__}")
        kind = catalog_spec_type(spec)
        if kind == "local":
            return cls.from_config(spec, catalog_name, base_dir)
        if kind == "sql":
            name = _check_catalog_name(catalog_name or _spec_value(spec, "name", "sql"))
            url, _ = jdbc_url(_spec_value(spec, "uri", "sql"), base_dir)
            if not url.startswith("jdbc:sqlite:"):
                raise ConfigError("the Scala Spark job ships only the SQLite JDBC driver, so it can't open a "
                                  f"{url.split(':')[1]} catalog; use spark_catalog_conf_for() with your own Spark")
            warehouse = _warehouse_location(_spec_value(spec, "warehouse", "sql"), base_dir)
            if not warehouse.startswith("file:"):
                raise ConfigError(f"the Scala Spark job needs a local warehouse, got {warehouse!r}")
            return cls(name, Path(url[len("jdbc:sqlite:"):]), warehouse)
        raise ConfigError(f"the Scala Spark job opens local and SQLite sql catalogs, not {kind!r} catalogs; "
                          "use spark_catalog_conf_for() with your own Spark")

    @classmethod
    def from_catalog(cls, catalog: Any) -> CatalogLocation:
        """Build it from a pyiceberg ``SqlCatalog`` on SQLite, such as ``LocalIcebergCatalog``.

        Raises:
            ValueError: If the catalog is not stored in a SQLite file.
        """
        properties = getattr(catalog, "properties", {})
        uri = str(properties.get("uri", ""))
        if not uri.startswith("sqlite:///"):
            raise ValueError(f"Spark can only share a SQLite pyiceberg catalog, got uri {uri!r}")
        warehouse = str(properties.get("warehouse", ""))
        if not warehouse:
            raise ValueError(f"Catalog {catalog.name!r} has no warehouse property")
        if "://" in warehouse and not warehouse.startswith("file:"):
            raise ValueError(f"Spark can only share a local warehouse, got {warehouse!r}")
        # sqlite:///relative.db and sqlite:////absolute.db, as SQLAlchemy reads them.
        db = Path(uri[len("sqlite:///"):]).resolve()
        return cls(_check_catalog_name(catalog.name), db, _warehouse_uri(warehouse))

    def conf(self) -> dict[str, str]:
        """The :func:`spark_catalog_conf` settings for this catalog."""
        return spark_catalog_conf(self.name, self.catalog_db, self.warehouse)


def find_scala_cli(scala_cli: str | os.PathLike | None = None) -> str:
    """Return the path of the ``scala-cli`` executable.

    Args:
        scala_cli: An explicit path. When omitted, ``PATH`` is searched, then
            :data:`SCALA_CLI_LOCATIONS`.

    Raises:
        EngineNotFound: If ``scala-cli`` is not found, with an install hint.
    """
    if scala_cli is not None:
        if os.path.isfile(scala_cli) and os.access(scala_cli, os.X_OK):
            return str(scala_cli)
        raise EngineNotFound(f"scala-cli not found at {scala_cli}. Install it with: {SCALA_CLI_INSTALL_HINT}")
    found = shutil.which("scala-cli")
    if found:
        return found
    for folder in SCALA_CLI_LOCATIONS:
        candidate = folder / "scala-cli"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise EngineNotFound(f"The Scala Spark job needs scala-cli, which is not on PATH or in "
                         f"{', '.join(str(p) for p in SCALA_CLI_LOCATIONS)}. Install it with: {SCALA_CLI_INSTALL_HINT}")


def scala_cli_java_home(scala_cli: str | os.PathLike | None = None, jvm: str = "temurin:17",
                        timeout: float = 600) -> str:
    """Return the home of the JDK that ``scala-cli`` manages, downloading it on first use.

    Useful as ``SparkEngine(..., java_home=scala_cli_java_home())`` on a machine without a JDK.

    Raises:
        EngineNotFound: If ``scala-cli`` is missing or cannot provide the JDK.
    """
    command = [find_scala_cli(scala_cli), "run", "--jvm", jvm, "-q", "-e",
               'println(System.getProperty("java.home"))']
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines or not Path(lines[-1]).is_dir():
        raise EngineNotFound(f"scala-cli could not provide a {jvm} JDK: {result.stderr.strip()[-500:]}")
    return lines[-1]


def parse_result(stdout: str) -> dict[str, str]:
    """Parse the ``LDP_SPARK_RESULT key=value ...`` line the Scala job prints last.

    Returns:
        The key/value pairs, or an empty dict if the line is missing.
    """
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            return dict(item.split("=", 1) for item in line[len(RESULT_PREFIX):].split() if "=" in item)
    return {}


class ScalaSparkJob:
    """Run the Scala Spark job in ``spark/`` against a local Iceberg catalog.

    The job counts a table's rows with Spark SQL, computes revenue by city per day when the table
    has timestamp, city and amount columns, prints the table's snapshot history and writes the
    aggregate to a new Iceberg table in the same namespace (``CREATE OR REPLACE ... AS SELECT``).

    Args:
        config: The catalog dict of a dataset config: a ``local`` spec ``{"identifier",
            "warehouse_path"}``, whose identifier names the namespace and (unless ``catalog_name``
            is given) the catalog, or a ``sql`` spec on SQLite ``{"type": "sql", "name", "uri",
            "warehouse", "namespace"}`` (see :meth:`CatalogLocation.from_spec`).
        catalog_name: Catalog name, when it differs from the identifier.
        base_dir: Folder relative paths in ``config`` resolve against (the config file's folder).
        project_dir: The scala-cli project. Defaults to :data:`SPARK_PROJECT_DIR`.
        scala_cli: Path of ``scala-cli``. Found with :func:`find_scala_cli` when omitted.
        conf: Extra Spark settings, passed to the job as ``--conf key=value``. A ``sql`` spec's
            ``properties`` are passed the same way, before these.
        show_rows: Rows the job prints for each result.
        timeout: Seconds to wait for the job. The first run downloads a JDK, Spark and Iceberg.

    Raises:
        ConfigError: If ``config`` is missing a key or its catalog is not a SQLite file.
    """

    def __init__(self, config: Mapping[str, Any], catalog_name: str | None = None, *,
                 base_dir: str | os.PathLike | None = None, project_dir: str | os.PathLike | None = None,
                 scala_cli: str | os.PathLike | None = None, conf: Mapping[str, str] | None = None,
                 show_rows: int = 10, timeout: float | None = 1800):
        self.catalog = CatalogLocation.from_spec(config, catalog_name, base_dir)
        self.namespace = _spec_namespace(config)
        self.project_dir = Path(project_dir) if project_dir is not None else SPARK_PROJECT_DIR
        extra: dict[str, str] = {}
        if catalog_spec_type(config) != "local":
            prefix = f"spark.sql.catalog.{self.catalog.name}"
            extra = _catalog_properties(config, prefix)
        self.conf = {**extra, **dict(conf or {})}
        if not isinstance(show_rows, int) or show_rows < 1:
            raise ValueError(f"show_rows must be a positive integer, got {show_rows!r}")
        self.show_rows = show_rows
        self.timeout = timeout
        self._scala_cli = scala_cli

    @property
    def scala_cli(self) -> str:
        """Path of the ``scala-cli`` executable. Raises ``EngineNotFound`` if it is missing."""
        return find_scala_cli(self._scala_cli)

    def job_args(self, table: str, output_table: str | None = None) -> list[str]:
        """Arguments of ``IcebergJob`` for ``table`` (what follows ``--`` on the command line)."""
        if not table:
            raise ValueError("table must be a non-empty table name")
        args = ["--catalog-name", self.catalog.name, "--catalog-db", str(self.catalog.catalog_db),
                "--warehouse", self.catalog.warehouse, "--namespace", self.namespace, "--table", table]
        if output_table:
            args += ["--output-table", output_table]
        args += ["--show-rows", str(self.show_rows)]
        for key, value in self.conf.items():
            args += ["--conf", f"{key}={value}"]
        return args

    def command(self, table: str, output_table: str | None = None) -> list[str]:
        """The full ``scala-cli run`` command for ``table``."""
        return [self.scala_cli, "run", str(self.project_dir), "-q", "--suppress-outdated-dependency-warning",
                "--", *self.job_args(table, output_table)]

    def run(self, table: str, output_table: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run the job on ``<namespace>.<table>``.

        Args:
            table: Source table in the namespace.
            output_table: Table to write the aggregate to. Defaults to ``<table>_spark_summary``.
            check: Raise :class:`SparkJobError` if the job fails.

        Returns:
            The finished process, with ``stdout`` and ``stderr`` as text. :func:`parse_result`
            reads the summary line from ``stdout``.

        Raises:
            EngineNotFound: If ``scala-cli`` or the Scala project is missing.
            SparkJobError: If the job fails and ``check`` is true.
            subprocess.TimeoutExpired: If the job runs longer than ``timeout``.
        """
        return self._run(self.command(table, output_table), check)

    def print_conf(self, table: str) -> dict[str, str]:
        """The Spark settings the Scala job would use, without starting Spark."""
        result = self._run(self.command(table) + ["--print-conf"], check=True)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def _run(self, command: Sequence[str], check: bool) -> subprocess.CompletedProcess:
        if not (self.project_dir / "IcebergJob.scala").is_file():
            raise EngineNotFound(f"Scala Spark project not found at {self.project_dir}. It ships in the "
                                 "spark/ folder of a local-data-platform source checkout; pass project_dir=.")
        logger.info("Running Scala Spark job: %s", shlex.join(_redact_command(command)))
        result = subprocess.run(list(command), capture_output=True, text=True, timeout=self.timeout, check=False)
        if result.returncode != 0 and check:
            detail = (result.stderr.strip() or result.stdout.strip())[-2000:]
            raise SparkJobError(f"Scala Spark job failed with exit code {result.returncode}: {detail}",
                                result.returncode, result.stdout, result.stderr)
        summary = parse_result(result.stdout)
        if summary:
            logger.info("Scala Spark job wrote %s (%s rows) from %s (%s rows)", summary.get("target"),
                        summary.get("target_rows"), summary.get("source"), summary.get("source_rows"))
        return result

    def __repr__(self) -> str:
        return f"ScalaSparkJob(catalog={self.catalog.name!r}, namespace={self.namespace!r})"


def _import_pyspark():
    """Import ``pyspark`` or raise :class:`EngineNotFound` with an install hint."""
    try:
        import pyspark
        from pyspark.sql import SparkSession  # noqa: F401
    except ImportError as error:
        raise EngineNotFound(f"The Spark engine needs the pyspark package. Install it with: "
                             f"{PYSPARK_INSTALL_HINT}") from error
    return pyspark


def find_java_home(java_home: str | os.PathLike | None = None) -> str | None:
    """Return the JDK folder PySpark should use, or ``None`` when a working ``java`` is on ``PATH``.

    Raises:
        EngineNotFound: If no JDK is found. On macOS ``/usr/bin/java`` is only a stub, so a JDK must
            come from ``java_home``, ``JAVA_HOME`` or ``/usr/libexec/java_home``.
    """
    if java_home is not None:
        if (Path(java_home) / "bin" / "java").is_file():
            return str(java_home)
        raise EngineNotFound(f"No bin/java under java_home={str(java_home)!r}. {JAVA_HINT}")
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    if sys.platform == "darwin":
        if os.path.exists("/usr/libexec/java_home"):
            probe = subprocess.run(["/usr/libexec/java_home"], capture_output=True, text=True, check=False)
            if probe.returncode == 0 and probe.stdout.strip():
                return probe.stdout.strip()
        raise EngineNotFound(f"No JDK found. {JAVA_HINT}")
    if shutil.which("java"):
        return None
    raise EngineNotFound(f"No java on PATH. {JAVA_HINT}")


def _quote(part: str) -> str:
    """Quote one part of a Spark SQL identifier."""
    return "`" + part.replace("`", "``") + "`"


@contextmanager
def _environment(**values: str | None) -> Iterator[None]:
    """Set environment variables for the duration of the block, then restore them."""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update({key: value for key, value in values.items() if value is not None})
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class SparkEngine(Engine):
    """Run Spark SQL from Python over the local Iceberg catalog, with PySpark.

    The session runs in-process (``local[*]``). ``spark.jars.packages`` pulls the Iceberg Spark runtime
    matching the installed PySpark and the SQLite JDBC driver from Maven Central on first use, and
    :func:`spark_catalog_conf` registers the catalog under its pyiceberg name, which is also the
    default catalog. The engine then runs ``USE <catalog>.<namespace>``, so tables can be named
    ``<table>`` or ``<catalog>.<namespace>.<table>``.

    Don't write ``<namespace>.<table>``: local-data-platform names the catalog after the namespace
    (``nyc``), and Spark reads ``nyc.rides`` as table ``rides`` at the root of catalog ``nyc``.
    :meth:`qualified` builds the full name.

    Args:
        config: The catalog dict of a dataset config, ``{"identifier", "warehouse_path"}``.
        catalog_name: Catalog name, when it differs from the identifier.
        base_dir: Folder a relative ``warehouse_path`` resolves against.
        catalog: A pyiceberg SQLite catalog (such as ``LocalIcebergCatalog``) to use instead of ``config``.
        namespace: Namespace to make current. Defaults to the config ``identifier``; it is created if
            it doesn't exist. ``None`` with ``catalog=`` leaves the root namespace current.
        master: Spark master URL.
        conf: Extra Spark settings. They override the defaults.
        packages: Maven coordinates for ``spark.jars.packages``. Defaults to :func:`spark_packages`
            for the installed PySpark version.
        java_home: JDK to launch Spark with. Found with :func:`find_java_home` when omitted.

    Raises:
        EngineNotFound: If ``pyspark`` or a JDK is missing.
        ConfigError: If ``config`` is missing a key.
    """

    engine = SupportedEngine.PYSPARK

    def __init__(self, config: Mapping[str, Any] | None = None, catalog_name: str | None = None, *,
                 base_dir: str | os.PathLike | None = None, catalog: Any = None, namespace: str | None = None,
                 master: str = "local[*]", conf: Mapping[str, str] | None = None,
                 packages: Sequence[str] | None = None, java_home: str | os.PathLike | None = None):
        super().__init__("pyspark")
        pyspark = _import_pyspark()
        from pyspark.sql import SparkSession

        if catalog is not None:
            self.catalog = CatalogLocation.from_catalog(catalog)
        elif config is not None:
            self.catalog = CatalogLocation.from_config(config, catalog_name, base_dir)
            namespace = namespace or str(config["identifier"])
        else:
            raise ConfigError("SparkEngine needs a catalog config dict or a pyiceberg catalog")

        minor = ".".join(pyspark.__version__.split(".")[:2])
        if packages is None and minor not in ICEBERG_SPARK_MINORS:
            # Without this check Spark fails inside the JVM with an opaque JAVA_GATEWAY_EXITED error.
            raise EngineNotFound(f"PySpark {pyspark.__version__} has no Iceberg {ICEBERG_VERSION} Spark runtime "
                                 f"(it ships runtimes for Spark {', '.join(ICEBERG_SPARK_MINORS)}). Install "
                                 f"pyspark>=4.0,<4.2, or pass packages= with an Iceberg runtime for Spark {minor}.")
        java = find_java_home(java_home)
        settings = {
            **LOCAL_SESSION_CONF,
            "spark.jars.packages": ",".join(packages or spark_packages(pyspark.__version__)),
            "spark.pyspark.python": sys.executable,
            "spark.pyspark.driver.python": sys.executable,
            **self.catalog.conf(),
            **dict(conf or {}),
        }
        self._owns_session = SparkSession.getActiveSession() is None
        if not self._owns_session:
            logger.warning("A SparkSession is already active; static settings such as spark.jars.packages "
                           "and spark.sql.extensions will not apply to it")
        builder = SparkSession.builder.master(master).appName("local-data-platform")
        for key, value in settings.items():
            builder = builder.config(key, value)
        with _environment(JAVA_HOME=java):
            self._session = builder.getOrCreate()
        self._arrow_input = int(pyspark.__version__.split(".")[0]) >= 4  # createDataFrame(pyarrow.Table)
        self._closed = False
        self.namespace = namespace
        if namespace:
            current = self.qualified()
            self._session.sql(f"CREATE NAMESPACE IF NOT EXISTS {current}")
            self._session.sql(f"USE {current}")
        logger.info("Started PySpark %s session with catalog %r at %s (namespace %s)", pyspark.__version__,
                    self.catalog.name, self.catalog.catalog_db, namespace or "root")

    def qualified(self, table: str | None = None) -> str:
        """The quoted Spark name ``<catalog>.<namespace>[.<table>]`` in this engine's namespace."""
        parts = [self.catalog.name, *(self.namespace.split(".") if self.namespace else [])]
        if table:
            parts.append(table)
        return ".".join(_quote(part) for part in parts)

    @property
    def session(self) -> Any:
        """The underlying ``pyspark.sql.SparkSession``."""
        self._check_open()
        return self._session

    def query(self, sql: str) -> pa.Table:
        """Run a Spark SQL statement and return its result as a ``pyarrow.Table``.

        DDL and DML statements (``CREATE TABLE``, ``INSERT INTO``) run eagerly and return an empty table.
        """
        self._check_open()
        logger.debug("Running Spark SQL: %s", sql)
        df = self._session.sql(sql)
        to_arrow = getattr(df, "toArrow", None)  # PySpark >= 4.0
        if callable(to_arrow):
            return to_arrow()
        return pa.Table.from_pandas(df.toPandas(), preserve_index=False)

    def get(self, sql: str) -> pa.Table:
        """Alias of :meth:`query`, so the engine fits the ``Base.get`` interface."""
        return self.query(sql)

    def put(self, df: pa.Table, table: str) -> int:
        """Append Arrow rows to an Iceberg table with Spark, creating the table if it doesn't exist.

        Args:
            df: The rows.
            table: A table name in this engine's namespace, or a full ``catalog.namespace.table`` name.

        Returns:
            The number of rows written.

        Raises:
            TypeError: If ``df`` is not a ``pyarrow.Table``.
            ValueError: If ``df`` is empty or ``table`` is empty.
        """
        self._check_open()
        if not isinstance(df, pa.Table):
            raise TypeError(f"put() expects a pyarrow.Table, got {type(df).__name__}")
        if df.num_rows == 0:
            raise ValueError("put() got an empty table")
        if not table:
            raise ValueError("put() needs a table name")
        name = table if "." in table else self.qualified(table)
        frame = self._session.createDataFrame(df if self._arrow_input else df.to_pandas())
        writer = frame.writeTo(name)
        if self._session.catalog.tableExists(name):
            writer.append()
        else:
            writer.create()
        logger.info("Spark wrote %d rows to %s", df.num_rows, name)
        return df.num_rows

    def stop(self) -> None:
        """Stop the Spark session this engine started. Safe to call more than once.

        A session that was already active when the engine was created is left running.
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            self._session.stop()

    close = stop

    def __enter__(self) -> SparkEngine:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()

    def __repr__(self) -> str:
        state = "stopped" if self._closed else "running"
        return f"SparkEngine({state}, catalog={self.catalog.name!r})"

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("SparkEngine is stopped")


# ---------------------------------------------------------------------------------------------------
# CLI: ldp spark CONFIG
# ---------------------------------------------------------------------------------------------------

def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a number of at least 1, got {value}")
    return value


def _conf_pairs(items: Sequence[str]) -> dict[str, str]:
    conf = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"--conf expects key=value, got {item!r}")
        conf[key.strip()] = value
    return conf


def _config_iceberg_block(config: Any) -> dict[str, Any]:
    """The config's Iceberg block: the target, else the source."""
    for section in ("target", "source"):
        block = config.metadata.get(section)
        if isinstance(block, dict) and str(block.get("format", "")).strip().upper() == "ICEBERG":
            for key in ("name", "catalog"):
                if not block.get(key):
                    raise ConfigError(f"config metadata.{section} is missing '{key}'")
            return block
    raise ConfigError(f"config {config.identifier} has no Iceberg table in its target or source")


def _cmd_spark(args: argparse.Namespace) -> int:
    from local_data_platform.etl import load_config

    config = load_config(args.config)
    block = _config_iceberg_block(config)
    job = ScalaSparkJob(block["catalog"], base_dir=config.base_dir, scala_cli=args.scala_cli,
                        conf=_conf_pairs(args.conf), show_rows=args.show_rows, timeout=args.timeout)
    table = str(block["name"])
    if args.dry_run:
        sys.stdout.write(shlex.join(_redact_command(job.command(table, args.output_table))) + "\n")
        return 0
    if not job.catalog.catalog_db.is_file():
        raise TableNotFound(f"Iceberg table {job.namespace}.{table} does not exist: there is no catalog at "
                            f"{job.catalog.catalog_db}. Run 'ldp run CONFIG' first, or check the catalog config.")
    if args.print_conf:
        for key, value in sorted(redact_conf(job.print_conf(table)).items()):
            sys.stdout.write(f"{key}={value}\n")
        return 0
    result = job.run(table, output_table=args.output_table)
    sys.stdout.write(result.stdout if result.stdout.endswith("\n") or not result.stdout else result.stdout + "\n")
    return 0


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> argparse.ArgumentParser:
    """Add ``ldp spark CONFIG [--output-table TABLE]`` to an ``argparse`` subparsers object.

    The command runs :class:`ScalaSparkJob` on the config's Iceberg target (or, when the target is
    not Iceberg, its source): it counts the rows, writes the revenue-by-city-per-day aggregate (or a
    row count) to ``--output-table`` and prints the job's output. The handler is set as
    ``handler`` and returns the exit status.

    Args:
        subparsers: The object ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers for shared options, such as the CLI's ``-v``.

    Returns:
        The ``spark`` sub-parser.
    """
    parser = subparsers.add_parser(
        "spark", parents=list(parents), help="run the Scala Spark job on a config's Iceberg table",
        description="Run spark/IcebergJob.scala with scala-cli on the config's Iceberg target (or source): count "
                    "its rows, aggregate revenue by city per day and write the result back as an Iceberg table. "
                    "The first run downloads a JDK, Spark and Iceberg. Local and SQLite sql catalogs only.")
    parser.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    parser.add_argument("--output-table", metavar="TABLE",
                        help="table to write the aggregate to (default: <table>_spark_summary)")
    parser.add_argument("--show-rows", type=_positive_int, default=10, metavar="N",
                        help="rows to print for each result (default 10)")
    parser.add_argument("--conf", action="append", default=[], metavar="KEY=VALUE",
                        help="extra Spark setting; repeatable")
    parser.add_argument("--print-conf", action="store_true",
                        help="print the job's Spark settings (secrets redacted) without starting Spark")
    parser.add_argument("--dry-run", action="store_true", help="print the scala-cli command and exit")
    parser.add_argument("--scala-cli", metavar="PATH", help="the scala-cli executable (default: found on PATH)")
    parser.add_argument("--timeout", type=float, default=1800, metavar="SECONDS",
                        help="seconds to wait for the job (default 1800)")
    parser.set_defaults(handler=_cmd_spark)
    return parser


__all__ = [
    "CatalogLocation",
    "ICEBERG_SPARK_MINORS",
    "ICEBERG_VERSION",
    "PYSPARK_INSTALL_HINT",
    "SCALA_CLI_INSTALL_HINT",
    "SPARK_CATALOG_TYPES",
    "SPARK_PROJECT_DIR",
    "SPARK_VERSION",
    "SQLITE_JDBC_VERSION",
    "ScalaSparkJob",
    "SparkEngine",
    "SparkJobError",
    "add_cli",
    "catalog_spec_type",
    "find_java_home",
    "find_scala_cli",
    "jdbc_url",
    "parse_result",
    "redact_conf",
    "scala_cli_java_home",
    "spark_catalog_conf",
    "spark_catalog_conf_for",
    "spark_packages",
]
