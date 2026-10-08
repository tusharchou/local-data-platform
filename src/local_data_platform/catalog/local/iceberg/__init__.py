"""A pyiceberg SQL catalog stored in a SQLite file next to its warehouse."""

import os
import weakref
from pathlib import Path

from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.typedef import Identifier

from local_data_platform.logger import get_logger

logger = get_logger(__name__)


class LocalIcebergCatalog(SqlCatalog):
    """A pyiceberg ``SqlCatalog`` backed by SQLite, with the warehouse on local disk.

    The catalog database is ``<path>/<name>_catalog.db`` and table data is written
    under ``<path>``. Both are absolute, so the catalog works the same whatever the
    current working directory is. The folder is created if it doesn't exist.

    Errors from pyiceberg and SQLAlchemy propagate with their original type.

    The SQLAlchemy engine's pooled SQLite connections are closed by :meth:`close`, or
    automatically when the catalog is garbage collected, so no ``unclosed database``
    ``ResourceWarning`` is left behind.

    Args:
        name: Catalog name. It also names the SQLite file.
        path: Warehouse folder. A relative path resolves against the cwd.
        **properties: Extra ``SqlCatalog`` properties (for example ``echo``). They
            override the defaults, including ``uri`` and ``warehouse``.
    """

    def __init__(self, name: str, path: str | os.PathLike, **properties: str):
        if not name:
            raise ValueError("LocalIcebergCatalog needs a non-empty name")
        if path is None or str(path) == "":
            raise ValueError("LocalIcebergCatalog needs a warehouse path")
        warehouse = Path(os.path.expanduser(str(path))).resolve()
        warehouse.mkdir(parents=True, exist_ok=True)
        self.warehouse_path = warehouse
        defaults = {
            "uri": f"sqlite:///{self.database_path(name, warehouse)}",
            "warehouse": f"file://{warehouse}",
        }
        logger.debug("Opening LocalIcebergCatalog %r at %s", name, defaults["uri"])
        super().__init__(name, **{**defaults, **properties})
        self._finalizer = weakref.finalize(self, self.engine.dispose)

    def close(self) -> None:
        """Close the catalog's pooled database connections. The catalog reconnects if it is used again."""
        self.engine.dispose()

    def __enter__(self) -> "LocalIcebergCatalog":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @staticmethod
    def database_path(name: str, path: str | os.PathLike) -> Path:
        """The SQLite file a catalog named ``name`` with warehouse ``path`` uses, without creating anything."""
        return Path(os.path.expanduser(str(path))).resolve() / f"{name}_catalog.db"

    @property
    def uri(self) -> str:
        """The SQLAlchemy URI of the catalog database."""
        return self.properties["uri"]

    @property
    def warehouse(self) -> str:
        """The warehouse location as a ``file://`` URI."""
        return self.properties["warehouse"]

    def get_dbs(self) -> list[Identifier]:
        """Return the namespaces in this catalog."""
        return self.list_namespaces()

    def get_tables(self, namespace: str | Identifier) -> list[Identifier]:
        """Return the table identifiers in ``namespace``."""
        return self.list_tables(namespace=namespace)


__all__ = ["LocalIcebergCatalog"]
