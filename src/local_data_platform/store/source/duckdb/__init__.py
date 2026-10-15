"""A DuckDB database file as a pipeline source, or a query over Iceberg tables: one table, or the result of one query.

``duckdb`` is an optional dependency (the ``duckdb`` extra). It is imported when the source is
read, never when this module is imported::

    pip install "local-data-platform[duckdb]"

This is the one way to publish a table or a query that lives in DuckDB to Iceberg. The file is
opened read-only and closed before :meth:`DuckDBTable.get` returns, so the Iceberg write that
follows never overlaps with the file being open, and another process can open the file for
writing right after.

A source that has ``inputs`` has no file. It opens an in-memory DuckDB, registers the Iceberg table that each input
config writes under that table's name, and runs one query on them. The query is ``sql`` or the text of ``query_file``.
A query that reads a table that is not in ``inputs`` fails with DuckDB's own unknown table error, so ``inputs`` is the
whole lineage of the table. The session time zone is UTC. This is how a gold table is defined as one SQL file over
silver tables, and :mod:`local_data_platform.gold` builds them in order.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa

from local_data_platform import Table
from local_data_platform.exceptions import ConfigError, EngineNotFound, TableNotFound
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

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
    """A table or a query in a DuckDB database file, or a query over Iceberg tables, read-only.

    Give exactly one of ``relation``, ``sql`` and ``query_file``. A ``relation`` needs a ``path``. A query reads the
    DuckDB file at ``path``, or, when there are ``inputs``, the Iceberg tables of those configs and no file.

    Args:
        name: Dataset name. Run events use it for a query source.
        path: The DuckDB file. Relative paths resolve against ``base_dir`` (or the cwd). Not given with ``inputs``.
        relation: The table or view to read: ``table``, ``schema.table`` or
            ``catalog.schema.table``.
        sql: One ``SELECT`` or ``WITH`` query to run.
        base_dir: Folder that relative paths resolve against, usually the config file's folder.
        query_file: A file that holds the query, which is read when the source is read. Instead of ``sql``.
        inputs: Dataset config files whose Iceberg target tables the query reads, each registered under its table
            name. They are read when the source is built, so a missing or invalid one fails there, and the tables are
            read when the source is read, so a table that is not there yet fails then.
        **options: Extra options kept on ``self.options``.

    Attributes:
        path: The absolute path of the DuckDB file, or ``None`` for a source with ``inputs``.
        relation: The relation, or ``None`` for a query.
        sql: The query, or ``None`` for a ``relation`` source or a ``query_file``.
        query_file: The absolute path of the file that holds the query, or ``None``.
        inputs: The absolute paths of the input configs.

    Raises:
        ConfigError: If a source has no ``path`` and no ``inputs``, or both, or not exactly one of ``relation``,
            ``sql`` and ``query_file``, or ``inputs`` with a ``relation``, or an input that is not a config of a
            pipeline that writes an Iceberg table, or two inputs that write tables with the same name.
    """

    def __init__(self, name: str, path: str | os.PathLike | None = None, relation: str | None = None,
                 sql: str | None = None, base_dir: str | os.PathLike | None = None, *,
                 query_file: str | os.PathLike | None = None, inputs: Sequence[str | os.PathLike] | None = None,
                 **options):
        given = [key for key, value in (("relation", relation), ("sql", sql), ("query_file", query_file))
                 if value is not None]
        if len(given) != 1:
            raise ConfigError(f"DuckDB dataset {name!r} needs exactly one of 'relation', 'sql' and 'query_file', "
                              f"got {'more than one' if given else 'none'}")
        value = {"relation": relation, "sql": sql, "query_file": query_file}[given[0]]
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            raise ConfigError(f"DuckDB dataset {name!r}: '{given[0]}' must be a non-empty string")
        if inputs is not None:
            if path:
                raise ConfigError(f"DuckDB dataset {name!r} has both a 'path' and 'inputs'. It reads a DuckDB file or "
                                  "the Iceberg tables of its inputs, not both")
            if relation is not None:
                raise ConfigError(f"DuckDB dataset {name!r} has 'inputs' and a 'relation'. A relation is a table of a "
                                  "DuckDB file, so give a 'sql' or a 'query_file' over the inputs")
            if isinstance(inputs, (str, os.PathLike)) or not len(inputs) or not all(
                    isinstance(item, (str, os.PathLike)) and str(item).strip() for item in inputs):
                raise ConfigError(f"DuckDB dataset {name!r}: 'inputs' must be a list of config paths")
        elif not path:
            raise ConfigError(f"DuckDB dataset {name!r} needs a path, or inputs to read instead")
        super().__init__(name, path=path, format="DUCKDB", base_dir=base_dir, **options)
        self.relation = relation.strip() if relation is not None else None
        self.sql = sql.strip() if sql is not None else None
        self.query_file = resolve_path(str(query_file).strip(), base_dir) if query_file is not None else None
        self.inputs: tuple[Path, ...] = tuple(resolve_path(str(item).strip(), base_dir) for item in inputs or ())
        self._input_configs = self._load_inputs()
        if self.relation is not None:
            relation_sql(self.relation)  # a malformed name fails here, not after the file is opened

    # ------------------------------------------------------------------ inputs

    def _load_inputs(self) -> list[Any]:
        """The config of each input, checked: it has to write an Iceberg table, and no two write the same name."""
        from local_data_platform import Config

        configs, seen = [], {}
        for file in self.inputs:
            config = Config.from_json(file)
            target = config.metadata["target"]
            if str(target.get("format", "")).strip().upper() != "ICEBERG":
                raise ConfigError(f"DuckDB dataset {self.name!r}: the input {file} writes to a {target.get('format')} "
                                  "target, and a query reads Iceberg tables")
            table = str(target.get("name"))
            if table in seen:
                raise ConfigError(f"DuckDB dataset {self.name!r}: the inputs {seen[table].name} and {file.name} both "
                                  f"write a table called {table!r}, which the query could not tell apart")
            seen[table] = file
            configs.append(config)
        return configs

    def lineage(self) -> list[dict[str, Any]]:
        """The Iceberg tables that the query reads, named as run events name datasets. Nothing is opened.

        Returns:
            One ``{"namespace", "name", "format", "table_identifier"}`` for each input, in order. Empty for a source
            with a file.
        """
        from local_data_platform.events import iceberg_block_ref

        return [iceberg_block_ref(config.metadata["target"], config.base_dir) for config in self._input_configs]

    # ------------------------------------------------------------------ reading

    def get(self) -> pa.Table:
        """Run the relation scan or the query, and release the file or the engine.

        A source with a file opens it read-only and closes it before it returns. A source with ``inputs`` registers
        the current snapshot of each input table in an in-memory DuckDB with the session time zone set to UTC.

        Returns:
            The result as a ``pyarrow.Table``.

        Raises:
            EngineNotFound: If ``duckdb`` is not installed.
            FileNotFoundError: If the file does not exist.
            TableNotFound: If the relation does not exist in the file, or an input table does not exist yet.
            ConfigError: If the query is not exactly one ``SELECT`` or ``WITH`` query, or does not run, or a query
                file cannot be read. A query that reads a table that is not in ``inputs`` fails with DuckDB's
                own message that the table does not exist.
        """
        duckdb = _import_duckdb()
        if self.inputs:
            return self._get_from_inputs(duckdb)
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

    def _get_from_inputs(self, duckdb: Any) -> pa.Table:
        """Register the input tables in memory, run the query, and close the engine."""
        from local_data_platform.engine.duckdb import DuckDBEngine
        from local_data_platform.pipeline.builders import iceberg_from_config

        statement = self._statement()
        with DuckDBEngine() as engine:
            engine.query("SET TimeZone = 'UTC'")
            for file, config in zip(self.inputs, self._input_configs):
                try:
                    iceberg = iceberg_from_config(config, "target", must_exist=True)
                    iceberg.table()
                except TableNotFound as error:
                    raise TableNotFound(f"DuckDB source {self.name!r} reads a table that {file.name} writes, and "
                                        f"{error}. Run that pipeline first") from error
                engine.register_iceberg(iceberg, str(config.metadata["target"]["name"]))
            try:
                table = engine.query(statement)
            except (duckdb.CatalogException, duckdb.ParserException, duckdb.BinderException) as error:
                names = ", ".join(str(config.metadata["target"]["name"]) for config in self._input_configs)
                raise ConfigError(f"DuckDB source {self.name!r}: the query failed over the inputs ({names}): "
                                  f"{_first_line(error)}") from error
        logger.info("Read %d rows from a query over %d Iceberg tables (%s)", table.num_rows, len(self.inputs),
                    self.name)
        return table

    def _statement(self) -> str:
        """The SQL to run: the relation scan, or the query after the one-read-only-statement guard."""
        if self.relation is not None:
            return relation_sql(self.relation)
        sql = self.sql
        if self.query_file is not None:
            try:
                sql = self.query_file.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ConfigError(f"DuckDB source {self.name!r}: cannot read the query file {self.query_file}: "
                                  f"{error.strerror or error}") from None
            if not sql:
                raise ConfigError(f"DuckDB source {self.name!r}: the query file {self.query_file} is empty")
        # The MCP server's guard is the project's one definition of a single read-only query. It is
        # imported here, not at the top, so the store layer does not depend on the MCP package.
        from local_data_platform.mcp_server.errors import QueryRejected
        from local_data_platform.mcp_server.guard import check_sql

        try:
            check_sql(sql)
        except QueryRejected as error:
            raise ConfigError(f"DuckDB source {self.name!r}: {error}") from error
        return sql

    def __repr__(self) -> str:
        if self.inputs:
            return f"DuckDBTable(name={self.name!r}, inputs={[file.name for file in self.inputs]!r})"
        what = f"relation={self.relation!r}" if self.relation is not None else "sql=..."
        return f"DuckDBTable(name={self.name!r}, path={str(self.path)!r}, {what})"


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


__all__ = ["DuckDBTable", "INSTALL_HINT", "relation_sql"]
