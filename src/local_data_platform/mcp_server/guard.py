"""The statement guard: only one read-only ``SELECT`` or ``WITH`` query gets through.

The guard runs three checks before DuckDB executes anything:

1. **Lexical.** DuckDB's tokenizer (comments are dropped, strings stay whole) must show a
   ``SELECT`` or ``WITH`` keyword as the first token, after any opening parentheses. This
   rejects ``PRAGMA``, ``DESCRIBE``, ``SHOW``, ``SUMMARIZE``, ``FROM``-first and ``VALUES``
   queries, which DuckDB's parser rewrites into ``SELECT`` statements, and it stops
   ``IMPORT DATABASE`` before the parser reads files at parse time.
2. **Parsed.** DuckDB's ``extract_statements`` must return exactly one statement, of type
   ``SELECT``. This rejects multi-statement text, however it is hidden in comments or strings,
   and CTE-wrapped writes such as ``WITH x AS (...) INSERT ...``, which parse as ``INSERT``.
3. **Walked.** DuckDB 1.x refuses a write inside a CTE, such as ``WITH x AS (DELETE ...) SELECT ...``
   or ``WITH x AS (COPY ... TO 'f') SELECT ...``, when it parses. DuckDB 2.0 parses it as a ``SELECT``
   and runs it, and ``allowed_directories`` lets a ``COPY`` write inside the folder of a table. So when
   the text has the keyword of a write, the guard has DuckDB serialize the query with
   ``json_serialize_sql`` and rejects it unless every query node in it is a ``SELECT``, a set
   operation or a CTE. It also rejects a text that DuckDB cannot serialize.

Pass the sandboxed connection to :func:`check_sql`, so parsing itself runs with file access
disabled.
"""

from __future__ import annotations

import json
import re
from typing import Any

from local_data_platform.exceptions import EngineNotFound

from .errors import QueryRejected

MAX_SQL_CHARS = 100_000
"""Longest SQL text the guard accepts."""

ALLOWED_FIRST_KEYWORDS = ("select", "with")

_WRITE_KEYWORDS = frozenset({"insert", "update", "delete", "merge", "copy"})
"""The keywords that start a statement which writes, and that DuckDB 2.0 lets a CTE hold."""

_QUERY_NODES = frozenset({"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE", "CTE_NODE"})
"""The nodes of a serialized query that only read. Any other ``*_NODE``, such as ``DELETE_QUERY_NODE``, writes."""

INSTALL_HINT = 'pip install "local-data-platform[duckdb]"'

_WORD = re.compile(r"[A-Za-z_]+")


def import_duckdb() -> Any:
    """Import ``duckdb`` or raise :class:`EngineNotFound` with an install hint."""
    try:
        import duckdb
    except ImportError as error:
        raise EngineNotFound(f"The MCP server's SQL sandbox needs the duckdb package. Install it with: "
                             f"{INSTALL_HINT}") from error
    return duckdb


def first_keyword(sql: str) -> str:
    """Return the first significant token of ``sql``, lower-cased, skipping comments and ``(``.

    Args:
        sql: The SQL text.

    Returns:
        The first keyword or word (for example ``"select"`` or ``"copy"``), the first
        operator character if the text starts with one other than ``(``, or ``""`` when
        the text holds no tokens.
    """
    duckdb = import_duckdb()
    tokens = duckdb.tokenize(sql)
    comment = getattr(getattr(duckdb, "token_type", None), "comment", None)
    offsets = [offset for offset, kind in tokens if kind != comment]
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(sql)
        text = sql[offset:end].strip()
        if text.startswith("("):
            continue
        match = _WORD.match(text)
        return match.group(0).lower() if match else text[:1]
    return ""


def check_sql(sql: Any, connection: Any = None) -> Any:
    """Check that ``sql`` is exactly one read-only query and return its parsed statement.

    Args:
        sql: The SQL text from the agent.
        connection: The DuckDB connection to parse with. Pass the sandboxed connection so
            parsing runs with file access disabled. When omitted, a throwaway in-memory
            connection with external access disabled is used.

    Returns:
        The single ``duckdb.Statement``, ready for ``connection.execute(statement)``.

    Raises:
        QueryRejected: If the text is empty, too long, not a ``SELECT`` or ``WITH`` query,
            holds more than one statement, or does not parse.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    if not isinstance(sql, str):
        raise QueryRejected(f"sql must be a string, got {type(sql).__name__}")
    if len(sql) > MAX_SQL_CHARS:
        raise QueryRejected(f"sql is {len(sql)} characters; the limit is {MAX_SQL_CHARS}")
    duckdb = import_duckdb()
    keyword = first_keyword(sql)
    if not keyword:
        raise QueryRejected("sql is empty")
    if keyword not in ALLOWED_FIRST_KEYWORDS:
        raise QueryRejected(f"only read-only SELECT or WITH queries are allowed; this statement starts with "
                            f"{keyword.upper()!r}")
    owned = connection is None
    if owned:
        connection = duckdb.connect(":memory:", config={"enable_external_access": False})
    try:
        try:
            statements = connection.extract_statements(sql)
        except duckdb.Error as error:
            raise QueryRejected(f"sql does not parse: {_first_line(error)}") from None
        if len(statements) != 1:
            raise QueryRejected(f"exactly one statement is allowed; found {len(statements)}")
        statement = statements[0]
        if statement.type != duckdb.StatementType.SELECT:
            raise QueryRejected(f"only read-only SELECT or WITH queries are allowed; this is a "
                                f"{statement.type.name} statement")
        _reject_writes_inside(connection, sql, duckdb)
    finally:
        if owned:
            connection.close()
    return statement


def _keywords(sql: str, duckdb: Any) -> set[str]:
    """The lower-cased keyword tokens of ``sql``. A quoted name and a string are not keywords."""
    tokens = duckdb.tokenize(sql)
    keyword = getattr(getattr(duckdb, "token_type", None), "keyword", None)
    ends = [offset for offset, _ in tokens[1:]] + [len(sql)]
    return {sql[offset:end].strip().lower() for (offset, kind), end in zip(tokens, ends)
            if keyword is None or kind == keyword}


def _holds_write(node: Any) -> bool:
    if isinstance(node, dict):
        kind = node.get("type")
        if isinstance(kind, str) and kind.endswith("_NODE") and kind not in _QUERY_NODES:
            return True
        return any(_holds_write(child) for child in node.values())
    if isinstance(node, list):
        return any(_holds_write(child) for child in node)
    return False


def _reject_writes_inside(connection: Any, sql: str, duckdb: Any) -> None:
    """Reject a ``SELECT`` that holds a query which writes, as DuckDB 2.0 allows in a CTE.

    Only a text with the keyword of a write is walked, so every other query goes through as before.
    """
    if not _WRITE_KEYWORDS & _keywords(sql, duckdb):
        return
    try:
        tree = json.loads(connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchall()[0][0])
    except (duckdb.Error, ValueError, IndexError):
        tree = {"error": True}
    if tree.get("error"):
        raise QueryRejected("only read-only SELECT or WITH queries are allowed, and DuckDB cannot show that this "
                            "one only reads, because it has the keyword of a write")
    if _holds_write(tree):
        raise QueryRejected("only read-only SELECT or WITH queries are allowed; a CTE or a subquery in this one "
                            "writes with INSERT, UPDATE, DELETE or COPY")


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


__all__ = ["ALLOWED_FIRST_KEYWORDS", "MAX_SQL_CHARS", "check_sql", "first_keyword", "import_duckdb"]
