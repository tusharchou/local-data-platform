"""Find the Iceberg tables an MCP server exposes, and apply the allowlist.

Tables come from two kinds of source:

* **Dataset configs.** A JSON config file, or a folder of them (``*.json`` directly inside
  it, not recursive). Every ``source`` or ``target`` block with ``format: ICEBERG`` names a
  table in its ``catalog``.
* **Catalog specs.** A JSON file holding a ``target.catalog`` style spec (or
  ``{"catalog": {...}}``). Every table in every top-level namespace of that catalog is found.

Discovery only reads. A ``local`` catalog whose SQLite file does not exist yet is reported as
unavailable instead of being created. Tables in the ``_ldp`` system namespace are skipped
unless an allowlist pattern names ``_ldp`` explicitly.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.catalog.provider import catalog_database_file, catalog_namespace, create_catalog
from local_data_platform.config import Config
from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

logger = get_logger(__name__)

SYSTEM_NAMESPACE = "_ldp"
"""The namespace LDP writes run events, quality results and audit rows to."""

_SECRET_MARKERS = ("token", "secret", "password", "credential")


@dataclass
class CatalogHandle:
    """One catalog the server reads from.

    Attributes:
        key: A stable key identifying the catalog (the SQLite file for ``local``).
        spec: The catalog spec, as written in the config (env var names only, no secrets).
        base_dir: The folder relative paths in ``spec`` resolve against.
        catalog: The pyiceberg catalog.
        warehouse: The local warehouse folder, or ``None`` for a remote warehouse.
    """

    key: str
    spec: Mapping[str, Any]
    base_dir: Path | None
    catalog: Any
    warehouse: Path | None

    def __repr__(self) -> str:
        return f"CatalogHandle(key={self.key!r}, spec={redact(self.spec)!r})"


@dataclass
class ExposedTable:
    """A table the server may serve.

    Attributes:
        identifier: ``"<namespace>.<name>"``, the name every tool takes.
        namespace: The namespace parts.
        name: The table name.
        catalog: The catalog the table lives in.
        source: Where the table was found: a config file or a catalog spec file.
    """

    identifier: str
    namespace: tuple[str, ...]
    name: str
    catalog: CatalogHandle = field(repr=False)
    source: str = ""

    def load(self) -> Any:
        """Load the pyiceberg table from its catalog (fresh metadata every call)."""
        return self.catalog.catalog.load_table((*self.namespace, self.name))


@dataclass
class Discovery:
    """What :func:`discover_tables` found.

    Attributes:
        tables: The tables to serve, in discovery order, after the allowlist.
        unavailable: Allowed tables that cannot be served, each ``{"table", "reason", "source"}``.
        catalogs: Every catalog that was opened.
    """

    tables: list[ExposedTable] = field(default_factory=list)
    unavailable: list[dict[str, str]] = field(default_factory=list)
    catalogs: list[CatalogHandle] = field(default_factory=list)

    def warehouses(self) -> list[Path]:
        """The distinct local warehouse folders, in discovery order."""
        seen: list[Path] = []
        for handle in self.catalogs:
            if handle.warehouse is not None and handle.warehouse not in seen:
                seen.append(handle.warehouse)
        return seen

    def close(self) -> None:
        """Close the catalogs that support it (the local SQLite catalogs)."""
        for handle in self.catalogs:
            close = getattr(handle.catalog, "close", None)
            if callable(close):
                close()


def redact(value: Any) -> Any:
    """Return ``value`` with every mapping value whose key looks secret replaced by ``"***"``."""
    if isinstance(value, Mapping):
        return {key: "***" if any(marker in str(key).lower() for marker in _SECRET_MARKERS) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def parse_allowlist(allow: str | Iterable[str] | None) -> list[str] | None:
    """Split an allowlist into patterns.

    Args:
        allow: ``None`` (serve every discovered table), a comma-separated string, or an
            iterable of strings that may themselves be comma-separated.

    Returns:
        The patterns, or ``None`` when no allowlist was given.

    Raises:
        ConfigError: If an allowlist was given but holds no patterns.
    """
    if allow is None:
        return None
    items = [allow] if isinstance(allow, str) else list(allow)
    patterns = [part.strip() for item in items for part in str(item).split(",") if part.strip()]
    if not patterns:
        raise ConfigError("the table allowlist is empty; pass table identifiers such as 'demo.rides' or 'demo.*'")
    return patterns


def is_allowed(identifier: str, patterns: Sequence[str] | None) -> bool:
    """Return whether ``identifier`` passes the allowlist.

    With no allowlist every table is allowed except those in the ``_ldp`` namespace. A
    pattern matches the full identifier (``demo.rides``, ``demo.*``) or, when it has no dot,
    the bare table name (``rides``). ``_ldp`` tables need a pattern that starts with ``_ldp``.
    """
    namespace, _, name = identifier.rpartition(".")
    system = namespace.split(".")[0] == SYSTEM_NAMESPACE
    if patterns is None:
        return not system
    for pattern in patterns:
        if system and not pattern.startswith(SYSTEM_NAMESPACE):
            continue
        if fnmatch.fnmatchcase(identifier, pattern) or ("." not in pattern and fnmatch.fnmatchcase(name, pattern)):
            return True
    return False


def config_files(path: str | os.PathLike) -> list[Path]:
    """Return the config files at ``path``: the file itself, or the ``*.json`` files in a folder.

    Raises:
        ConfigError: If ``path`` does not exist.
    """
    resolved = resolve_path(path)
    if resolved.is_dir():
        return sorted(item for item in resolved.glob("*.json") if item.is_file())
    if resolved.is_file():
        return [resolved]
    raise ConfigError(f"config path not found: {resolved}")


class _Unavailable(LDPError):
    """A table or catalog that exists in a config but cannot be served."""


class _Catalogs:
    """Open each distinct catalog once."""

    def __init__(self) -> None:
        self.handles: dict[str, CatalogHandle] = {}

    def get(self, spec: Mapping[str, Any], base_dir: Path | None) -> CatalogHandle:
        if not isinstance(spec, Mapping):
            raise ConfigError(f"catalog spec must be an object, got {type(spec).__name__}")
        kind = catalog_type(spec)
        warehouse = _local_warehouse(spec, base_dir)
        if kind == "local" and (not spec.get("identifier") or not spec.get("warehouse_path")):
            raise ConfigError("local catalog spec needs 'identifier' and 'warehouse_path'")
        # A local or SQLite catalog that doesn't exist yet would be created empty by create_catalog.
        database = catalog_database_file(spec, base_dir)
        if kind == "local":
            key = f"local:{database}"
        else:
            key = f"{kind}:{json.dumps(redact(dict(spec)), sort_keys=True, default=str)}@{base_dir}"
        if database is not None and key not in self.handles and not database.is_file():
            raise _Unavailable(f"there is no catalog at {database} yet; run the pipeline first")
        if key not in self.handles:
            catalog = create_catalog(spec, base_dir=base_dir)
            self.handles[key] = CatalogHandle(key=key, spec=dict(spec), base_dir=base_dir, catalog=catalog,
                                              warehouse=warehouse)
            logger.debug("Opened catalog %s", key)
        return self.handles[key]


def catalog_type(spec: Mapping[str, Any]) -> str:
    """The catalog ``type`` of ``spec``, lower-cased; ``local`` when absent (``LocalIceberg`` is an alias)."""
    kind = str(spec.get("type", "local")).replace("_", "").lower()
    return "local" if kind == "localiceberg" else kind


def _local_warehouse(spec: Mapping[str, Any], base_dir: Path | None) -> Path | None:
    kind = catalog_type(spec)
    if kind == "local" and spec.get("warehouse_path"):
        return resolve_path(str(spec["warehouse_path"]), base_dir)
    warehouse = spec.get("warehouse")
    if isinstance(warehouse, str) and warehouse:
        if warehouse.startswith("file://"):
            return Path(warehouse[len("file://"):]).resolve()
        if "://" not in warehouse:
            return resolve_path(warehouse, base_dir)
    return None


def _split_namespace(namespace: str) -> tuple[str, ...]:
    return tuple(part for part in str(namespace).split(".") if part)


def discover_tables(configs: Iterable[str | os.PathLike] = (), catalogs: Iterable[str | os.PathLike] = (), *,
                    allow: str | Iterable[str] | None = None) -> Discovery:
    """Find the tables to serve.

    Args:
        configs: Config files, or folders of ``*.json`` config files.
        catalogs: Catalog spec JSON files.
        allow: The table allowlist (see :func:`is_allowed`); ``None`` serves every table.

    Returns:
        A :class:`Discovery`. An identifier found twice (in two catalogs) is served from the
        first and reported unavailable for the second.

    Raises:
        ConfigError: If a path does not exist, an explicitly named config file is invalid,
            or a catalog spec file is not a JSON object.
    """
    patterns = parse_allowlist(allow)
    opened = _Catalogs()
    found: dict[str, ExposedTable] = {}
    unavailable: list[dict[str, str]] = []

    def offer(identifier: str, namespace: tuple[str, ...], name: str, handle: CatalogHandle, source: str) -> None:
        if not is_allowed(identifier, patterns):
            logger.debug("Table %s from %s is not on the allowlist", identifier, source)
            return
        if identifier in found:
            if found[identifier].catalog is not handle:
                unavailable.append({"table": identifier, "source": source,
                                    "reason": f"duplicate identifier; already served from {found[identifier].source}"})
            return
        found[identifier] = ExposedTable(identifier, namespace, name, handle, source)

    for path in configs:
        explicit = resolve_path(path).is_file()
        for file in config_files(path):
            try:
                config = Config.from_json(file)
            except ConfigError as error:
                if explicit:
                    raise
                logger.debug("Skipping %s: not a dataset config (%s)", file, error)
                continue
            for section in ("target", "source"):
                block = config.metadata.get(section) or {}
                if str(block.get("format", "")).strip().upper() != "ICEBERG":
                    continue
                name, spec = block.get("name"), block.get("catalog")
                if not name or not isinstance(spec, Mapping):
                    logger.warning("Skipping %s.%s in %s: it needs 'name' and a 'catalog' object", config.identifier,
                                   section, file)
                    continue
                namespace = _split_namespace(catalog_namespace(spec))
                identifier = ".".join((*namespace, str(name)))
                if not is_allowed(identifier, patterns):
                    continue
                try:
                    handle = opened.get(spec, config.base_dir)
                    if not handle.catalog.table_exists((*namespace, str(name))):
                        raise _Unavailable("the table does not exist yet; run the pipeline first")
                except _Unavailable as error:
                    unavailable.append({"table": identifier, "source": str(file), "reason": str(error)})
                    continue
                offer(identifier, namespace, str(name), handle, str(file))

    for path in catalogs:
        file = resolve_path(path)
        if not file.is_file():
            raise ConfigError(f"catalog spec file not found: {file}")
        try:
            data = json.loads(file.read_text())
        except json.JSONDecodeError as error:
            raise ConfigError(f"catalog spec {file} is not valid JSON: {error}") from error
        spec = data.get("catalog", data) if isinstance(data, Mapping) else data
        if not isinstance(spec, Mapping):
            raise ConfigError(f"catalog spec {file} must be a JSON object")
        try:
            handle = opened.get(spec, file.parent)
        except _Unavailable as error:
            unavailable.append({"table": "*", "source": str(file), "reason": str(error)})
            continue
        for namespace in handle.catalog.list_namespaces():
            for table_identifier in handle.catalog.list_tables(namespace):
                parts = tuple(table_identifier)
                offer(".".join(parts), parts[:-1], parts[-1], handle, str(file))

    discovery = Discovery(tables=list(found.values()), unavailable=unavailable, catalogs=list(opened.handles.values()))
    logger.info("Discovered %d tables (%d unavailable) in %d catalogs", len(discovery.tables),
                len(discovery.unavailable), len(discovery.catalogs))
    return discovery


__all__ = [
    "CatalogHandle",
    "Discovery",
    "ExposedTable",
    "SYSTEM_NAMESPACE",
    "config_files",
    "discover_tables",
    "is_allowed",
    "parse_allowlist",
    "redact",
]
