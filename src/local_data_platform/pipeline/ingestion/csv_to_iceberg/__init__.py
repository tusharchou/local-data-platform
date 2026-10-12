"""CSV file to Iceberg table."""

from local_data_platform import Config
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.builders import csv_from_config, iceberg_from_config
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.pipeline.registry import register_pipeline


@register_pipeline("CSV", "ICEBERG")
class CSVToIceberg(Ingestion):
    """Load a CSV file into an Iceberg table.

    Config: ``source`` is ``{"name", "format": "CSV", "path"}`` and ``target`` is an
    Iceberg block (``name``, ``catalog``, and optionally ``write_mode``, ``join_cols``
    and ``partition_by``). See :class:`~local_data_platform.pipeline.Pipeline` for the
    constructor arguments.
    """

    def build_source(self, config: Config) -> CSV:
        """Build the CSV source from ``config.source``."""
        return csv_from_config(config, "source")

    def build_target(self, config: Config) -> Iceberg:
        """Build the Iceberg target from ``config.target``."""
        return iceberg_from_config(config, "target")


__all__ = ["CSVToIceberg"]
