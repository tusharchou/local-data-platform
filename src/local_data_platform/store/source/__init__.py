"""Sources: stores that data is read from."""

from local_data_platform.store import Store


class Source(Store):
    """Base class for sources."""


__all__ = ["Source"]
