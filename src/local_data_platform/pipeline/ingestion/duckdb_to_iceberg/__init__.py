"""A table or query in a DuckDB file to an Iceberg table.

This is the one way to publish SQL-built data to Iceberg, so it gets the same quality gate, run
events and snapshots as ingested data. Later milestones extend this route instead of adding
another SQL format.
"""

from local_data_platform import Config
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.builders import duckdb_from_config, iceberg_from_config
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.pipeline.registry import register_pipeline
from local_data_platform.store.source.duckdb import DuckDBTable


@register_pipeline("DUCKDB", "ICEBERG")
class DuckDBToIceberg(Ingestion):
    """Load a table, or the result of one query, from a DuckDB file into an Iceberg table.

    Config: ``source`` is ``{"name", "format": "DUCKDB", "path", "relation"}`` or
    ``{"name", "format": "DUCKDB", "path", "sql"}``, and ``target`` is an Iceberg block. The file is
    opened read-only and closed before the Iceberg write starts. See
    :class:`~local_data_platform.pipeline.Pipeline` for the constructor arguments.
    """

    def build_source(self, config: Config) -> DuckDBTable:
        """Build the DuckDB source from ``config.source``."""
        return duckdb_from_config(config, "source")

    def build_target(self, config: Config) -> Iceberg:
        """Build the Iceberg target from ``config.target``."""
        return iceberg_from_config(config, "target")


__all__ = ["DuckDBToIceberg"]
