"""One sheet of an Excel workbook to a CSV file."""

from local_data_platform import Config
from local_data_platform.format.csv import CSV
from local_data_platform.format.excel import Excel
from local_data_platform.pipeline.builders import csv_from_config, excel_from_config
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.pipeline.registry import register_pipeline


@register_pipeline("XLSX", "CSV")
class ExcelToCSV(Ingestion):
    """Turn a sheet of an ``.xlsx`` workbook into a CSV file, replaced atomically.

    Config: ``source`` is ``{"name", "format": "XLSX", "path"}`` and, optionally, ``sheet`` (a name, or a
    position from 0) and ``header_row``. ``target`` is ``{"name", "format": "CSV", "path"}``, and ``path`` can
    be any folder, such as the one Google Drive for desktop syncs. The Excel reader is an optional extra:
    ``pip install "local-data-platform[excel]"``. See :class:`~local_data_platform.pipeline.Pipeline` for the
    constructor arguments.
    """

    def build_source(self, config: Config) -> Excel:
        """Build the Excel source from ``config.source``."""
        return excel_from_config(config, "source")

    def build_target(self, config: Config) -> CSV:
        """Build the CSV target from ``config.target``."""
        return csv_from_config(config, "target")


__all__ = ["ExcelToCSV"]
