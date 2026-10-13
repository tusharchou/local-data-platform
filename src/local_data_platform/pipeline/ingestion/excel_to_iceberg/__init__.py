"""One sheet of an Excel workbook to an Iceberg table."""

from local_data_platform import Config
from local_data_platform.format.excel import Excel
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.builders import excel_from_config, iceberg_from_config
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.pipeline.registry import register_pipeline


@register_pipeline("XLSX", "ICEBERG")
class ExcelToIceberg(Ingestion):
    """Load a sheet of an ``.xlsx`` workbook into an Iceberg table.

    Config: ``source`` is ``{"name", "format": "XLSX", "path"}`` and, optionally, ``sheet`` (a name, or a
    position from 0) and ``header_row``. ``target`` is an Iceberg block (``name``, ``catalog``, and optionally
    ``write_mode``, ``join_cols`` and ``partition_by``). The Excel reader is an optional extra:
    ``pip install "local-data-platform[excel]"``. See :class:`~local_data_platform.pipeline.Pipeline` for the
    constructor arguments.
    """

    def build_source(self, config: Config) -> Excel:
        """Build the Excel source from ``config.source``."""
        return excel_from_config(config, "source")

    def build_target(self, config: Config) -> Iceberg:
        """Build the Iceberg target from ``config.target``."""
        return iceberg_from_config(config, "target")


__all__ = ["ExcelToIceberg"]
