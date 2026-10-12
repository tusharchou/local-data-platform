"""Build sources and targets from a config's ``metadata.source`` or ``metadata.target`` block.

Every relative path in a block resolves against the config's ``base_dir`` (the
config file's folder). The built-in pipelines and the ``ldp`` CLI use these
functions, so a block means the same thing everywhere.
"""

from collections.abc import Iterable
from typing import Any

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.parquet import Parquet
from local_data_platform.logger import get_logger
from local_data_platform.store.source.gcp.bigquery import BigQuery, GCPCredentials

logger = get_logger(__name__)

SECTIONS = ("source", "target")

_FILE_KEYS = {"name", "format", "path"}
_ICEBERG_SOURCE_KEYS = {"name", "format", "catalog"}
_ICEBERG_TARGET_KEYS = _ICEBERG_SOURCE_KEYS | {"write_mode", "join_cols", "partition_by", "schema_evolution"}
# ``path`` on an Iceberg block is a pre-0.1.1 leftover that Iceberg ignores.
_ICEBERG_LEGACY_KEYS = {"path"}
_BIGQUERY_KEYS = {"name", "format", "engine", "path", "credentials"}


def config_block(config: Config, section: str, expected_format: str | None = None,
                 known_keys: Iterable[str] | None = None) -> dict[str, Any]:
    """Return ``config.metadata[section]`` after checking its format and keys.

    Args:
        config: The dataset config.
        section: ``"source"`` or ``"target"``.
        expected_format: When given, the block's ``format`` must match it (case-insensitive).
        known_keys: When given, keys outside this set are logged as ignored.

    Returns:
        The block.

    Raises:
        ConfigError: If the section is unknown or missing, or the format doesn't match.
    """
    if section not in SECTIONS:
        raise ConfigError(f"config section must be 'source' or 'target', got {section!r}")
    block = config.metadata.get(section)
    if not isinstance(block, dict):
        raise ConfigError(f"config metadata is missing the '{section}' object")
    actual = str(block.get("format", "")).strip().upper()
    if expected_format is not None and actual != expected_format.upper():
        raise ConfigError(f"config metadata.{section}.format is {block.get('format')!r}, expected {expected_format!r}")
    if known_keys is not None:
        unknown = sorted(set(block) - set(known_keys))
        if unknown:
            logger.warning("config %s: metadata.%s keys %s are not used and are ignored",
                           config.identifier, section, unknown)
    return block


def _require(block: dict[str, Any], key: str, section: str) -> Any:
    value = block.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f"config metadata.{section} is missing '{key}'")
    return value


def csv_from_config(config: Config, section: str = "source") -> CSV:
    """Build a :class:`~local_data_platform.format.csv.CSV` from ``{"name", "format": "CSV", "path"}``."""
    block = config_block(config, section, "CSV", _FILE_KEYS)
    return CSV(name=_require(block, "name", section), path=_require(block, "path", section),
               base_dir=config.base_dir)


def parquet_from_config(config: Config, section: str = "source") -> Parquet:
    """Build a :class:`~local_data_platform.format.parquet.Parquet` from ``{"name", "format": "PARQUET", "path"}``."""
    block = config_block(config, section, "PARQUET", _FILE_KEYS)
    return Parquet(name=_require(block, "name", section), path=_require(block, "path", section),
                   base_dir=config.base_dir)


def iceberg_from_config(config: Config, section: str = "target", *, must_exist: bool = False) -> Iceberg:
    """Build an :class:`~local_data_platform.format.iceberg.Iceberg` table from a config block.

    The block is ``{"name", "format": "ICEBERG", "catalog": {"identifier",
    "warehouse_path"}}``. A target block may also set ``write_mode``, ``join_cols``,
    ``partition_by`` and ``schema_evolution``; a source block only reads, so those
    keys are not passed for it. ``warehouse_path`` resolves against the config's folder.

    Args:
        config: The dataset config.
        section: ``"target"`` or ``"source"``.
        must_exist: For read-only callers. When the catalog is a SQLite file (``local``, or ``sql``
            on a ``sqlite:///`` file) that doesn't exist, raise ``TableNotFound`` instead of letting
            the catalog create an empty database.

    Raises:
        ConfigError: If the block is incomplete or invalid.
        TableNotFound: With ``must_exist``, if the catalog database doesn't exist.
    """
    known = _ICEBERG_TARGET_KEYS if section == "target" else _ICEBERG_SOURCE_KEYS
    block = config_block(config, section, "ICEBERG", known | _ICEBERG_LEGACY_KEYS)
    name = _require(block, "name", section)
    catalog = _require(block, "catalog", section)
    if must_exist and isinstance(catalog, dict):
        from local_data_platform.catalog.provider import require_catalog_database

        require_catalog_database(catalog, config.base_dir, table=name)
    if section == "source":
        return Iceberg(name=name, config=catalog, base_dir=config.base_dir)
    return Iceberg(
        name=name,
        config=catalog,
        partition_by=block.get("partition_by"),
        write_mode=block.get("write_mode"),
        join_cols=block.get("join_cols"),
        base_dir=config.base_dir,
        schema_evolution=block.get("schema_evolution", True),
    )


def bigquery_from_config(config: Config, section: str = "source", client: Any = None) -> BigQuery:
    """Build a :class:`~local_data_platform.store.source.gcp.bigquery.BigQuery` source.

    The block keeps its pre-0.1.1 shape: ``{"name", "format": "JSON", "engine":
    "BIGQUERY", "path": <JSON file holding {"query": ...}>, "credentials": {"name",
    "path"}}``. Both paths resolve against the config's folder (``~`` is expanded).
    The query is read from ``path`` when the source is read, not here.

    Args:
        config: The dataset config.
        section: The block to read, normally ``"source"``.
        client: A ready BigQuery client (or a test double). When omitted, one is
            built from the credentials on the first query.

    Raises:
        ConfigError: If the block is incomplete, or the credentials file is missing
            or not valid JSON.
    """
    block = config_block(config, section, "JSON", _BIGQUERY_KEYS)
    engine = str(block.get("engine", "")).strip().upper()
    if engine != "BIGQUERY":
        raise ConfigError(f"config metadata.{section}.engine must be 'BIGQUERY' for a BigQuery source, "
                          f"got {block.get('engine')!r}")
    credentials = None
    credentials_block = block.get("credentials")
    if credentials_block is not None:
        if not isinstance(credentials_block, dict):
            raise ConfigError(f"config metadata.{section}.credentials must be an object with a 'path'")
        key_path = _require(credentials_block, "path", f"{section}.credentials")
        credentials = GCPCredentials(key_path, base_dir=config.base_dir)
    elif client is None:
        logger.warning("config %s: BigQuery source has no credentials; the query will fail unless a client is given",
                       config.identifier)
    return BigQuery(name=_require(block, "name", section), credentials=credentials,
                    path=_require(block, "path", section), client=client, base_dir=config.base_dir)


__all__ = [
    "bigquery_from_config",
    "config_block",
    "csv_from_config",
    "iceberg_from_config",
    "parquet_from_config",
]
