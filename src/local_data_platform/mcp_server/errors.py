"""Errors raised by the MCP server's tools.

Every error derives from :class:`~local_data_platform.exceptions.LDPError`. A tool call that
raises one of them is answered with an MCP error result, and the audit log records the
error's :attr:`status`.
"""

from local_data_platform.exceptions import LDPError


class ToolError(LDPError):
    """Base class for errors a tool reports back to the agent.

    Attributes:
        status: The audit status recorded for the call.
    """

    status = "error"


class InvalidArguments(ToolError):
    """The tool arguments are missing, of the wrong type or out of range."""

    status = "rejected"


class QueryRejected(ToolError):
    """The SQL failed the statement guard: it is not exactly one ``SELECT`` or ``WITH`` query."""

    status = "rejected"


class TableNotAllowed(ToolError):
    """The table does not exist or is not on the server's allowlist.

    The two cases share one error on purpose, so an agent cannot probe for tables it may not see.
    """

    status = "rejected"


class DatasetNotFound(ToolError):
    """No pinned dataset version with that name exists over an allowlisted table."""

    status = "rejected"


class AccessDenied(ToolError):
    """DuckDB's sandbox refused the query, for example a file read outside the allowed tables."""

    status = "denied"


class QueryTimeout(ToolError):
    """The query ran longer than the server's timeout and was interrupted."""

    status = "timeout"


class QueryFailed(ToolError):
    """DuckDB could not run the query, for example because of a syntax or binder error."""

    status = "error"


__all__ = [
    "AccessDenied",
    "DatasetNotFound",
    "InvalidArguments",
    "QueryFailed",
    "QueryRejected",
    "QueryTimeout",
    "TableNotAllowed",
    "ToolError",
]
