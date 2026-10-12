"""The statement guard: only one read-only ``SELECT`` or ``WITH`` query gets through.

The guard runs two checks before DuckDB executes anything:

1. **Lexical.** DuckDB's tokenizer (comments are dropped, strings stay whole) must show a
   ``SELECT`` or ``WITH`` keyword as the first token, after any opening parentheses. This
   rejects ``PRAGMA``, ``DESCRIBE``, ``SHOW``, ``SUMMARIZE``, ``FROM``-first and ``VALUES``
   queries, which DuckDB's parser rewrites into ``SELECT`` statements, and it stops
   ``IMPORT DATABASE`` before the parser reads files at parse time.
2. **Parsed.** DuckDB's ``extract_statements`` must return exactly one statement, of type
   ``SELECT``. This rejects multi-statement text, however it is hidden in comments or strings,
   and CTE-wrapped writes such as ``WITH x AS (...) INSERT ...``, which parse as ``INSERT``.

Pass the sandboxed connection to :func:`check_sql`, so parsing itself runs with file access
disabled.
"""

from __future__ import annotations

import re
from typing import Any

from local_data_platform.exceptions import EngineNotFound

from .errors import QueryRejected

MAX_SQL_CHARS = 100_000
"""Longest SQL text the guard accepts."""

ALLOWED_FIRST_KEYWORDS = ("select", "with")

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
        statements = connection.extract_statements(sql)
    except duckdb.Error as error:
        raise QueryRejected(f"sql does not parse: {_first_line(error)}") from None
    finally:
        if owned:
            connection.close()
    if len(statements) != 1:
        raise QueryRejected(f"exactly one statement is allowed; found {len(statements)}")
    statement = statements[0]
    if statement.type != duckdb.StatementType.SELECT:
        raise QueryRejected(f"only read-only SELECT or WITH queries are allowed; this is a "
                            f"{statement.type.name} statement")
    return statement


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


__all__ = ["ALLOWED_FIRST_KEYWORDS", "MAX_SQL_CHARS", "check_sql", "first_keyword", "import_duckdb"]
