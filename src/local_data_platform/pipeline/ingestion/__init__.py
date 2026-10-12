"""Ingestion pipelines bring data into the lakehouse (CSV, Parquet or BigQuery to a table or file).

:class:`Ingestion` only labels the direction; everything else comes from
:class:`~local_data_platform.pipeline.Pipeline`.
"""

from local_data_platform.pipeline import Pipeline


class Ingestion(Pipeline):
    """Base class for pipelines that load external data into local storage."""


__all__ = ["Ingestion"]
