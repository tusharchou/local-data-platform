"""Iceberg table to Parquet file."""

from local_data_platform import Config
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.parquet import Parquet
from local_data_platform.pipeline.builders import iceberg_from_config, parquet_from_config
from local_data_platform.pipeline.egression import Egression
from local_data_platform.pipeline.registry import register_pipeline


@register_pipeline("ICEBERG", "PARQUET")
class IcebergToParquet(Egression):
    """Export the current snapshot of an Iceberg table to a single Parquet file.

    Config: ``source`` is ``{"name", "format": "ICEBERG", "catalog"}`` and ``target``
    is ``{"name", "format": "PARQUET", "path"}``. The file is replaced atomically on
    every run. See :class:`~local_data_platform.pipeline.Pipeline` for the
    constructor arguments.
    """

    def build_source(self, config: Config) -> Iceberg:
        """Build the Iceberg source from ``config.source``."""
        return iceberg_from_config(config, "source")

    def build_target(self, config: Config) -> Parquet:
        """Build the Parquet target from ``config.target``."""
        return parquet_from_config(config, "target")


__all__ = ["IcebergToParquet"]
