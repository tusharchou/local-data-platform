"""The built-in pipelines. Importing this module registers them.

:func:`local_data_platform.pipeline.registry.create_pipeline` imports it on the
first lookup, so callers don't have to.

| Route | Pipeline |
|---|---|
| CSV -> ICEBERG | :class:`CSVToIceberg` |
| PARQUET -> ICEBERG | :class:`ParquetToIceberg` |
| ICEBERG -> CSV | :class:`IcebergToCSV` |
| ICEBERG -> PARQUET | :class:`IcebergToParquet` |
| JSON -> CSV, engine BIGQUERY | :class:`BigQueryToCSV` |
"""

from local_data_platform.pipeline.egression.iceberg_to_csv import IcebergToCSV
from local_data_platform.pipeline.egression.iceberg_to_parquet import IcebergToParquet
from local_data_platform.pipeline.ingestion.bigquery_to_csv import BigQueryToCSV
from local_data_platform.pipeline.ingestion.csv_to_iceberg import CSVToIceberg
from local_data_platform.pipeline.ingestion.parquet_to_iceberg import ParquetToIceberg

BUILTIN_PIPELINES = (CSVToIceberg, ParquetToIceberg, IcebergToCSV, IcebergToParquet, BigQueryToCSV)
"""Every built-in pipeline class."""

__all__ = ["BUILTIN_PIPELINES", "BigQueryToCSV", "CSVToIceberg", "IcebergToCSV", "IcebergToParquet",
           "ParquetToIceberg"]
