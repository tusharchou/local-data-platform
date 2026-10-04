"""Catalogs: where table metadata lives.

:func:`create_catalog` builds a pyiceberg catalog from a config's ``target.catalog`` block. The
built-in types are ``local`` (the default: a
:class:`local_data_platform.catalog.local.iceberg.LocalIcebergCatalog`, as in 0.1.1), ``sql``,
``rest`` and ``glue``; plugins add more with :func:`register_catalog_type`. See
:mod:`local_data_platform.catalog.provider` and ``docs/catalogs.md``.
"""

from local_data_platform import Base

from .provider import (
    CatalogFactory,
    catalog_name,
    catalog_namespace,
    catalog_type,
    create_catalog,
    redact,
    register_catalog_type,
    registered_catalog_types,
)


class Catalog(Base):
    """Base class for catalogs that are not backed by pyiceberg."""


__all__ = [
    "Catalog",
    "CatalogFactory",
    "catalog_name",
    "catalog_namespace",
    "catalog_type",
    "create_catalog",
    "redact",
    "register_catalog_type",
    "registered_catalog_types",
]
