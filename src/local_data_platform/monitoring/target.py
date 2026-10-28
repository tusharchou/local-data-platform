"""The table that a monitor looks at, loaded without writing anything.

``ldp monitor`` only reads. It never writes to a table of a user, and it never creates a catalog or a namespace.
Building an :class:`~local_data_platform.format.iceberg.Iceberg` object from a config would create the namespace, so
this module asks the catalog for the table directly, after it has checked that a local catalog is there. A table that
is not there is not an error of the tool. It is what a monitor reports, as a table that was never written.

This module needs only the standard library when it is imported. pyiceberg is imported when a table is loaded.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Target:
    """The table that the SLA of a config is about.

    Attributes:
        pipeline: The identifier of the config.
        identifier: The table, such as ``shop.orders``, or the path of the file of a target that is a file.
        iceberg: Whether the target is an Iceberg table. A file has no commits, so a monitor cannot read its age.
        table: The pyiceberg table, or ``None`` when it does not exist yet or the target is a file.
        why: When there is no table, why, in a few words.
    """

    pipeline: str
    identifier: str | None
    iceberg: bool
    table: Any = None
    why: str = ""

    @property
    def table_uuid(self) -> str | None:
        """The UUID of the table, or ``None`` when there is none."""
        return str(self.table.metadata.table_uuid) if self.table is not None else None


def load_target(config: Any) -> Target:
    """Load the target table of a config for reading.

    Args:
        config: A :class:`~local_data_platform.Config`.

    Returns:
        The :class:`Target`. Its ``table`` is ``None`` when the target is a file, when the catalog is a local file that
        does not exist yet, and when the catalog has no such table.
    """
    block = config.metadata["target"]
    kind = str(block.get("format", "")).strip().upper()
    if kind != "ICEBERG":
        return Target(pipeline=config.identifier, identifier=block.get("path") or block.get("name"), iceberg=False,
                      why=f"the target is a {kind} file, not an Iceberg table")
    from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

    from local_data_platform.catalog.provider import catalog_database_file, catalog_namespace, create_catalog

    spec = block["catalog"]
    identifier = f"{catalog_namespace(spec)}.{block['name']}"
    database = catalog_database_file(spec, config.base_dir)
    if database is not None and not database.is_file():
        return Target(pipeline=config.identifier, identifier=identifier, iceberg=True,
                      why="the catalog does not exist yet")
    catalog = create_catalog(spec, base_dir=config.base_dir)
    try:
        table = catalog.load_table(identifier)
    except (NoSuchTableError, NoSuchNamespaceError):
        return Target(pipeline=config.identifier, identifier=identifier, iceberg=True,
                      why="the table does not exist in its catalog")
    return Target(pipeline=config.identifier, identifier=identifier, iceberg=True, table=table)
