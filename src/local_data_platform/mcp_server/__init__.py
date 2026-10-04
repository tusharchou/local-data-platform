"""A read-only MCP server that lets AI agents query Iceberg tables safely.

The package is named ``mcp_server`` so it never shadows the ``mcp`` SDK. Importing it loads
neither ``mcp`` nor ``duckdb``; both are optional (the ``mcp`` and ``duckdb`` extras) and are
imported when a server or sandbox is built.

Layers, from the outside in:

* :mod:`.cli`: ``ldp mcp`` (:func:`add_cli`) and ``python -m local_data_platform.mcp_server``.
* :mod:`.server`: the MCP low-level server over stdio (:func:`build_server`, :func:`serve_stdio`).
* :mod:`.tools`: the six tools as plain Python, with argument validation and auditing
  (:class:`LakeTools`).
* :mod:`.sandbox`: the locked-down DuckDB connection (:class:`DuckDBSandbox`).
* :mod:`.guard`: the one-``SELECT`` statement guard (:func:`check_sql`).
* :mod:`.discovery`: tables from configs and catalog specs, and the allowlist.
* :mod:`.audit`: the JSONL (and optional ``_ldp.audit``) audit log.

See ``docs/agents.md``.
"""

from .audit import AuditLog, AuditRecord
from .cli import add_cli
from .discovery import Discovery, ExposedTable, discover_tables
from .errors import (
    AccessDenied,
    DatasetNotFound,
    InvalidArguments,
    QueryFailed,
    QueryRejected,
    QueryTimeout,
    TableNotAllowed,
    ToolError,
)
from .guard import check_sql
from .sandbox import DuckDBSandbox
from .server import build_server, serve_stdio
from .tools import LakeTools, ToolOutcome

__all__ = [
    "AccessDenied",
    "AuditLog",
    "AuditRecord",
    "DatasetNotFound",
    "Discovery",
    "DuckDBSandbox",
    "ExposedTable",
    "InvalidArguments",
    "LakeTools",
    "QueryFailed",
    "QueryRejected",
    "QueryTimeout",
    "TableNotAllowed",
    "ToolError",
    "ToolOutcome",
    "add_cli",
    "build_server",
    "check_sql",
    "discover_tables",
    "serve_stdio",
]
