"""A DuckDB database file as a pipeline source: one table, or the result of one query.

``duckdb`` is an optional dependency (the ``duckdb`` extra). It is imported when the source is
read, never when this module is imported::

    pip install "local-data-platform[duckdb]"

This is the one way to publish a table or a query that lives in DuckDB to Iceberg. The file is
opened read-only and closed before :meth:`DuckDBTable.get` returns, so the Iceberg write that
follows never overlaps with the file being open, and another process can open the file for
writing right after.
"""

from __future__ import annotations

import os
from typing import Any

import pyarrow as pa

from local_data_platform import Table
from local_data_platform.exceptions import ConfigError, EngineNotFound, TableNotFound
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

INSTALL_HINT = 'pip install "local-data-platform[duckdb]"'
MAX_RELATION_PARTS = 3


def _import_duckdb() -> Any:
    """Import ``duckdb`` or raise :class:`EngineNotFound` with an install hint."""
    try:
        import duckdb
    except ImportError as error:
        raise EngineNotFound(f"A DUCKDB source needs the duckdb package. Install it with: {INSTALL_HINT}") from error
    return duckdb


def relation_sql(relation: str) -> str:
    """Return ``SELECT * FROM <relation>`` with every part of the name quoted.

    Args:
        relation: ``table``, ``schema.table`` or ``catalog.schema.table``.

    Raises:
        ConfigError: If a part is empty or there are more than three parts.
    """
    from local_data_platform.engine.duckdb.row_filter import quote_identifier

    parts = relation.split(".")
    if len(parts) > MAX_RELATION_PARTS or not all(part.strip() for part in parts):
        raise ConfigError(f"DuckDB relation {relation!r} must be table, schema.table or catalog.schema.table")
    return "SELECT * FROM " + ".".join(quote_identifier(part.strip()) for part in parts)


class DuckDBTable(Table):
    """A table or a query in a DuckDB database file, read-only.

    Args:
        name: Dataset name. Run events use it for a ``sql`` source.
        path: The DuckDB file. Relative paths resolve against ``base_dir`` (or the cwd).
        relation: The table or view to read: ``table``, ``schema.table`` or
            ``catalog.schema.table``. Give this or ``sql``, not both.
        sql: One ``SELECT`` or ``WITH`` query to run on the file. Give this or ``relation``.
        base_dir: Folder that relative paths resolve against, usually the config file's folder.
        **options: Extra options kept on ``self.options``.

    Attributes:
        path: The absolute path of the DuckDB file.
        relation: The relation, or ``None`` for a ``sql`` source.
        sql: The query, or ``None`` for a ``relation`` source.

    Raises:
        ConfigError: If ``path`` is empty, or not exactly one of ``relation`` and ``sql`` is given.
    """

    def __init__(self, name: str, path: str | os.PathLike, relation: str | None = None, sql: str | None = None,
                 base_dir: str | os.PathLike | None = None, **options):
        if not path:
            raise ConfigError(f"DuckDB dataset {name!r} needs a path")
        given = [key for key, value in (("relation", relation), ("sql", sql)) if value is not None]
        if len(given) != 1:
            raise ConfigError(f"DuckDB dataset {name!r} needs exactly one of 'relation' and 'sql', "
                              f"got {'both' if given else 'neither'}")
        value = relation if relation is not None else sql
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"DuckDB dataset {name!r}: '{given[0]}' must be a non-empty string")
        super().__init__(name, path=path, format="DUCKDB", base_dir=base_dir, **options)
        self.relation = relation.strip() if relation is not None else None
        self.sql = sql.strip() if sql is not None else None
        if self.relation is not None:
            relation_sql(self.relation)  # a malformed name fails here, not after the file is opened

    def get(self) -> pa.Table:
        """Open the file read-only, run the relation scan or the query, and close the file.

        Returns:
            The result as a ``pyarrow.Table``.

        Raises:
            EngineNotFound: If ``duckdb`` is not installed.
            FileNotFoundError: If the file does not exist.
            TableNotFound: If the relation does not exist in the file.
            ConfigError: If the ``sql`` is not exactly one ``SELECT`` or ``WITH`` query, or does not run.
        """
        duckdb = _import_duckdb()
        if not self.path.is_file():
            raise FileNotFoundError(f"DuckDB file not found: {self.path}")
        statement = self._statement()
        connection = duckdb.connect(str(self.path), read_only=True)
        try:
            result = connection.execute(statement)
            # duckdb >= 1.4 names it to_arrow_table() and deprecates fetch_arrow_table().
            fetch = getattr(result, "to_arrow_table", None) or result.fetch_arrow_table
            table = fetch()
        except duckdb.CatalogException as error:
            if self.relation is not None:
                raise TableNotFound(f"DuckDB relation {self.relation} does not exist in {self.path}") from error
            raise ConfigError(f"DuckDB source {self.name!r}: the query failed on {self.path}: "
                              f"{_first_line(error)}") from error
        except (duckdb.ParserException, duckdb.BinderException) as error:
            raise ConfigError(f"DuckDB source {self.name!r}: the query failed on {self.path}: "
                              f"{_first_line(error)}") from error
        finally:
            connection.close()
        logger.info("Read %d rows from DuckDB %s (%s)", table.num_rows, self.path, self.relation or "query")
        return table

    def _statement(self) -> str:
        """The SQL to run: the relation scan, or the query after the one-read-only-statement guard."""
        if self.relation is not None:
            return relation_sql(self.relation)
        # The MCP server's guard is the project's one definition of a single read-only query. It is
        # imported here, not at the top, so the store layer does not depend on the MCP package.
        from local_data_platform.mcp_server.errors import QueryRejected
        from local_data_platform.mcp_server.guard import check_sql

        try:
            check_sql(self.sql)
        except QueryRejected as error:
            raise ConfigError(f"DuckDB source {self.name!r}: {error}") from error
        return self.sql

    def __repr__(self) -> str:
        what = f"relation={self.relation!r}" if self.relation is not None else "sql=..."
        return f"DuckDBTable(name={self.name!r}, path={str(self.path)!r}, {what})"


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


__all__ = ["DuckDBTable", "INSTALL_HINT", "relation_sql"]
