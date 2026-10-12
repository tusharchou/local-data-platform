"""Parquet file (or folder of files) to Iceberg table."""

from local_data_platform import Config
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.parquet import Parquet
from local_data_platform.pipeline.builders import iceberg_from_config, parquet_from_config
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.pipeline.registry import register_pipeline


@register_pipeline("PARQUET", "ICEBERG")
class ParquetToIceberg(Ingestion):
    """Load a Parquet file, or every Parquet file in a folder, into an Iceberg table.

    Before 0.1.1 this class lived under ``ingestion`` but inherited ``Egression``;
    it is now an :class:`~local_data_platform.pipeline.ingestion.Ingestion`.

    Config: ``source`` is ``{"name", "format": "PARQUET", "path"}`` and ``target`` is
    an Iceberg block. See :class:`~local_data_platform.pipeline.Pipeline` for the
    constructor arguments.
    """

    def build_source(self, config: Config) -> Parquet:
        """Build the Parquet source from ``config.source``."""
        return parquet_from_config(config, "source")

    def build_target(self, config: Config) -> Iceberg:
        """Build the Iceberg target from ``config.target``."""
        return iceberg_from_config(config, "target")


__all__ = ["ParquetToIceberg"]
