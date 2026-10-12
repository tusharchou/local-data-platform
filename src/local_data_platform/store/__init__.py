"""Stores: datasets that live outside the lakehouse (sources and targets)."""

from local_data_platform import Table


class Store(Table):
    """Base class for sources and targets."""


__all__ = ["Store"]
