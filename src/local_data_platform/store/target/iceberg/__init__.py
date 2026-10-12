"""Backward-compatible import path for the Iceberg format.

Before 0.1.1 this module held a second, diverging ``Iceberg`` class. It now
re-exports :class:`local_data_platform.format.iceberg.Iceberg`, so both import
paths give the same class.
"""

from local_data_platform.format.iceberg import Iceberg, WriteResult

__all__ = ["Iceberg", "WriteResult"]
