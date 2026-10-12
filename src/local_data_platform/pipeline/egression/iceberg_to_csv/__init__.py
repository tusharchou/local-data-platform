"""Iceberg table to CSV file."""

from local_data_platform import Config
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.builders import csv_from_config, iceberg_from_config
from local_data_platform.pipeline.egression import Egression
from local_data_platform.pipeline.registry import register_pipeline


@register_pipeline("ICEBERG", "CSV")
class IcebergToCSV(Egression):
    """Export the current snapshot of an Iceberg table to a CSV file.

    Config: ``source`` is ``{"name", "format": "ICEBERG", "catalog"}`` and ``target``
    is ``{"name", "format": "CSV", "path"}``. The CSV file is replaced atomically on
    every run. See :class:`~local_data_platform.pipeline.Pipeline` for the
    constructor arguments.
    """

    def build_source(self, config: Config) -> Iceberg:
        """Build the Iceberg source from ``config.source``."""
        return iceberg_from_config(config, "source")

    def build_target(self, config: Config) -> CSV:
        """Build the CSV target from ``config.target``."""
        return csv_from_config(config, "target")


__all__ = ["IcebergToCSV"]
