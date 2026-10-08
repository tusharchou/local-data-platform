"""The six read-only tools the MCP server exposes, as plain Python.

:class:`LakeTools` holds the discovered tables, the DuckDB sandbox and the audit log.
Each tool is a method returning a JSON-ready dict; :meth:`LakeTools.call` validates the
arguments, runs the tool, turns errors into a :class:`ToolOutcome` and audits the call.
The MCP layer (:mod:`local_data_platform.mcp_server.server`) only translates outcomes into
MCP results, so everything here can be used and tested without the ``mcp`` SDK.

* ``list_tables()``: every allowlisted table, how to name it in SQL, its row count and last commit.
* ``describe_table(table)``: schema, partition spec, row count, snapshots, freshness, and the
  latest ``_ldp`` run and quality status.
* ``query(sql, max_rows)``: rows of one read-only ``SELECT``/``WITH`` query, capped and timed out.
* ``sample_rows(table, n)``: the first ``n`` rows in scan order.
* ``table_history(table)``: snapshots newest first, with operations, record counts and ``ldp.*``
  properties.
* ``get_dataset(name)``: the latest pinned version of a dataset (needs
  :mod:`local_data_platform.datasets`).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import decimal
import importlib
import inspect
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import LDPError
from local_data_platform.logger import get_logger

from .audit import AuditLog, AuditRecord, iceberg_audit_sink
from .discovery import SYSTEM_NAMESPACE, Discovery, ExposedTable, discover_tables, redact
from .errors import DatasetNotFound, InvalidArguments, TableNotAllowed, ToolError
from .sandbox import DuckDBSandbox, QueryResult

logger = get_logger(__name__)

DEFAULT_MAX_ROWS = 500
"""Server-wide row cap: no tool returns more rows than this."""
DEFAULT_QUERY_ROWS = 100
"""Rows ``query`` returns when the agent does not pass ``max_rows``."""
DEFAULT_SAMPLE_ROWS = 10
DEFAULT_HISTORY = 50
MAX_HISTORY = 1000
DEFAULT_TIMEOUT_S = 30.0
MAX_CELL_CHARS = 4096
"""Longer strings (and base64 of longer binaries) in results are cut to this many characters."""
RECENT_SNAPSHOTS = 10
TERMINAL_RUN_EVENTS = ("run.published", "run.skipped_duplicate", "run.blocked_quality", "run.failed")
_TS_COLUMNS = ("ts", "event_ts", "timestamp", "emitted_at", "evaluated_at", "created_at")
_TABLE_KEY_COLUMNS = ("table_uuid", "table_identifier")
_DATASET_DIR_PARAMS = ("warehouse", "warehouse_path", "root", "base_dir", "path")
_TOOL_NAMES = ("list_tables", "describe_table", "query", "sample_rows", "table_history", "get_dataset")


# ---------------------------------------------------------------------- JSON


def jsonable(value: Any) -> Any:
    """Convert a value from a pyarrow row or pyiceberg object into JSON-safe data.

    Timestamps, dates and times become ISO strings, decimals and UUIDs strings, bytes
    base64, NaN and infinities the strings ``"NaN"``, ``"Infinity"`` and ``"-Infinity"``,
    and strings longer than :data:`MAX_CELL_CHARS` are cut with a marker.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, str):
        if len(value) > MAX_CELL_CHARS:
            return value[:MAX_CELL_CHARS] + f"...[{len(value) - MAX_CELL_CHARS} more characters]"
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (decimal.Decimal, uuid.UUID, Path)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return jsonable(base64.b64encode(bytes(value)).decode("ascii"))
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    return str(value)


def result_payload(result: QueryResult, max_rows: int) -> dict[str, Any]:
    """Turn a :class:`QueryResult` into ``{"columns", "rows", "row_count", "truncated", ...}``.

    Rows are lists in column order, so duplicate column names survive.
    """
    table = result.table
    columns = [{"name": item.name, "type": str(item.type)} for item in table.schema]
    values = [column.to_pylist() for column in table.columns]
    rows = [jsonable(list(row)) for row in zip(*values)] if values else []
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": result.truncated,
            "max_rows": max_rows, "duration_ms": result.duration_ms}


def _iso_ms(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc).isoformat(timespec="milliseconds")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _summary(snapshot: Any) -> dict[str, str]:
    summary = getattr(snapshot, "summary", None)
    return dict(summary.additional_properties) if summary is not None else {}


def _operation(snapshot: Any) -> str | None:
    summary = getattr(snapshot, "summary", None)
    operation = getattr(summary, "operation", None)
    return getattr(operation, "value", operation)


def _total_records(iceberg_table: Any) -> int | None:
    snapshot = iceberg_table.current_snapshot()
    if snapshot is None:
        return 0
    return _int_or_none(_summary(snapshot).get("total-records"))


def _snapshot_entry(snapshot: Any, *, current_id: int | None = None, lineage: set[int] | None = None) -> dict[str, Any]:
    summary = _summary(snapshot)
    entry = {
        "snapshot_id": snapshot.snapshot_id,
        "parent_id": snapshot.parent_snapshot_id,
        "committed_at": _iso_ms(snapshot.timestamp_ms),
        "operation": _operation(snapshot),
        "added_records": _int_or_none(summary.get("added-records")),
        "deleted_records": _int_or_none(summary.get("deleted-records")),
        "total_records": _int_or_none(summary.get("total-records")),
        "ldp": {key: value for key, value in summary.items() if key.startswith("ldp.")},
    }
    if lineage is not None:
        entry["is_current"] = snapshot.snapshot_id == current_id
        entry["on_main"] = snapshot.snapshot_id in lineage
    return entry


def _main_lineage(iceberg_table: Any) -> set[int]:
    lineage: set[int] = set()
    snapshot = iceberg_table.current_snapshot()
    while snapshot is not None and snapshot.snapshot_id not in lineage:
        lineage.add(snapshot.snapshot_id)
        parent = snapshot.parent_snapshot_id
        snapshot = iceberg_table.snapshot_by_id(parent) if parent is not None else None
    return lineage


# ---------------------------------------------------------------------- _ldp


def _load_ldp_table(catalog: Any, name: str) -> Any:
    from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

    try:
        return catalog.load_table((SYSTEM_NAMESPACE, name))
    except (NoSuchTableError, NoSuchNamespaceError):
        return None


def _parse_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return row
    if isinstance(payload, Mapping):
        row = dict(row)
        row["payload"] = dict(payload)
    return row


def _field(row: Mapping[str, Any] | None, key: str) -> Any:
    """``row[key]``, or the same key inside the row's ``payload`` object."""
    if row is None:
        return None
    if row.get(key) is not None:
        return row[key]
    payload = row.get("payload")
    return payload.get(key) if isinstance(payload, Mapping) else None


def _ldp_rows(catalog: Any, name: str, table_uuid: str, identifier: str) -> list[dict[str, Any]] | None:
    """The ``_ldp.<name>`` rows about one table, oldest first; ``None`` if the table is absent.

    A row is about the table when its ``table_uuid`` matches, or its ``table_identifier``
    does (the first run of a new table emits ``run.started`` before the table has a UUID).
    """
    ldp_table = _load_ldp_table(catalog, name)
    if ldp_table is None:
        return None
    columns = [item.name for item in ldp_table.schema().fields]
    keys = {column: (table_uuid if column == "table_uuid" else identifier)
            for column in _TABLE_KEY_COLUMNS if column in columns}
    if not keys:
        logger.debug("_ldp.%s has none of the columns %s; cannot attribute rows", name, _TABLE_KEY_COLUMNS)
        return []
    try:
        from pyiceberg.expressions import EqualTo, Or

        expressions = [EqualTo(column, value) for column, value in keys.items()]
        row_filter = expressions[0] if len(expressions) == 1 else Or(*expressions)
        rows = ldp_table.scan(row_filter=row_filter).to_arrow().to_pylist()
    except Exception:  # noqa: BLE001 - e.g. a non-string column type; filter in Python instead
        rows = [row for row in ldp_table.scan().to_arrow().to_pylist()
                if any(str(row.get(column)) == value for column, value in keys.items())]
    ts_column = next((column for column in _TS_COLUMNS if column in columns), None)
    for row in rows:
        row["_ts"] = row.get(ts_column) if ts_column else None
    rows.sort(key=lambda row: (str(row["_ts"] or ""), str(row.get("run_id") or ""),
                               _int_or_none(row.get("attempt")) or 0, _int_or_none(row.get("seq")) or 0,
                               _int_or_none(row.get("check_index")) or 0))
    return [_parse_payload(row) for row in rows]


def _latest_run(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Summarize the most recent run in ``_ldp.runs`` rows: status, times and event types."""
    if not rows:
        return None
    last = rows[-1]
    run_id, attempt = last.get("run_id"), last.get("attempt")
    run = [row for row in rows if row.get("run_id") == run_id and row.get("attempt") == attempt]
    types = [row.get("type") for row in run]
    finished = next((row for row in reversed(run) if row.get("type") == "run.finished"), None)
    terminal = next((kind for kind in reversed(types) if kind in TERMINAL_RUN_EVENTS), None)
    status = _field(finished, "status") if finished is not None else None
    if status is None:
        status = terminal[len("run."):] if terminal else ("finished" if finished is not None else "running")
    return jsonable({
        "run_id": run_id,
        "attempt": attempt,
        "pipeline": _field(last, "pipeline"),
        "status": status,
        "started_at": run[0].get("_ts"),
        "finished_at": finished.get("_ts") if finished is not None else None,
        "snapshot_id": next((_field(row, "snapshot_id") for row in reversed(run)
                             if _field(row, "snapshot_id") is not None), None),
        "idempotency_key": next((_field(row, "idempotency_key") for row in reversed(run)
                                 if _field(row, "idempotency_key") is not None), None),
        "events": types,
    })


def _latest_quality(quality_rows: list[dict[str, Any]] | None, run_rows: list[dict[str, Any]] | None,
                    iceberg_table: Any) -> dict[str, Any] | None:
    """The latest quality verdict for a table.

    The newest ``quality.evaluated`` event in ``_ldp.runs`` decides which evaluation is
    latest; its per-check rows come from ``_ldp.quality_results`` when they exist. Without
    ``_ldp`` rows, the current snapshot's ``ldp.quality`` property is used.
    """
    evaluated = [row for row in (run_rows or []) if row.get("type") == "quality.evaluated"]
    quality_rows = quality_rows or []
    if evaluated:
        event = evaluated[-1]
        checks = [row for row in quality_rows if row.get("event_id") == event.get("event_id")]
    elif quality_rows:
        event = quality_rows[-1]
        checks = [row for row in quality_rows if row.get("event_id") == event.get("event_id")]
    else:
        snapshot = iceberg_table.current_snapshot()
        if snapshot is not None and "ldp.quality" in _summary(snapshot):
            return {"source": "snapshot", "snapshot_id": snapshot.snapshot_id,
                    "status": _summary(snapshot)["ldp.quality"]}
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    details = [{"check": _field(row, "check_name"), "column": _field(row, "column_name"),
                "passed": _field(row, "passed"), "failing_rows": _field(row, "failing_rows"),
                "details": _field(row, "details")} for row in checks]
    verdicts = [item["passed"] for item in details if item["passed"] is not None]
    passed = payload.get("passed")
    if passed is None and verdicts:
        passed = all(bool(verdict) for verdict in verdicts)
    return jsonable({
        "source": f"{SYSTEM_NAMESPACE}.quality_results" if checks else f"{SYSTEM_NAMESPACE}.runs",
        "run_id": event.get("run_id"),
        "evaluated_at": event.get("_ts"),
        "passed": passed,
        "on_failure": payload.get("on_failure", _field(event, "on_failure")),
        "checks_run": payload.get("checks_run", len(details)),
        "checks_failed": payload.get("checks_failed", sum(1 for verdict in verdicts if not verdict)),
        "checks": details,
    })


# ---------------------------------------------------------------------- tools


@dataclass(frozen=True)
class ToolSpec:
    """A tool's name, human title, description and JSON Schema for its arguments."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolOutcome:
    """The result of :meth:`LakeTools.call`.

    Attributes:
        ok: Whether the tool succeeded.
        data: The tool's JSON-ready result, or ``{"error": {"type", "message"}}``.
        status: The audit status: ``ok``, ``rejected``, ``denied``, ``timeout`` or ``error``.
        error: The error message, when the tool failed.
    """

    ok: bool
    data: dict[str, Any]
    status: str = "ok"
    error: str | None = None


def default_audit_path(discovery: Discovery) -> Path:
    """``<first local warehouse>/.ldp/audit/mcp_audit.jsonl``, or the same under the cwd."""
    warehouses = discovery.warehouses()
    root = warehouses[0] if warehouses else Path.cwd()
    return root / ".ldp" / "audit" / "mcp_audit.jsonl"


class LakeTools:
    """The read-only lakehouse tools, over the tables of a :class:`Discovery`.

    Args:
        discovery: The tables to serve (already filtered by the allowlist).
        max_rows: The server-wide row cap. A tool's own ``max_rows`` or ``n`` is clamped to it.
        timeout_s: Queries are interrupted after this many seconds.
        audit: Where calls are audited. Defaults to an in-memory :class:`AuditLog`.
        native: Passed to :class:`DuckDBSandbox`.
        install_extensions: Passed to :class:`DuckDBSandbox`.
        memory_limit: Passed to :class:`DuckDBSandbox`.
        threads: Passed to :class:`DuckDBSandbox`.

    Raises:
        ValueError: If ``max_rows`` or ``timeout_s`` is not positive.
    """

    def __init__(self, discovery: Discovery, *, max_rows: int = DEFAULT_MAX_ROWS, timeout_s: float = DEFAULT_TIMEOUT_S,
                 audit: AuditLog | None = None, native: bool | None = None, install_extensions: bool = False,
                 memory_limit: str | None = None, threads: int | None = None):
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError(f"max_rows must be a whole number of at least 1, got {max_rows!r}")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise ValueError(f"timeout_s must be a positive number of seconds, got {timeout_s!r}")
        self.discovery = discovery
        self.max_rows = max_rows
        self.timeout_s = float(timeout_s)
        self.audit = audit if audit is not None else AuditLog(None)
        self.sandbox = DuckDBSandbox(discovery.tables, native=native, install_extensions=install_extensions,
                                     memory_limit=memory_limit, threads=threads)
        registered = set(self.sandbox.identifiers)
        self._tables = {table.identifier: table for table in discovery.tables if table.identifier in registered}
        self.unavailable = list(discovery.unavailable) + [
            {"table": table.identifier, "source": table.source, "reason": "could not be registered; see the server log"}
            for table in discovery.tables if table.identifier not in registered
        ]
        self._specs = {spec.name: spec for spec in self.tool_specs()}
        self._call_lock = threading.Lock()

    @classmethod
    def from_sources(cls, configs: Iterable[str | os.PathLike] = (), catalogs: Iterable[str | os.PathLike] = (), *,
                     allow: str | Iterable[str] | None = None, audit_path: str | os.PathLike | None = None,
                     iceberg_audit: bool = True, **kwargs: Any) -> LakeTools:
        """Discover tables, open the audit log and build the tools.

        Args:
            configs: Config files or folders of them.
            catalogs: Catalog spec files.
            allow: The table allowlist.
            audit_path: The JSONL audit file; defaults to :func:`default_audit_path`.
            iceberg_audit: Also audit to ``_ldp.audit`` in the first catalog when
                :func:`~local_data_platform.mcp_server.audit.iceberg_audit_sink` finds a sink.
            **kwargs: Passed to :class:`LakeTools`.

        Returns:
            The tools. Close them with :meth:`close`.
        """
        discovery = discover_tables(configs, catalogs, allow=allow)
        sink = None
        if iceberg_audit and discovery.catalogs:
            first = discovery.catalogs[0]
            sink = iceberg_audit_sink(first.spec, first.base_dir)
        audit = AuditLog(audit_path if audit_path is not None else default_audit_path(discovery), sink=sink)
        try:
            return cls(discovery, audit=audit, **kwargs)
        except BaseException:
            audit.close()
            discovery.close()
            raise

    # ------------------------------------------------------------------ specs

    def tool_specs(self) -> list[ToolSpec]:
        """The six tools with argument schemas that reflect this server's limits."""
        table_arg = {"type": "string", "description": "A table identifier from list_tables, e.g. 'demo.rides'."}
        return [
            ToolSpec("list_tables", "List tables",
                     "List every table this server exposes, with the name to use in SQL, its row count and when it "
                     "last changed. Call this first.",
                     {"type": "object", "properties": {}, "additionalProperties": False}),
            ToolSpec("describe_table", "Describe a table",
                     "Show a table's schema, partition spec, row count, recent snapshots, freshness (time since the "
                     "last commit) and the latest pipeline run and data-quality status recorded in _ldp.",
                     {"type": "object", "properties": {"table": table_arg}, "required": ["table"],
                      "additionalProperties": False}),
            ToolSpec("query", "Run a read-only SQL query",
                     f"Run ONE read-only DuckDB SQL query that starts with SELECT or WITH. Reference tables by the "
                     f"sql_name from list_tables (e.g. demo.rides). Other statements, file functions outside the "
                     f"served tables, and multiple statements are refused. At most {self.max_rows} rows are returned "
                     f"and the query is stopped after {self.timeout_s:g} seconds.",
                     {"type": "object", "properties": {
                         "sql": {"type": "string", "description": "One SELECT or WITH query in DuckDB SQL."},
                         "max_rows": {"type": "integer", "minimum": 1, "maximum": self.max_rows,
                                      "description": f"Rows to return (default "
                                                     f"{min(DEFAULT_QUERY_ROWS, self.max_rows)}, at most "
                                                     f"{self.max_rows})."}},
                      "required": ["sql"], "additionalProperties": False}),
            ToolSpec("sample_rows", "Sample rows",
                     "Return the first n rows of a table in scan order, to see what the data looks like.",
                     {"type": "object", "properties": {
                         "table": table_arg,
                         "n": {"type": "integer", "minimum": 1, "maximum": self.max_rows,
                               "description": f"Rows to return (default {min(DEFAULT_SAMPLE_ROWS, self.max_rows)})."}},
                      "required": ["table"], "additionalProperties": False}),
            ToolSpec("table_history", "Show table history",
                     "List a table's snapshots newest first: when each was committed, the operation, records added, "
                     "deleted and in total, whether it is on the main branch, and LDP run properties.",
                     {"type": "object", "properties": {
                         "table": table_arg,
                         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HISTORY,
                                   "description": f"Snapshots to return (default {DEFAULT_HISTORY})."}},
                      "required": ["table"], "additionalProperties": False}),
            ToolSpec("get_dataset", "Get a pinned dataset",
                     "Return the latest pinned version of a named dataset: the table, snapshot id, row filter, "
                     "selected fields, row count and schema fingerprint that reproduce it exactly.",
                     {"type": "object", "properties": {"name": {"type": "string", "description": "The dataset name."}},
                      "required": ["name"], "additionalProperties": False}),
        ]

    def instructions(self) -> str:
        """Server instructions shown to the agent at connect time."""
        return (f"Read-only access to {len(self._tables)} Apache Iceberg tables through DuckDB. Start with "
                f"list_tables, then describe_table or sample_rows. query runs one SELECT or WITH statement (DuckDB "
                f"dialect), returns at most {self.max_rows} rows and stops after {self.timeout_s:g} seconds. For "
                f"time travel on a table whose access is 'native', query iceberg_scan('<metadata_location from "
                f"describe_table>', snapshot_from_id => <snapshot_id from table_history>). Every call is audited.")

    # ------------------------------------------------------------------ dispatch

    def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> ToolOutcome:
        """Validate the arguments, run a tool, audit the call and return its outcome. Never raises.

        Calls are thread-safe and run one at a time, so the MCP layer can run each in a worker thread.
        """
        started = time.perf_counter()
        args = dict(arguments) if isinstance(arguments, Mapping) else {}
        try:
            if arguments is not None and not isinstance(arguments, Mapping):
                raise InvalidArguments("arguments must be an object")
            spec = self._specs.get(tool)
            if spec is None:
                raise InvalidArguments(f"unknown tool {tool!r}; the tools are {', '.join(_TOOL_NAMES)}")
            kwargs = self._validate(spec, args)
            with self._call_lock:
                data = getattr(self, tool)(**kwargs)
            outcome = ToolOutcome(ok=True, data=data)
        except ToolError as error:
            outcome = ToolOutcome(False, {"error": {"type": type(error).__name__, "message": str(error)}},
                                  error.status, str(error))
        except LDPError as error:
            outcome = ToolOutcome(False, {"error": {"type": type(error).__name__, "message": str(error)}},
                                  "error", str(error))
        except Exception as error:  # noqa: BLE001 - the server must answer every call
            logger.exception("Tool %r failed", tool)
            message = f"internal error: {type(error).__name__}: {error}"
            outcome = ToolOutcome(False, {"error": {"type": "InternalError", "message": message}}, "error", message)
        self._audit(tool, args, outcome, started)
        return outcome

    def _audit(self, tool: str, args: Mapping[str, Any], outcome: ToolOutcome, started: float) -> None:
        sql = args.get("sql") if isinstance(args.get("sql"), str) else None
        table = args.get("table") if isinstance(args.get("table"), str) else None
        scalars = {key: (value[:1000] if isinstance(value, str) else value) for key, value in args.items()
                   if key not in ("sql", "table") and (value is None or isinstance(value, (str, int, float, bool)))}
        rows = outcome.data.get("row_count") if outcome.ok else None
        truncated = outcome.data.get("truncated") if outcome.ok and "row_count" in outcome.data else None
        record = AuditRecord(tool=str(tool)[:200], status=outcome.status,
                             duration_ms=round((time.perf_counter() - started) * 1000, 3), sql=sql, table=table,
                             arguments=scalars, rows=rows, truncated=truncated, error=outcome.error)
        try:
            self.audit.record(record)
        except Exception as error:  # noqa: BLE001 - never fail a call because of the audit
            logger.error("Could not write the audit record: %s", error)

    @staticmethod
    def _validate(spec: ToolSpec, args: Mapping[str, Any]) -> dict[str, Any]:
        properties = spec.input_schema.get("properties", {})
        unknown = sorted(set(args) - set(properties))
        if unknown:
            raise InvalidArguments(f"{spec.name} does not take {unknown}; it takes {sorted(properties) or 'nothing'}")
        for name in spec.input_schema.get("required", []):
            if args.get(name) is None:
                raise InvalidArguments(f"{spec.name} needs '{name}'")
        kwargs: dict[str, Any] = {}
        for name, value in args.items():
            if value is None:
                continue
            kind = properties[name].get("type")
            if kind == "string":
                if not isinstance(value, str) or not value.strip():
                    raise InvalidArguments(f"'{name}' must be a non-empty string")
            elif kind == "integer":
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise InvalidArguments(f"'{name}' must be a whole number, got {value!r}")
                if value < properties[name].get("minimum", 1):
                    raise InvalidArguments(f"'{name}' must be at least {properties[name].get('minimum', 1)}")
            kwargs[name] = value
        return kwargs

    def _resolve(self, table: str) -> ExposedTable:
        if table in self._tables:
            return self._tables[table]
        matches = [item for item in self._tables.values() if item.name == table]
        if len(matches) == 1:
            return matches[0]
        raise TableNotAllowed(f"table {table!r} is not available; call list_tables for the tables you can use")

    @property
    def tables(self) -> list[str]:
        """The identifiers of the served tables."""
        return list(self._tables)

    def close(self) -> None:
        """Close the sandbox, the audit log and the catalogs. Safe to call more than once."""
        self.sandbox.close()
        self.audit.close()
        self.discovery.close()

    def __enter__(self) -> LakeTools:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"LakeTools(tables={self.tables!r}, max_rows={self.max_rows}, timeout_s={self.timeout_s:g})"

    # ------------------------------------------------------------------ tools

    def list_tables(self) -> dict[str, Any]:
        """Every served table, and the allowed ones that could not be served."""
        tables = []
        for identifier, table in self._tables.items():
            info: dict[str, Any] = {
                "table": identifier,
                "sql_name": self.sandbox.sql_name(identifier),
                "aliases": self.sandbox.aliases(identifier),
                "namespace": ".".join(table.namespace),
                "name": table.name,
                "access": self.sandbox.access(identifier),
                "source": table.source,
            }
            try:
                iceberg_table = table.load()
                snapshot = iceberg_table.current_snapshot()
                info["row_count"] = _total_records(iceberg_table)
                info["last_updated"] = _iso_ms(snapshot.timestamp_ms) if snapshot is not None else None
            except Exception as error:  # noqa: BLE001 - list what we can
                info["error"] = str(error)
            tables.append(info)
        return {"tables": tables, "unavailable": jsonable(self.unavailable), "sql_dialect": "duckdb",
                "limits": {"max_rows": self.max_rows, "timeout_s": self.timeout_s}}

    def describe_table(self, table: str) -> dict[str, Any]:
        """Schema, partitioning, counts, snapshots, freshness and ``_ldp`` status of one table."""
        exposed = self._resolve(table)
        iceberg_table = exposed.load()
        schema = iceberg_table.schema()
        spec = iceberg_table.spec()
        snapshot = iceberg_table.current_snapshot()
        snapshots = sorted(iceberg_table.snapshots(), key=lambda item: item.timestamp_ms, reverse=True)
        now_ms = time.time() * 1000
        freshness: dict[str, Any] = {
            "last_commit_at": _iso_ms(snapshot.timestamp_ms) if snapshot is not None else None,
            "age_seconds": round((now_ms - snapshot.timestamp_ms) / 1000, 3) if snapshot is not None else None,
            "current_snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
        }
        catalog = exposed.catalog.catalog
        table_uuid = str(iceberg_table.metadata.table_uuid)
        ldp: dict[str, Any] = {}
        try:
            runs = _ldp_rows(catalog, "runs", table_uuid, exposed.identifier)
            quality = _ldp_rows(catalog, "quality_results", table_uuid, exposed.identifier)
            ldp = {"runs_table": runs is not None, "quality_table": quality is not None,
                   "latest_run": _latest_run(runs or []), "quality": _latest_quality(quality, runs, iceberg_table)}
        except Exception as error:  # noqa: BLE001 - describe the table even if _ldp is unreadable
            logger.warning("Could not read _ldp status for %s: %s", exposed.identifier, error)
            ldp = {"error": str(error), "latest_run": None,
                   "quality": _latest_quality(None, None, iceberg_table)}
        latest_run = ldp.get("latest_run")
        if latest_run:
            freshness["last_run_at"] = latest_run.get("finished_at") or latest_run.get("started_at")
            freshness["last_run_status"] = latest_run.get("status")
        return jsonable({
            "table": exposed.identifier,
            "sql_name": self.sandbox.sql_name(exposed.identifier),
            "access": self.sandbox.access(exposed.identifier),
            "table_uuid": table_uuid,
            "format_version": iceberg_table.metadata.format_version,
            "location": iceberg_table.location(),
            "metadata_location": iceberg_table.metadata_location,
            "schema": {"schema_id": schema.schema_id, "fields": [
                {"id": item.field_id, "name": item.name, "type": str(item.field_type), "required": item.required,
                 "doc": item.doc} for item in schema.fields]},
            "partition_spec": {"spec_id": spec.spec_id, "fields": [
                {"source_column": schema.find_column_name(item.source_id), "transform": str(item.transform),
                 "name": item.name} for item in spec.fields]},
            "sort_order": [{"source_column": schema.find_column_name(item.source_id), "transform": str(item.transform),
                            "direction": str(item.direction), "null_order": str(item.null_order)}
                           for item in iceberg_table.sort_order().fields],
            "row_count": _total_records(iceberg_table),
            "snapshot_count": len(snapshots),
            "recent_snapshots": [_snapshot_entry(item) for item in snapshots[:RECENT_SNAPSHOTS]],
            "properties": redact(dict(iceberg_table.properties)),
            "freshness": freshness,
            "latest_run": latest_run,
            "quality": ldp.get("quality"),
            "ldp": {key: value for key, value in ldp.items() if key not in ("latest_run", "quality")},
        })

    def query(self, sql: str, max_rows: int | None = None) -> dict[str, Any]:
        """Run one guarded read-only query."""
        cap = min(max_rows if max_rows is not None else DEFAULT_QUERY_ROWS, self.max_rows)
        stale = self.sandbox.refresh(timeout_s=self.timeout_s)
        result = self.sandbox.query(sql, max_rows=cap, timeout_s=self.timeout_s)
        return {**result_payload(result, cap), "stale_tables": stale}

    def sample_rows(self, table: str, n: int | None = None) -> dict[str, Any]:
        """The first ``n`` rows of a table."""
        exposed = self._resolve(table)
        cap = min(n if n is not None else DEFAULT_SAMPLE_ROWS, self.max_rows)
        stale = self.sandbox.refresh(timeout_s=self.timeout_s)
        payload = result_payload(self.sandbox.sample(exposed.identifier, cap, timeout_s=self.timeout_s), cap)
        return {"table": exposed.identifier, **payload, "stale_tables": stale}

    def table_history(self, table: str, limit: int | None = None) -> dict[str, Any]:
        """Snapshots newest first, and the table's branches and tags."""
        exposed = self._resolve(table)
        iceberg_table = exposed.load()
        limit = min(limit if limit is not None else DEFAULT_HISTORY, MAX_HISTORY)
        current = iceberg_table.current_snapshot()
        current_id = current.snapshot_id if current is not None else None
        lineage = _main_lineage(iceberg_table)
        snapshots = sorted(iceberg_table.snapshots(), key=lambda item: item.timestamp_ms, reverse=True)
        refs = {name: {"snapshot_id": ref.snapshot_id,
                       "type": getattr(ref.snapshot_ref_type, "value", str(ref.snapshot_ref_type))}
                for name, ref in iceberg_table.metadata.refs.items()}
        return jsonable({
            "table": exposed.identifier,
            "current_snapshot_id": current_id,
            "snapshot_count": len(snapshots),
            "history": [_snapshot_entry(item, current_id=current_id, lineage=lineage) for item in snapshots[:limit]],
            "truncated": len(snapshots) > limit,
            "refs": refs,
        })

    def get_dataset(self, name: str) -> dict[str, Any]:
        """The latest (or ``name@vN``) pinned version of a dataset over an allowlisted table.

        Returns a ``{"available": False, "reason"}`` result, not an error, when this install
        has no :mod:`local_data_platform.datasets` module. When the table is served natively,
        the result includes ``sql`` that reads exactly the pinned rows through ``iceberg_scan``.
        """
        try:
            module = importlib.import_module("local_data_platform.datasets")
        except ImportError:
            return {"available": False, "name": name,
                    "reason": "pinned datasets are not available: this install has no local_data_platform.datasets "
                              "module (added in local-data-platform 0.2.0)"}
        base, _, ref = name.partition("@")
        wanted = None
        if ref:
            try:
                wanted = int(ref.lstrip("vV"))
            except ValueError:
                raise InvalidArguments(f"expected a dataset name or '<name>@v<N>', got {name!r}") from None
        versions = [redact(version) for version in self._dataset_versions(module, base)
                    if version.get("name", base) == base and version.get("table_identifier") in self._tables]
        versions.sort(key=lambda version: (_int_or_none(version.get("version")) or 0, str(version.get("created_at"))))
        chosen = versions[-1] if versions and wanted is None else next(
            (version for version in versions if _int_or_none(version.get("version")) == wanted), None)
        if chosen is None:
            which = f"version {wanted} of dataset {base!r}" if wanted is not None else f"dataset {base!r}"
            raise DatasetNotFound(f"no pinned {which} over the tables this server exposes")
        brief = [{key: version.get(key) for key in ("version", "snapshot_id", "created_at", "row_count")}
                 for version in versions[-20:]]
        result = {"available": True, "name": base, "version": chosen, "version_count": len(versions),
                  "versions": brief}
        sql = self._dataset_sql(chosen)
        if sql is not None:
            result["sql"] = sql
            result["sql_note"] = ("reads the pinned snapshot with iceberg_scan; row_filter is pyiceberg syntax, "
                                  "adjust it if DuckDB rejects it")
        return jsonable(result)

    def _dataset_sql(self, version: Mapping[str, Any]) -> str | None:
        from .sandbox import local_path, quote_identifier, quote_literal

        identifier = version.get("table_identifier")
        metadata = local_path(version.get("metadata_location"))
        snapshot_id = _int_or_none(version.get("snapshot_id"))
        if self.sandbox.access(identifier) != "native" or metadata is None or snapshot_id is None:
            return None
        if not any(metadata.startswith(folder) for folder in self.sandbox.allowed_directories):
            return None
        fields = version.get("selected_fields")
        columns = ", ".join(quote_identifier(item) for item in fields) if fields else "*"
        sql = f"SELECT {columns} FROM iceberg_scan({quote_literal(metadata)}, snapshot_from_id => {snapshot_id})"
        row_filter = version.get("row_filter")
        return f"{sql} WHERE {row_filter}" if row_filter else sql

    def _dataset_versions(self, module: Any, name: str) -> list[dict[str, Any]]:
        warehouses = self.discovery.warehouses()
        list_versions = getattr(module, "list_versions", None)
        found: list[Any] = []
        if callable(list_versions):
            try:
                parameters = inspect.signature(list_versions).parameters
                keyword = next((item for item in _DATASET_DIR_PARAMS if item in parameters), None)
                if keyword is not None:
                    for warehouse in warehouses:
                        found.extend(list_versions(name, **{keyword: warehouse}))
                else:
                    found.extend(list_versions(name))
            except (TypeError, ValueError) as error:
                logger.debug("datasets.list_versions(%r) did not fit (%s); reading manifests", name, error)
                found = []
            except LDPError as error:
                logger.debug("datasets.list_versions(%r) failed (%s); reading manifests", name, error)
                found = []
        if not found:
            found = _read_manifests(warehouses, name)
        return [_version_dict(item) for item in found]


def _version_dict(version: Any) -> dict[str, Any]:
    if isinstance(version, Mapping):
        return dict(version)
    to_dict = getattr(version, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if dataclasses.is_dataclass(version) and not isinstance(version, type):
        return dataclasses.asdict(version)
    return dict(vars(version))


def _read_manifests(warehouses: Iterable[Path], name: str) -> list[dict[str, Any]]:
    """Dataset manifests named ``name`` under ``<warehouse>/.ldp/datasets/``."""
    found = []
    for warehouse in warehouses:
        root = Path(warehouse) / ".ldp" / "datasets"
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, Mapping) and data.get("name") == name:
                found.append(dict(data))
    return found


__all__ = [
    "DEFAULT_MAX_ROWS",
    "DEFAULT_QUERY_ROWS",
    "DEFAULT_SAMPLE_ROWS",
    "DEFAULT_TIMEOUT_S",
    "LakeTools",
    "ToolOutcome",
    "ToolSpec",
    "default_audit_path",
    "jsonable",
    "result_payload",
]
