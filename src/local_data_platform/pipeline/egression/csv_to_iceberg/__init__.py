"""Deprecated import path for :class:`~local_data_platform.pipeline.ingestion.csv_to_iceberg.CSVToIceberg`.

Loading a CSV file into Iceberg brings data *into* the lakehouse, so the class
lives under ``pipeline.ingestion``. This alias will be removed in 0.2.0.
"""

import warnings

from local_data_platform.pipeline.ingestion.csv_to_iceberg import CSVToIceberg as _CSVToIceberg


class CSVToIceberg(_CSVToIceberg):
    """Deprecated alias of the ingestion ``CSVToIceberg``; it warns when built.

    Use ``create_pipeline(config)`` or
    ``local_data_platform.pipeline.ingestion.csv_to_iceberg.CSVToIceberg`` instead.
    It is not registered: the registry routes CSV to ICEBERG to the ingestion class.
    """

    def __init__(self, config=None, **kwargs):
        warnings.warn(
            "local_data_platform.pipeline.egression.csv_to_iceberg.CSVToIceberg is deprecated and will be "
            "removed in 0.2.0; use create_pipeline(config) or local_data_platform.pipeline.ingestion.csv_to_iceberg."
            "CSVToIceberg",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(config, **kwargs)


__all__ = ["CSVToIceberg"]
