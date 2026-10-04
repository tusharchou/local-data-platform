"""Query engines.

:class:`Engine` is the base for every engine. Concrete engines live in subpackages,
for example :class:`local_data_platform.engine.duckdb.DuckDBEngine`, and
:mod:`local_data_platform.engine.router` picks one for a scan. Importing this
package never imports an optional engine dependency.
"""

from typing import Any

from local_data_platform import Worker


class Engine(Worker):
    """Base class for query engines.

    Args:
        name: Engine name, used in error messages and logs.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


def to_pyiceberg_table(table: Any, caller: str = "register_iceberg()"):
    """Return the pyiceberg ``Table`` behind ``table``.

    Args:
        table: A pyiceberg ``Table``, or an object such as
            :class:`local_data_platform.format.iceberg.Iceberg` whose ``table()``
            method returns one.
        caller: Named in the error message.

    Returns:
        The pyiceberg ``Table``.

    Raises:
        TypeError: If ``table`` is neither.
    """
    from pyiceberg.table import Table as PyIcebergTable

    if isinstance(table, PyIcebergTable):
        return table
    getter = getattr(table, "table", None)
    if callable(getter):
        resolved = getter()
        if isinstance(resolved, PyIcebergTable):
            return resolved
        raise TypeError(f"{type(table).__name__}.table() returned {type(resolved).__name__}, "
                        "expected a pyiceberg Table")
    raise TypeError(f"{caller} expects an Iceberg format object or a pyiceberg Table, "
                    f"got {type(table).__name__}")


__all__ = ["Engine", "to_pyiceberg_table"]
