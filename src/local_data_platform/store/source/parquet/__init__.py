"""Backward-compatible import path for the Parquet format.

Before 0.1.1 this module held a second, diverging ``Parquet`` class. It now
re-exports :class:`local_data_platform.format.parquet.Parquet`, so both import
paths give the same class.
"""

from local_data_platform.format.parquet import Parquet

__all__ = ["Parquet"]
