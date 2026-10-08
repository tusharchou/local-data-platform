"""The MCP server's audit log: one record per tool call, never the result data.

Each record holds the tool, its (scalar) arguments, the SQL for ``query``, the table,
the number of rows returned, whether they were truncated, the duration, the status
(``ok``, ``rejected``, ``denied``, ``timeout`` or ``error``) and the error message.

Records always go to a JSON Lines file, flushed after every call. They also go to the
``_ldp.audit`` Iceberg table in the first served catalog through
:meth:`local_data_platform.events.IcebergSink.emit_audit`, which buffers them and appends
them when the server stops (or every ``batch_size`` records). :func:`iceberg_audit_sink`
looks the method up by name (``emit_audit``, ``append_audit`` or ``audit``, taking one
record mapping); without one, or if the sink cannot be built, the JSONL file is the only
audit trail.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import threading
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.logger import get_logger

logger = get_logger(__name__)

AUDIT_SCHEMA_VERSION = 1
MAX_AUDIT_SQL_CHARS = 100_000
AUDIT_METHODS = ("emit_audit", "append_audit", "audit")
"""``IcebergSink`` methods, in the order tried, that take one audit record mapping."""


@dataclass(frozen=True)
class AuditRecord:
    """One audited tool call.

    Attributes:
        event_id: A random id for the record.
        ts: When the call finished, in UTC, ISO 8601.
        tool: The tool name as the client sent it.
        status: ``ok``, ``rejected``, ``denied``, ``timeout`` or ``error``.
        duration_ms: Wall time of the call.
        sql: The SQL of a ``query`` call, capped at 100,000 characters.
        table: The table argument, when the tool takes one.
        arguments: The other scalar arguments (``n``, ``max_rows``, ``name``, ...).
        rows: Rows returned to the client.
        truncated: Whether rows were cut at the row cap.
        error: The error message sent to the client.
        schema_version: The record layout version.
    """

    tool: str
    status: str
    duration_ms: float
    sql: str | None = None
    table: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    rows: int | None = None
    truncated: bool | None = None
    error: str | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"))
    schema_version: int = AUDIT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """The record as a JSON-ready dict."""
        data = asdict(self)
        data["arguments"] = dict(self.arguments)
        return data


def iceberg_audit_sink(catalog_spec: Mapping[str, Any], base_dir: str | os.PathLike | None = None) -> Any:
    """Return an ``events.IcebergSink`` that can audit, or ``None`` if there is none.

    Args:
        catalog_spec: The catalog spec whose ``_ldp.audit`` table receives the records.
        base_dir: The folder relative paths in the spec resolve against.

    Returns:
        ``None`` when :mod:`local_data_platform.events` or its ``IcebergSink`` is missing,
        when the sink class has none of :data:`AUDIT_METHODS`, or when building it fails
        (logged as a warning).
    """
    try:
        events = importlib.import_module("local_data_platform.events")
    except ImportError:
        logger.debug("local_data_platform.events is not available; auditing to JSONL only")
        return None
    sink_class = getattr(events, "IcebergSink", None)
    if sink_class is None or not any(callable(getattr(sink_class, name, None)) for name in AUDIT_METHODS):
        logger.info("events.IcebergSink has no audit method (%s); auditing to JSONL only", ", ".join(AUDIT_METHODS))
        return None
    try:
        return sink_class(catalog_spec, base_dir)
    except Exception as error:  # noqa: BLE001 - the JSONL audit still works
        logger.warning("Could not open the _ldp.audit Iceberg sink (%s); auditing to JSONL only", error)
        return None


class AuditLog:
    """Write :class:`AuditRecord` objects to a JSONL file and, optionally, an Iceberg sink.

    Writing is thread-safe. The JSONL file is opened (and its folder created) on the first
    record, then appended to and flushed after every record, so a crash loses nothing that
    was acknowledged and a server that never answers a call leaves no file behind.

    Args:
        path: The JSONL file. ``None`` disables the file (for tests that inspect
            :attr:`records` only).
        sink: An object with one of :data:`AUDIT_METHODS` (and optionally ``flush()``),
            normally from :func:`iceberg_audit_sink`.
        keep: Keep the last ``keep`` records in :attr:`records`, for inspection.
    """

    def __init__(self, path: str | os.PathLike | None, *, sink: Any = None, keep: int = 1000):
        self.path = Path(path) if path is not None else None
        self._sink = sink
        self._sink_method = next((getattr(sink, name) for name in AUDIT_METHODS
                                  if sink is not None and callable(getattr(sink, name, None))), None)
        if sink is not None and self._sink_method is None:
            raise TypeError(f"audit sink {type(sink).__name__} has none of the methods {AUDIT_METHODS}")
        self._keep = keep
        self._lock = threading.Lock()
        self._file = None
        self._closed = False
        self.records: list[AuditRecord] = []

    @property
    def iceberg_enabled(self) -> bool:
        """Whether records also go to ``_ldp.audit``."""
        return self._sink_method is not None

    def record(self, entry: AuditRecord) -> None:
        """Write one record. A failing Iceberg sink is logged and does not fail the call."""
        data = entry.to_dict()
        if data.get("sql") and len(data["sql"]) > MAX_AUDIT_SQL_CHARS:
            data["sql"] = data["sql"][:MAX_AUDIT_SQL_CHARS]
        with self._lock:
            self.records.append(entry)
            del self.records[:-self._keep]
            if self.path is not None and not self._closed:
                if self._file is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._file = open(self.path, "a", encoding="utf-8")
                self._file.write(json.dumps(data, default=str, ensure_ascii=False) + "\n")
                self._file.flush()
            if self._sink_method is not None:
                try:
                    self._sink_method(data)
                except Exception as error:  # noqa: BLE001 - the JSONL record was written
                    logger.warning("Could not write an audit record to _ldp.audit: %s", error)

    def close(self) -> None:
        """Flush the Iceberg sink and close the file. Safe to call more than once.

        Records after :meth:`close` are kept in :attr:`records` but not written.
        """
        with self._lock:
            self._closed = True
            flush = getattr(self._sink, "flush", None)
            if callable(flush):
                try:
                    flush()
                except Exception as error:  # noqa: BLE001
                    logger.warning("Could not flush the _ldp.audit sink: %s", error)
            if self._file is not None:
                self._file.close()
                self._file = None

    def __repr__(self) -> str:
        return f"AuditLog(path={str(self.path) if self.path else None!r}, iceberg={self.iceberg_enabled})"


__all__ = ["AUDIT_METHODS", "AuditLog", "AuditRecord", "iceberg_audit_sink"]
