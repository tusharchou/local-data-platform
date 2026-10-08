"""Targets: stores that data is written to."""

from local_data_platform.store import Store


class Target(Store):
    """Base class for targets."""


__all__ = ["Target"]
