"""Catalogs that keep their metadata on the local filesystem."""

from local_data_platform.catalog import Catalog


class LocalCatalog(Catalog):
    """Base class for local catalogs that are not backed by pyiceberg."""


__all__ = ["LocalCatalog"]
