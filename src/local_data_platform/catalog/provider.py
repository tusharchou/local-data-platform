"""Catalog provider: build a pyiceberg catalog from a config's ``target.catalog`` block.

Contract: ``docs/design/v0_1_1_platform.md`` section C1. Built-in catalog types:

========================  =========================================================  ==========================
``type``                  Spec keys                                                  Returns
========================  =========================================================  ==========================
``local`` (the default;   ``identifier``, ``warehouse_path``                         ``LocalIcebergCatalog``,
alias ``LocalIceberg``)                                                              the original catalog
``sql`` (alias            ``uri`` (a SQLAlchemy URI), ``warehouse``, ``name``,       ``SqlCatalog``
``sqlite``)               ``password_env``, ``properties``, ``properties_env``       (JdbcCatalog-compatible)
``rest``                  ``uri``, ``warehouse``, ``name``, ``token_env``,           ``RestCatalog``
                          ``credential_env``, ``properties``, ``properties_env``
``glue``                  ``name``, ``warehouse``, ``properties``,                   ``GlueCatalog``
                          ``properties_env``
========================  =========================================================  ==========================

Every type also takes ``namespace`` (falling back to ``identifier``), the namespace tables live in;
see :func:`catalog_namespace`. Plugins add types with :func:`register_catalog_type`.

**Secrets never appear in configs or logs.** A spec names the environment variable that holds a
secret (``token_env``, ``credential_env``, ``password_env``, ``properties_env``) and the variable is
read from ``os.environ`` when :func:`create_catalog` runs. A spec key or property whose name
contains ``token``, ``secret``, ``password`` or ``credential`` and holds a literal value is
rejected, and so is a password embedded in a ``uri``. Log lines show specs through
:func:`redact`, which masks those keys and URI passwords.

pyiceberg is imported only when a catalog is built, and ``boto3`` only for ``glue``.
"""

import json
import os
import re
import sys
import weakref
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import fs
from ..exceptions import ConfigError, EngineNotFound, TableNotFound
from ..logger import get_logger
from ..paths import resolve_path

if TYPE_CHECKING:
    import argparse

    from pyiceberg.catalog import Catalog as PyIcebergCatalog

logger = get_logger(__name__)

CatalogFactory = Callable[[Mapping[str, Any], Path | None], "PyIcebergCatalog"]
"""Builds a catalog from ``(spec, base_dir)``; register one with :func:`register_catalog_type`."""

SECRET_MARKERS = ("token", "secret", "password", "credential")
"""A key whose lower-cased name contains one of these is treated as a secret."""

REDACTED = "***"
"""What :func:`redact` puts in place of a secret."""

GLUE_INSTALL_HINT = 'pip install "local-data-platform[glue]" (or pip install boto3)'

_REGISTRY: dict[str, CatalogFactory] = {}

# Looked up with "_" and "-" removed, so "LocalIceberg", "local_iceberg" and "LOCAL" all work.
_ALIASES = {"localiceberg": "local", "sqlite": "sql"}

_URI_PASSWORD = re.compile(r"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://[^:/?#@\s]*):(?P<password>[^@/?#\s]*)@")


# ---------------------------------------------------------------------- registry


def register_catalog_type(name: str) -> Callable[[CatalogFactory], CatalogFactory]:
    """Register a factory for a catalog ``type`` (case-insensitive).

    A factory registered under an existing name replaces it.

    Args:
        name: The ``type`` value that selects the factory.

    Returns:
        A decorator that registers the factory and returns it unchanged.

    Raises:
        ValueError: If ``name`` is empty.
    """
    key = str(name).strip().lower()
    if not key:
        raise ValueError("catalog type name must be non-empty")

    def decorator(factory: CatalogFactory) -> CatalogFactory:
        _REGISTRY[key] = factory
        return factory

    return decorator


def registered_catalog_types() -> list[str]:
    """Return the registered catalog type names, sorted."""
    return sorted(_REGISTRY)


def catalog_type(spec: Mapping[str, Any]) -> str:
    """Return the normalised ``type`` of a catalog spec: ``local`` when it has none."""
    raw = str(spec.get("type") or "local").strip().lower()
    return _ALIASES.get(raw.replace("_", "").replace("-", ""), raw)


def catalog_namespace(spec: Mapping[str, Any]) -> str:
    """Return the namespace a spec's tables live in.

    For ``local`` this is ``identifier`` (the original rule), falling back to ``namespace``. For every
    other type it is ``namespace``, falling back to ``identifier``.

    Raises:
        ConfigError: If the spec has neither key.
    """
    first, second = ("identifier", "namespace") if catalog_type(spec) == "local" else ("namespace", "identifier")
    ns = spec.get(first) or spec.get(second)
    if not ns:
        raise ConfigError("catalog spec needs 'identifier' (local) or 'namespace'")
    return str(ns)


def catalog_name(spec: Mapping[str, Any]) -> str:
    """Return the name the built catalog gets.

    * ``local``: ``identifier`` (it also names the SQLite file), as in the hardening contract.
    * ``sql``: ``name``, else the namespace, else ``"sql"``. The name is the ``catalog_name`` column
      of the ``iceberg_tables`` table, which a Spark ``JdbcCatalog`` on the same database must match;
      defaulting to the namespace lets a ``local`` catalog's SQLite file be opened as ``sql``.
    * other types: ``name``, else the type (``"rest"``, ``"glue"``). The name is client-side only.
    """
    kind = catalog_type(spec)
    if kind == "local":
        return str(spec.get("identifier") or spec.get("namespace") or "")
    if spec.get("name"):
        return str(spec["name"])
    if kind == "sql" and (spec.get("namespace") or spec.get("identifier")):
        return catalog_namespace(spec)
    return kind


def create_catalog(spec: Mapping[str, Any], *, base_dir: str | os.PathLike | None = None) -> "PyIcebergCatalog":
    """Return a pyiceberg catalog for ``spec``, a config's ``target.catalog`` block.

    Secrets named by ``*_env`` keys are read from ``os.environ`` now, on every call.

    Args:
        spec: The catalog spec. ``type`` defaults to ``local``; see the module docstring.
        base_dir: Folder that relative local paths in the spec resolve against, usually the
            config file's folder (default: the cwd).

    Returns:
        The catalog.

    Raises:
        ConfigError: If the spec is not an object, its type is unknown, a key is missing, it holds
            a literal secret, or a named environment variable is not set.
        EngineNotFound: If the type needs an optional dependency that is missing.
    """
    if not isinstance(spec, Mapping):
        raise ConfigError(f"catalog spec must be an object, got {type(spec).__name__}")
    kind = catalog_type(spec)
    factory = _REGISTRY.get(kind)
    if factory is None:
        raise ConfigError(f"unknown catalog type {kind!r}; registered: {registered_catalog_types()}")
    _reject_literal_secrets(spec)
    logger.debug("Creating a %s catalog from %s", kind, redact(spec))
    return factory(spec, Path(base_dir) if base_dir else None)


def catalog_database_file(spec: Mapping[str, Any], base_dir: str | os.PathLike | None = None) -> Path | None:
    """Return the SQLite file :func:`create_catalog` would open for ``spec``, creating it if missing.

    * ``local`` (and a ``sql`` block in the original local shape, with ``warehouse_path`` and no ``uri``):
      ``<warehouse_path>/<identifier>_catalog.db``.
    * ``sql`` on a ``sqlite:///`` file URI: that file, resolved as :func:`create_catalog` resolves it.
    * Anything else (in-memory SQLite, Postgres, ``rest``, ``glue``, plugin types) and incomplete
      specs: ``None``. Nothing is created.

    Read-only commands check this file exists before building the catalog, so a mistyped config
    fails instead of leaving an empty catalog behind (see :func:`require_catalog_database`).
    """
    if not isinstance(spec, Mapping):
        return None
    kind = catalog_type(spec)
    base = Path(base_dir) if base_dir else None
    if kind == "local" or (kind == "sql" and not spec.get("uri") and spec.get("warehouse_path")):
        if not (spec.get("identifier") and spec.get("warehouse_path")):
            return None
        from .local.iceberg import LocalIcebergCatalog

        return LocalIcebergCatalog.database_path(str(spec["identifier"]), resolve_path(spec["warehouse_path"], base))
    if kind == "sql" and isinstance(spec.get("uri"), str):
        return _sqlite_file(spec["uri"], base)
    return None


def require_catalog_database(spec: Mapping[str, Any], base_dir: str | os.PathLike | None = None, *,
                             table: str | None = None) -> None:
    """Raise :class:`TableNotFound` if ``spec``'s catalog is a SQLite file that doesn't exist yet.

    Call it before a read builds the catalog: building a ``local`` or SQLite ``sql`` catalog creates
    an empty database file. Catalogs without a local file (see :func:`catalog_database_file`) pass.

    Args:
        spec: The catalog spec.
        base_dir: Folder relative paths in the spec resolve against.
        table: The name of the table being read (without its namespace), for the message.
    """
    database = catalog_database_file(spec, base_dir)
    if database is not None and not database.is_file():
        subject = "There"
        if table:
            subject = f"Iceberg table {catalog_namespace(spec)}.{table} does not exist: there"
        raise TableNotFound(f"{subject} is no catalog at {database}. Run 'ldp run CONFIG' first, or check the "
                            "catalog's warehouse_path or uri.")


# ---------------------------------------------------------------------- secrets


def is_secret_key(key: Any) -> bool:
    """Return whether ``key`` names a secret: it contains ``token``, ``secret``, ``password`` or ``credential``."""
    lowered = str(key).lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def redact_uri(text: str) -> str:
    """Mask the password in any ``scheme://user:password@host`` URI inside ``text``."""
    return _URI_PASSWORD.sub(lambda m: f"{m.group('prefix')}:{REDACTED}@", text)


def redact(value: Any) -> Any:
    """Return a copy of ``value`` safe to log or print.

    Mapping values under secret-looking keys (see :func:`is_secret_key`) become ``"***"``,
    recursively, and URI passwords in strings are masked.
    """
    if isinstance(value, Mapping):
        return {key: REDACTED if is_secret_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_uri(value)
    return value


def _reject_literal_secrets(spec: Mapping[str, Any]) -> None:
    for key in spec:
        if is_secret_key(key) and not str(key).endswith("_env"):
            raise ConfigError(f"catalog spec key {key!r} holds a secret; put the value in an environment "
                              f"variable and reference it by name with '{key}_env'")
    uri = spec.get("uri")
    if isinstance(uri, str) and _URI_PASSWORD.search(uri):
        raise ConfigError(f"catalog uri {redact_uri(uri)!r} embeds a password; put it in an environment "
                          "variable and reference it with 'password_env'")


def _from_env(env_var: Any, what: str) -> str:
    name = str(env_var)
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{what} names environment variable {name!r}, which is not set")
    return value


def _secret(spec: Mapping[str, Any], key: str) -> str | None:
    """The value of the environment variable that ``spec[key]`` names, or ``None`` without ``key``."""
    env_var = spec.get(key)
    return _from_env(env_var, f"catalog {key}") if env_var else None


def _properties(spec: Mapping[str, Any]) -> dict[str, Any]:
    """``properties`` passed through (no literal secrets), plus ``properties_env`` read from the environment."""
    properties = spec.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise ConfigError(f"catalog 'properties' must be an object, got {type(properties).__name__}")
    result: dict[str, Any] = {}
    for key, value in properties.items():
        if is_secret_key(key):
            raise ConfigError(f"catalog property {key!r} holds a secret; name the environment variable that "
                              f"holds it in 'properties_env' instead, e.g. {{{json.dumps(str(key))}: \"MY_ENV_VAR\"}}")
        result[str(key)] = value
    from_env = spec.get("properties_env") or {}
    if not isinstance(from_env, Mapping):
        raise ConfigError(f"catalog 'properties_env' must be an object, got {type(from_env).__name__}")
    for key, env_var in from_env.items():
        result[str(key)] = _from_env(env_var, f"catalog properties_env[{str(key)!r}]")
    return result


# ---------------------------------------------------------------------- helpers


def _require(spec: Mapping[str, Any], key: str, kind: str) -> str:
    value = spec.get(key)
    if not value:
        raise ConfigError(f"{kind} catalog spec is missing '{key}'")
    return str(value)


def _warehouse_uri(value: Any, base_dir: Path | None) -> str:
    """A warehouse location as a URI: URIs pass through, local paths become ``file://<absolute path>``."""
    text = str(value)
    if fs.uri_scheme(text) is not None:
        return text
    path = resolve_path(text, base_dir)
    path.mkdir(parents=True, exist_ok=True)
    return f"file://{path}"


def _sqlite_file(uri: str, base_dir: Path | None) -> Path | None:
    """The database file of a ``sqlite:///`` URI, or ``None`` for other URIs and in-memory SQLite.

    ``sqlite:///relative.db`` is relative (to ``base_dir`` here) and ``sqlite:////abs.db`` absolute,
    as SQLAlchemy reads them. The absolute form is taken literally, without ``resolve_path``'s legacy
    leading-slash fallback.
    """
    if not uri.startswith("sqlite:///"):
        return None
    database = uri[len("sqlite:///"):].partition("?")[0]
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database) if os.path.isabs(database) else resolve_path(database, base_dir)


def _sql_uri(spec: Mapping[str, Any], base_dir: Path | None) -> str:
    """The SQLAlchemy URI: relative SQLite paths resolved against ``base_dir``, ``password_env`` applied."""
    uri = _require(spec, "uri", "sql")
    path = _sqlite_file(uri, base_dir)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _, sep, query = uri.partition("?")
        uri = f"sqlite:///{path}{sep}{query}"
    password = _secret(spec, "password_env")
    if password is not None:
        from sqlalchemy.engine import make_url

        uri = make_url(uri).set(password=password).render_as_string(hide_password=False)
    return uri


# ---------------------------------------------------------------------- built-in types


@register_catalog_type("local")
def _local(spec: Mapping[str, Any], base_dir: Path | None) -> "PyIcebergCatalog":
    """``LocalIcebergCatalog(identifier, path=warehouse_path)``, as in the hardening contract."""
    from .local.iceberg import LocalIcebergCatalog

    for key in ("identifier", "warehouse_path"):
        if not spec.get(key):
            raise ConfigError(f"local catalog spec is missing '{key}'")
    warehouse = resolve_path(spec["warehouse_path"], base_dir)
    return LocalIcebergCatalog(str(spec["identifier"]), path=warehouse)


@register_catalog_type("sql")
def _sql(spec: Mapping[str, Any], base_dir: Path | None) -> "PyIcebergCatalog":
    """A pyiceberg ``SqlCatalog`` on any SQLAlchemy URI (SQLite, Postgres, ...).

    Its ``iceberg_tables`` layout is the one Java's ``JdbcCatalog`` uses (``jdbc.schema-version=V1``),
    so Spark can share it when both sides use the same catalog name.
    """
    if not spec.get("uri") and spec.get("warehouse_path"):
        # The original local catalog accepted "type": "sql" / "sqlite" on an {identifier, warehouse_path} block.
        return _local(spec, base_dir)
    from pyiceberg.catalog.sql import SqlCatalog

    properties = _properties(spec)
    properties["uri"] = _sql_uri(spec, base_dir)
    if spec.get("warehouse"):
        properties["warehouse"] = _warehouse_uri(spec["warehouse"], base_dir)
    catalog = SqlCatalog(catalog_name(spec), **properties)
    # Close pooled connections when the catalog is garbage collected (no "unclosed database" warning).
    weakref.finalize(catalog, catalog.engine.dispose)
    return catalog


@register_catalog_type("rest")
def _rest(spec: Mapping[str, Any], base_dir: Path | None) -> "PyIcebergCatalog":
    """A pyiceberg ``RestCatalog``. Building it calls the server's ``GET /v1/config``.

    ``warehouse`` is passed to the server unchanged: for many REST catalogs it is a catalog name,
    not a path.
    """
    from pyiceberg.catalog.rest import RestCatalog

    properties = _properties(spec)
    properties["uri"] = _require(spec, "uri", "rest")
    if spec.get("warehouse"):
        properties["warehouse"] = str(spec["warehouse"])
    token = _secret(spec, "token_env")
    if token is not None:
        properties["token"] = token
    credential = _secret(spec, "credential_env")
    if credential is not None:
        properties["credential"] = credential
    logger.debug("Connecting to the REST catalog at %s", redact_uri(properties["uri"]))
    return RestCatalog(catalog_name(spec), **properties)


@register_catalog_type("glue")
def _glue(spec: Mapping[str, Any], base_dir: Path | None) -> "PyIcebergCatalog":
    """A pyiceberg ``GlueCatalog``. AWS credentials come from boto3's default chain."""
    try:
        from pyiceberg.catalog.glue import GlueCatalog
    except ImportError as exc:
        raise EngineNotFound(f"The glue catalog needs boto3, which is not installed: {GLUE_INSTALL_HINT}") from exc
    properties = _properties(spec)
    if spec.get("warehouse"):
        properties["warehouse"] = _warehouse_uri(spec["warehouse"], base_dir)
    return GlueCatalog(catalog_name(spec), **properties)


# ---------------------------------------------------------------------- CLI


def load_spec_file(path: str | os.PathLike) -> tuple[dict[str, Any], Path]:
    """Read a catalog spec from a JSON file.

    Args:
        path: A JSON file holding either a catalog spec (``{"type": ...}``) or a dataset config,
            whose target's (else source's) ``catalog`` block is used.

    Returns:
        ``(spec, base_dir)``, where ``base_dir`` is the file's folder.

    Raises:
        ConfigError: If the file is missing, is not valid JSON, or holds no catalog spec.
    """
    file = resolve_path(path)
    if not file.is_file():
        raise ConfigError(f"catalog spec file not found: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"catalog spec file {file} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"catalog spec file {file} must hold a JSON object")
    if "metadata" not in data:
        return data, file.parent
    from ..config import Config

    config = Config.from_dict(data, base_dir=file.parent)
    for section in ("target", "source"):
        block = config.metadata[section].get("catalog")
        if isinstance(block, dict):
            return dict(block), file.parent
    raise ConfigError(f"config {file} has no 'catalog' block in its target or source")


def _cmd_catalog_test(args: "argparse.Namespace") -> int:
    """``ldp catalog test SPEC``: connect, list namespaces and tables, print a redacted summary."""
    spec, base_dir = load_spec_file(args.spec)
    kind = catalog_type(spec)
    out = sys.stdout
    out.write(f"catalog type: {kind}\n")
    out.write(f"spec: {json.dumps(redact(spec), sort_keys=True, default=str)}\n")
    database = catalog_database_file(spec, base_dir)
    if database is not None and not database.is_file():
        # Building a local or SQLite catalog would create an empty database here.
        raise ConfigError(f"there is no {kind} catalog at {database}; run a pipeline first, or check the spec's "
                          f"{'warehouse_path' if kind == 'local' else 'uri'}")
    catalog = create_catalog(spec, base_dir=base_dir)
    try:
        namespaces = [".".join(ns) for ns in catalog.list_namespaces()]
        out.write(f"catalog name: {catalog.name}\n")
        out.write(f"namespaces ({len(namespaces)}): {', '.join(namespaces) or '-'}\n")
        namespace = spec.get("namespace") or spec.get("identifier")
        if namespace and str(namespace) in namespaces:
            tables = [".".join(identifier) for identifier in catalog.list_tables(str(namespace))]
            out.write(f"tables in {namespace} ({len(tables)}): {', '.join(tables) or '-'}\n")
    finally:
        close = getattr(catalog, "close", None)
        if callable(close):
            close()
    out.write("ok\n")
    return 0


def add_cli(subparsers: "argparse._SubParsersAction", parents: "Sequence[argparse.ArgumentParser]" = ()) -> None:
    """Add ``ldp catalog test SPEC`` to the ``ldp`` parser's subcommands.

    Args:
        subparsers: The object ``ArgumentParser.add_subparsers()`` returned. Handlers are set as
            ``handler``, taking the parsed arguments and returning the exit status.
        parents: Parent parsers for shared options, such as the CLI's ``-v``.
    """
    catalog = subparsers.add_parser(
        "catalog", parents=list(parents), help="check a catalog spec",
        description="Commands for the catalogs that Iceberg tables live in (local, sql, rest, glue).")
    commands = catalog.add_subparsers(dest="catalog_command", metavar="COMMAND")
    test = commands.add_parser(
        "test", parents=list(parents), help="connect to a catalog and list its namespaces",
        description="Build the catalog SPEC describes, list its namespaces (and the tables of the spec's "
                    "namespace) and print a summary with secrets redacted. Exit status 0 means it works.")
    test.add_argument("spec", metavar="SPEC",
                      help="a JSON file holding a catalog spec, or a dataset config with a catalog block")
    test.set_defaults(handler=_cmd_catalog_test)

    def _usage(args: "argparse.Namespace") -> int:
        catalog.print_help(sys.stderr)
        return 1

    catalog.set_defaults(handler=_usage)


__all__ = [
    "CatalogFactory",
    "REDACTED",
    "SECRET_MARKERS",
    "add_cli",
    "catalog_database_file",
    "catalog_name",
    "catalog_namespace",
    "catalog_type",
    "create_catalog",
    "is_secret_key",
    "load_spec_file",
    "redact",
    "redact_uri",
    "register_catalog_type",
    "registered_catalog_types",
    "require_catalog_database",
]
