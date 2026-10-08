"""Egression pipelines take data out of the lakehouse (Iceberg to CSV or Parquet).

:class:`Egression` only labels the direction; everything else comes from
:class:`~local_data_platform.pipeline.Pipeline`.
"""

from local_data_platform.pipeline import Pipeline


class Egression(Pipeline):
    """Base class for pipelines that export lakehouse tables to files."""


__all__ = ["Egression"]
