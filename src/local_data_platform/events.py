"""Run events: what each pipeline run did, as a stream of :class:`RunEvent` records.

Every :meth:`~local_data_platform.pipeline.Pipeline.run` emits events to an
:class:`EventSink`::

    run.started -> run.extracted -> quality.evaluated
        -> run.published | run.skipped_duplicate | run.blocked_quality | run.failed
        -> run.finished

A run that fails before its checks jumps straight to ``run.failed``. The envelope is
the one in ``docs/design/saas_architecture.md`` section 6.5, shared by the laptop and
the cloud: consumers apply each event at most once per ``(run_id, attempt, seq)`` and
ignore fields they don't know.

Sinks:

* :class:`NullSink` drops everything. It is the default, so 0.1.1 behaviour is unchanged.
* :class:`MemorySink` keeps the events in a list, for tests and notebooks.
* :class:`JsonlSink` appends one JSON object per line to a local file.
* :class:`OpenLineageSink` turns each run into OpenLineage 1.x ``START`` and
  ``COMPLETE``/``FAIL`` events, written to a JSONL file or POSTed to an HTTP endpoint.
* :class:`IcebergSink` appends batches of events to ``_ldp.runs`` and
  ``_ldp.quality_results``, two day-partitioned Iceberg tables in the pipeline's catalog.
  Its :meth:`~IcebergSink.emit_audit` also takes the MCP server's audit records, for
  ``_ldp.audit``.
* :class:`MultiSink` fans out to several sinks.

Payloads follow the boundary allowlist (SaaS section 10.2): specs, schemas, counts,
snapshot ids and summaries, and check verdicts. They never carry rows, failing-row
samples or credentials. See ``docs/observability.md``.
"""

import dataclasses
import datetime as dt
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

logger = get_logger(__name__)

SCHEMA_VERSION = 1
"""Version of the :class:`RunEvent` envelope."""

EVENT_TYPES = (
    "run.started",
    "run.extracted",
    "quality.evaluated",
    "run.staged",
    "run.published",
    "run.skipped_duplicate",
    "run.blocked_quality",
    "run.failed",
    "run.finished",
    "table.maintained",
)
"""The event types LDP emits. Consumers must ignore types they don't know."""

RUN_STATUSES = ("published", "skipped_duplicate", "blocked_quality", "failed")
"""The ``status`` a ``run.finished`` event reports."""

SYSTEM_NAMESPACE = "_ldp"
"""The namespace LDP's own tables live in, inside the customer's catalog."""

RUNS_TABLE = "runs"
QUALITY_TABLE = "quality_results"
AUDIT_TABLE = "audit"
"""``_ldp.audit``: one row per MCP tool call (``mcp_server.audit``), never the result data."""

DEFAULT_JSONL_PATH = ".ldp/events.jsonl"
"""Where the ``jsonl`` sink writes when a config gives no ``path`` (relative to the config's folder)."""

DEFAULT_OPENLINEAGE_PATH = ".ldp/openlineage.jsonl"
"""Where the ``openlineage`` sink writes when a config gives neither ``path`` nor ``url``."""

SINK_TYPES = ("null", "jsonl", "openlineage", "iceberg")
"""The sink types ``metadata.observability.sinks`` accepts."""

OPENLINEAGE_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_OL_FACETS = "https://openlineage.io/spec/facets"
_OL_FACET_SCHEMAS = {
    "schema": f"{_OL_FACETS}/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet",
    "dataQualityMetrics":
        f"{_OL_FACETS}/1-0-1/DataQualityMetricsDatasetFacet.json#/$defs/DataQualityMetricsDatasetFacet",
    "dataQualityAssertions":
        f"{_OL_FACETS}/1-1-0/DataQualityAssertionsDatasetFacet.json#/$defs/DataQualityAssertionsDatasetFacet",
    "outputStatistics":
        f"{_OL_FACETS}/1-0-2/OutputStatisticsOutputDatasetFacet.json#/$defs/OutputStatisticsOutputDatasetFacet",
    "errorMessage": f"{_OL_FACETS}/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet",
    "nominalTime": f"{_OL_FACETS}/1-0-1/NominalTimeRunFacet.json#/$defs/NominalTimeRunFacet",
    "jobType": f"{_OL_FACETS}/2-0-4/JobTypeJobFacet.json#/$defs/JobTypeJobFacet",
}
_OL_LINEAGE_PATH = "/api/v1/lineage"

_UTC = dt.timezone.utc
_MAX_MESSAGE = 2000


# ---------------------------------------------------------------------- ids


_uuid7_lock = threading.Lock()
_uuid7_last_ms = 0
_uuid7_counter = 0
_COUNTER_BITS = 42
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1


def _uuid7_fallback(now_ms: int | None = None) -> uuid.UUID:
    """Build an RFC 9562 UUIDv7, monotonic within this process.

    The layout follows CPython 3.14's ``uuid.uuid7``: a 48-bit Unix millisecond
    timestamp, then a 42-bit counter (12 bits in ``rand_a`` and 30 in ``rand_b``) that
    is re-seeded randomly each millisecond and incremented within one, then 32 random
    bits. If the counter overflows, or the clock goes backwards, the timestamp is
    advanced past the last one used, so ids still sort in creation order.

    Args:
        now_ms: The Unix time in milliseconds; the current time by default (for tests).

    Returns:
        The UUID.
    """
    global _uuid7_last_ms, _uuid7_counter
    with _uuid7_lock:
        ms = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
        if ms > _uuid7_last_ms:
            counter = secrets.randbits(_COUNTER_BITS - 1)  # leave headroom so increments rarely overflow
        else:
            ms = _uuid7_last_ms
            counter = _uuid7_counter + 1
            if counter > _COUNTER_MAX:
                ms += 1
                counter = secrets.randbits(_COUNTER_BITS - 1)
        _uuid7_last_ms, _uuid7_counter = ms, counter
    value = ((ms & 0xFFFF_FFFF_FFFF) << 80) | (0x7 << 76) | ((counter >> 30) << 64)
    value |= (0b10 << 62) | ((counter & 0x3FFF_FFFF) << 32) | secrets.randbits(32)
    return uuid.UUID(int=value)


def uuid7() -> str:
    """Return a new time-ordered UUIDv7 as a canonical 36-character string.

    Uses :func:`uuid.uuid7` when Python has it (3.14+), else an equivalent
    implementation of RFC 9562.
    """
    native = getattr(uuid, "uuid7", None)
    return str(native() if native is not None else _uuid7_fallback())


# ---------------------------------------------------------------------- envelope


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=_UTC) if value.tzinfo is None else value.astimezone(_UTC)


def _parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _utc(value)
    if isinstance(value, str):
        return _utc(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return dt.datetime.fromtimestamp(value / 1000, tz=_UTC)
    raise ValueError(f"event ts must be a datetime or an ISO-8601 string, got {value!r}")


def _jsonable(value: Any) -> Any:
    from local_data_platform.quality.report import to_jsonable

    return to_jsonable(value)


@dataclasses.dataclass(frozen=True)
class RunEvent:
    """One thing that happened in a pipeline run (SaaS design section 6.5).

    Attributes:
        event_id: A UUIDv7 string, unique per event.
        type: What happened, one of :data:`EVENT_TYPES`.
        schema_version: The envelope version, :data:`SCHEMA_VERSION`.
        ts: When it happened, timezone-aware UTC.
        run_id: The run's UUIDv7 string.
        attempt: The attempt number, from 1.
        seq: Position within ``(run_id, attempt)``, increasing from 0.
        table_uuid: The target Iceberg table's UUID when known.
        payload: Allowlisted facts about the step: counts, schemas, check verdicts,
            snapshot ids. Always JSON-serialisable.
    """

    event_id: str
    type: str
    schema_version: int
    ts: dt.datetime
    run_id: str
    attempt: int
    seq: int
    table_uuid: str | None = None
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type:
            raise ValueError(f"event type must be a non-empty string, got {self.type!r}")
        if not isinstance(self.ts, dt.datetime):
            raise TypeError(f"event ts must be a datetime, got {type(self.ts).__name__}")
        object.__setattr__(self, "ts", _utc(self.ts))
        object.__setattr__(self, "payload", dict(self.payload or {}))

    def to_dict(self) -> dict[str, Any]:
        """Return the event as JSON-serialisable primitives; ``ts`` is an ISO-8601 string."""
        return {
            "event_id": self.event_id,
            "type": self.type,
            "schema_version": self.schema_version,
            "ts": self.ts.isoformat(),
            "run_id": self.run_id,
            "attempt": self.attempt,
            "seq": self.seq,
            "table_uuid": self.table_uuid,
            "payload": _jsonable(self.payload),
        }

    def to_json(self) -> str:
        """Return the event as one line of compact JSON with sorted keys."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunEvent":
        """Build an event from :meth:`to_dict` output. Unknown fields are ignored.

        Raises:
            ValueError: If a required field is missing or malformed.
        """
        if not isinstance(data, Mapping):
            raise ValueError(f"a run event must be an object, got {type(data).__name__}")
        missing = [key for key in ("event_id", "type", "ts", "run_id") if key not in data]
        if missing:
            raise ValueError(f"run event is missing {missing}")
        payload = data.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            event_id=str(data["event_id"]),
            type=str(data["type"]),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            ts=_parse_ts(data["ts"]),
            run_id=str(data["run_id"]),
            attempt=int(data.get("attempt") or 1),
            seq=int(data.get("seq") or 0),
            table_uuid=data.get("table_uuid"),
            payload=payload,
        )


def event_json_schema() -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) of the :meth:`RunEvent.to_dict` envelope.

    Unknown fields are allowed, so newer producers stay readable by older consumers.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local-data-platform.readthedocs.io/schemas/run-event-v1.json",
        "title": "LDP RunEvent",
        "type": "object",
        "required": ["event_id", "type", "schema_version", "ts", "run_id", "attempt", "seq", "payload"],
        "properties": {
            "event_id": {"type": "string", "format": "uuid"},
            "type": {"type": "string", "minLength": 1, "examples": list(EVENT_TYPES)},
            "schema_version": {"type": "integer", "minimum": 1},
            "ts": {"type": "string", "format": "date-time"},
            "run_id": {"type": "string", "minLength": 1},
            "attempt": {"type": "integer", "minimum": 1},
            "seq": {"type": "integer", "minimum": 0},
            "table_uuid": {"type": ["string", "null"]},
            "payload": {"type": "object"},
        },
        "additionalProperties": True,
    }


# ---------------------------------------------------------------------- redaction and payload helpers


_SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?(-----END [A-Z ]+-----|$)", re.DOTALL), "[REDACTED PEM]"),
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(?i)\b([\w.-]*(?:token|secret|password|passwd|credential|api[_-]?key)[\w.-]*)"
                r"(\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+"), r"\1\2***"),
    (re.compile(r"(?i)(authorization:?\s*(?:bearer|basic)\s+)\S+"), r"\1***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA****************"),
)


def redact_text(text: str, limit: int | None = _MAX_MESSAGE) -> str:
    """Mask things that look like secrets in free text such as an error message.

    Masks PEM blocks, passwords in URLs (``scheme://user:***@host``), ``key=value``
    pairs whose key mentions a token, secret, password, credential or API key, bearer
    tokens, and AWS access key ids.

    Args:
        text: The text.
        limit: Truncate the result to this many characters; ``None`` keeps it whole.

    Returns:
        The redacted text.
    """
    result = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    if limit is not None and len(result) > limit:
        result = result[: limit - 3] + "..."
    return result


def schema_payload(schema: Any) -> list[dict[str, Any]]:
    """Describe a ``pyarrow.Schema`` as ``[{"name", "type"}]``, with ``fields`` for structs.

    Names and types only: schemas are on the boundary allowlist, values are not.
    """
    import pyarrow as pa

    def describe(field: Any) -> dict[str, Any]:
        item: dict[str, Any] = {"name": field.name, "type": str(field.type)}
        if pa.types.is_struct(field.type):
            item["fields"] = [describe(field.type.field(i)) for i in range(field.type.num_fields)]
        return item

    return [describe(field) for field in schema]


_CHECK_COLUMN = re.compile(r"^[a-z_]+\(([^,()]+)\)$")
_VALUE_METRICS = {"sample", "invalid_values"}  # failing-row samples: never on the boundary (SaaS 10.2)
_BOUND_METRICS = {"observed_min", "observed_max"}


def _redact_details(details: str) -> str:
    # Unique and AcceptedValues append examples of failing values after "; e.g. ".
    head, _, _ = str(details).partition("; e.g.")
    return redact_text(head, limit=500)


def _redact_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in metrics.items():
        if key in _VALUE_METRICS:
            continue
        if key in _BOUND_METRICS and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            continue  # observed bounds of non-numeric columns are data
        kept[key] = value
    return _jsonable(kept)


def quality_payload(report: Any) -> dict[str, Any]:
    """Summarise a :class:`~local_data_platform.quality.QualityReport` for an event.

    Keeps each check's name, verdict, failing-row count and numeric metrics. Drops
    failing-value samples (``metrics["sample"]``, ``metrics["invalid_values"]``,
    the ``e.g.`` part of ``details``) and non-numeric observed bounds.

    Returns:
        ``{"passed", "checks_run", "checks_failed", "results": [...]}``.
    """
    results = []
    for result in getattr(report, "results", []) or []:
        match = _CHECK_COLUMN.match(str(result.name))
        results.append({
            "name": str(result.name),
            "column": match.group(1).strip() if match else None,
            "passed": bool(result.passed),
            "failing_rows": int(result.failing_rows or 0),
            "details": _redact_details(result.details),
            "metrics": _redact_metrics(result.metrics or {}),
        })
    failed = sum(1 for item in results if not item["passed"])
    return {"passed": failed == 0, "checks_run": len(results), "checks_failed": failed, "results": results}


def dataset_ref(obj: Any) -> dict[str, Any]:
    """Name a pipeline source or target the way OpenLineage names datasets.

    * Iceberg tables: ``namespace`` is the catalog's warehouse URI, ``name`` is
      ``<namespace>.<table>``.
    * Local files: ``namespace`` is ``file``, ``name`` is the absolute path.
    * Object-store files: ``namespace`` is ``<scheme>://<bucket>``, ``name`` is the key.
    * BigQuery queries: ``namespace`` is ``bigquery``, ``name`` is the source name.
    * Anything else: ``namespace`` is ``ldp``, ``name`` is its ``name``.

    Returns:
        ``{"namespace", "name", "format"}``, plus ``table_identifier`` for Iceberg.
    """
    fmt = getattr(obj, "format", None)
    fmt = str(fmt).upper() if fmt else None
    identifier = getattr(obj, "identifier", None)
    catalog = getattr(obj, "catalog", None)
    if isinstance(identifier, str) and catalog is not None and not isinstance(catalog, Mapping):
        properties = getattr(catalog, "properties", None) or {}
        warehouse = properties.get("warehouse") if isinstance(properties, Mapping) else None
        if not warehouse and getattr(obj, "path", None):
            warehouse = Path(obj.path).resolve().as_uri()
        namespace = str(warehouse or f"iceberg://{getattr(catalog, 'name', 'catalog')}").rstrip("/")
        return {"namespace": redact_text(namespace, None), "name": identifier, "format": fmt or "ICEBERG",
                "table_identifier": identifier}
    if type(obj).__name__ == "BigQuery":
        return {"namespace": "bigquery", "name": str(getattr(obj, "name", "query")), "format": "BIGQUERY"}
    path = getattr(obj, "path", None)
    if path:
        text = str(path)
        parts = urlsplit(text)
        if parts.scheme and len(parts.scheme) > 1 and parts.scheme != "file":
            return {"namespace": f"{parts.scheme}://{parts.netloc}", "name": parts.path.lstrip("/"), "format": fmt}
        local = Path(parts.path if parts.scheme == "file" else text)
        return {"namespace": "file", "name": local.resolve().as_posix(), "format": fmt}
    return {"namespace": "ldp", "name": str(getattr(obj, "name", None) or type(obj).__name__), "format": fmt}


# ---------------------------------------------------------------------- sinks


@runtime_checkable
class EventSink(Protocol):
    """Where run events go. ``emit`` may buffer; ``flush`` persists what is buffered.

    A pipeline calls ``flush`` once at the end of every run, whatever the outcome.
    Sink errors never fail a run: the pipeline logs them as warnings.
    """

    def emit(self, event: RunEvent) -> None:
        """Record one event."""

    def flush(self) -> None:
        """Persist any buffered events."""


class NullSink:
    """Drops every event. The default sink, so runs behave exactly as in 0.1.1."""

    def emit(self, event: RunEvent) -> None:
        """Drop the event."""

    def flush(self) -> None:
        """Do nothing."""

    def __repr__(self) -> str:
        return "NullSink()"


class MemorySink:
    """Keeps events in :attr:`events`, in emit order. For tests and notebooks.

    Attributes:
        events: Every event emitted so far.
        flushes: How many times :meth:`flush` was called.
    """

    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self.flushes = 0

    def emit(self, event: RunEvent) -> None:
        """Append the event to :attr:`events`."""
        self.events.append(event)

    def flush(self) -> None:
        """Count the flush; the events are already in memory."""
        self.flushes += 1

    @property
    def types(self) -> list[str]:
        """The event types, in emit order."""
        return [event.type for event in self.events]

    def __repr__(self) -> str:
        return f"MemorySink({len(self.events)} events)"


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    # One O_APPEND write per line, so concurrent writers on a local disk don't interleave lines.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


class JsonlSink:
    """Appends each event to a local file as one line of JSON (:meth:`RunEvent.to_json`).

    Every ``emit`` writes straight away, so a crash loses nothing already emitted.
    Read the file back with :func:`read_jsonl`.

    Args:
        path: The file. Parent folders are created on the first write.
        base_dir: Folder a relative ``path`` resolves against (default: the cwd).
    """

    def __init__(self, path: str | os.PathLike, base_dir: str | os.PathLike | None = None):
        self.path = resolve_path(path, base_dir)
        self._lock = threading.Lock()

    def emit(self, event: RunEvent) -> None:
        """Append the event to the file."""
        with self._lock:
            _append_line(self.path, event.to_json())

    def flush(self) -> None:
        """Do nothing: every event is written when emitted."""

    def __repr__(self) -> str:
        return f"JsonlSink({str(self.path)!r})"


def read_jsonl(path: str | os.PathLike, base_dir: str | os.PathLike | None = None) -> list[RunEvent]:
    """Read the events a :class:`JsonlSink` wrote, in file order.

    Blank lines are skipped. A line that is not a valid event (for example one cut
    short by a crash) is skipped with a warning.

    Raises:
        FileNotFoundError: If the file doesn't exist.
    """
    resolved = resolve_path(path, base_dir)
    events = []
    with open(resolved, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                events.append(RunEvent.from_dict(json.loads(line)))
            except (ValueError, TypeError) as exc:
                logger.warning("Skipping line %d of %s: not a run event (%s)", number, resolved, exc)
    return events


class MultiSink:
    """Sends every event to several sinks. One sink failing doesn't stop the others.

    Args:
        sinks: The sinks, in order.
    """

    def __init__(self, sinks: Iterable[EventSink]):
        self.sinks = list(sinks)
        for sink in self.sinks:
            _check_sink(sink)

    def emit(self, event: RunEvent) -> None:
        """Emit to each sink; errors are logged as warnings."""
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception as exc:  # noqa: BLE001 - one broken sink must not starve the others
                logger.warning("Event sink %r failed to record %s: %s", sink, event.type, redact_text(str(exc)))

    def flush(self) -> None:
        """Flush each sink; errors are logged as warnings."""
        for sink in self.sinks:
            try:
                sink.flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Event sink %r failed to flush: %s", sink, redact_text(str(exc)))

    def __repr__(self) -> str:
        return f"MultiSink({self.sinks!r})"


def _check_sink(sink: Any) -> None:
    if not (callable(getattr(sink, "emit", None)) and callable(getattr(sink, "flush", None))):
        raise TypeError(f"an event sink needs emit(event) and flush() methods, got {type(sink).__name__}")


# ---------------------------------------------------------------------- OpenLineage


def _ol_facet(name: str, producer: str, **fields: Any) -> dict[str, Any]:
    return {"_producer": producer, "_schemaURL": _OL_FACET_SCHEMAS[name], **fields}


def _ol_run_id(run_id: str) -> str:
    """OpenLineage needs a canonical UUID: normalise a dashless hex id, hash anything else (uuid5)."""
    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ldp-run:{run_id}"))


def _default_producer() -> str:
    from local_data_platform import __version__

    return f"https://github.com/tusharchou/local-data-platform/tree/v{__version__}"


class OpenLineageSink:
    """Emits each run as OpenLineage 1.x ``RunEvent`` JSON (spec 2-0-2).

    ``run.started`` becomes a ``START`` event. ``run.finished`` becomes ``COMPLETE``
    (published or skipped duplicate) or ``FAIL`` (blocked by quality checks, or
    failed). The other LDP events of the run are folded into those two:

    * ``run.runId`` is the LDP run id (as a canonical UUID) and ``job`` is
      ``{namespace, pipeline name}``.
    * ``inputs`` and ``outputs`` are named as in :func:`dataset_ref`, with a ``schema``
      facet once the data has been read.
    * The output carries ``dataQualityMetrics`` (row count and per-column null counts
      of the validated batch) and ``dataQualityAssertions`` (one per check), plus an
      ``outputStatistics`` output facet (rows, bytes and files written) when published.
    * The run carries ``nominalTime`` for a logical window, ``errorMessage`` on
      failure, and an ``ldp_run`` facet with the attempt, status, idempotency key,
      spec hash and snapshot id.

    Args:
        path_or_url: A local file (one JSON event per line) or an ``http(s)://``
            endpoint. A URL with no path posts to ``/api/v1/lineage`` (Marquez).
        namespace: The OpenLineage job namespace.
        api_key_env: The name of an environment variable holding a bearer token for
            HTTP. The token is read at send time and never logged.
        timeout: HTTP timeout in seconds.
        base_dir: Folder a relative file path resolves against.
        producer: The ``producer`` URI; defaults to this package's repository and version.
    """

    def __init__(self, path_or_url: str | os.PathLike, *, namespace: str = "ldp", api_key_env: str | None = None,
                 timeout: float = 10.0, base_dir: str | os.PathLike | None = None, producer: str | None = None):
        text = str(path_or_url)
        scheme = urlsplit(text).scheme.lower()
        self.url: str | None = None
        self.path: Path | None = None
        if scheme in ("http", "https"):
            parts = urlsplit(text)
            self.url = text if parts.path not in ("", "/") else text.rstrip("/") + _OL_LINEAGE_PATH
        else:
            self.path = resolve_path(text, base_dir)
        if not namespace or not isinstance(namespace, str):
            raise ConfigError("the OpenLineage job namespace must be a non-empty string")
        self.namespace = namespace
        self.api_key_env = api_key_env
        self.timeout = float(timeout)
        self.producer = producer or _default_producer()
        self._runs: dict[tuple[str, int], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        target = redact_text(self.url, None) if self.url else str(self.path)
        return f"OpenLineageSink({target!r}, namespace={self.namespace!r})"

    def emit(self, event: RunEvent) -> None:
        """Fold the event into its run and send an OpenLineage event at start and finish."""
        key = (event.run_id, event.attempt)
        with self._lock:
            state = self._runs.setdefault(key, {})
            state[event.type] = event
            if event.type == "run.started":
                lineage = self.to_openlineage(state, "START", event)
            elif event.type == "run.finished":
                lineage = self.to_openlineage(state, self._final_type(state, event), event)
                del self._runs[key]
            else:
                return
        self._send(lineage)

    def flush(self) -> None:
        """Do nothing: OpenLineage events are sent when a run starts and finishes."""

    @staticmethod
    def _final_type(state: Mapping[str, RunEvent], finished: RunEvent) -> str:
        status = finished.payload.get("status")
        if status in ("blocked_quality", "failed") or "run.failed" in state or "run.blocked_quality" in state:
            return "FAIL"
        return "COMPLETE"

    def to_openlineage(self, state: Mapping[str, RunEvent], event_type: str, event: RunEvent) -> dict[str, Any]:
        """Build one OpenLineage ``RunEvent`` dict from the LDP events of a run seen so far.

        Args:
            state: The run's LDP events, keyed by type.
            event_type: ``START``, ``COMPLETE`` or ``FAIL``.
            event: The LDP event that triggered it; its ``ts`` is the ``eventTime``.

        Returns:
            The OpenLineage event.
        """
        started = state.get("run.started")
        start = started.payload if started is not None else {}
        context = event.payload
        pipeline = str(start.get("pipeline") or context.get("pipeline") or "pipeline")
        producer = self.producer

        run_facets: dict[str, Any] = {}
        window = start.get("logical_window")
        if isinstance(window, (list, tuple)) and len(window) == 2 and window[0]:
            run_facets["nominalTime"] = _ol_facet("nominalTime", producer, nominalStartTime=window[0],
                                                  **({"nominalEndTime": window[1]} if window[1] else {}))
        failed = state.get("run.failed")
        if failed is not None and event_type == "FAIL":
            message = f"{failed.payload.get('error_type', 'Error')}: {failed.payload.get('message', '')}"
            run_facets["errorMessage"] = _ol_facet("errorMessage", producer, message=message,
                                                   programmingLanguage="python")
        blocked = state.get("run.blocked_quality")
        if blocked is not None and event_type == "FAIL" and "errorMessage" not in run_facets:
            names = ", ".join(blocked.payload.get("failures", []))
            run_facets["errorMessage"] = _ol_facet(
                "errorMessage", producer, message=f"blocked by failed quality checks: {names}",
                programmingLanguage="python")
        if event_type != "START":
            finish = context
            run_facets["ldp_run"] = {
                "_producer": producer,
                "_schemaURL": "https://local-data-platform.readthedocs.io/observability/#openlineage",
                "attempt": event.attempt,
                "status": finish.get("status"),
                "idempotencyKey": finish.get("idempotency_key") or start.get("idempotency_key"),
                "specHash": start.get("spec_hash"),
                "snapshotId": finish.get("published_snapshot_id"),
            }

        inputs = self._datasets(start.get("source"), state.get("run.extracted"), None, None)
        outputs = self._datasets(start.get("target"), state.get("quality.evaluated"), state.get("quality.evaluated"),
                                 state.get("run.published"))
        return {
            "eventType": event_type,
            "eventTime": event.ts.isoformat(timespec="milliseconds"),
            "producer": producer,
            "schemaURL": OPENLINEAGE_SCHEMA_URL,
            "run": {"runId": _ol_run_id(event.run_id), "facets": run_facets},
            "job": {
                "namespace": self.namespace,
                "name": pipeline,
                "facets": {"jobType": _ol_facet("jobType", producer, processingType="BATCH", integration="LDP",
                                                jobType="PIPELINE")},
            },
            "inputs": inputs,
            "outputs": outputs,
        }

    def _datasets(self, ref: Any, schema_event: RunEvent | None, quality_event: RunEvent | None,
                  published: RunEvent | None) -> list[dict[str, Any]]:
        if not isinstance(ref, Mapping) or not ref.get("name"):
            return []
        producer = self.producer
        dataset: dict[str, Any] = {"namespace": str(ref.get("namespace") or "ldp"), "name": str(ref["name"]),
                                   "facets": {}}
        if schema_event is not None and schema_event.payload.get("schema"):
            fields = [self._ol_field(item) for item in schema_event.payload["schema"]]
            dataset["facets"]["schema"] = _ol_facet("schema", producer, fields=fields)
        if quality_event is not None:
            payload = quality_event.payload
            nulls = payload.get("null_counts") or {}
            dataset["facets"]["dataQualityMetrics"] = _ol_facet(
                "dataQualityMetrics", producer, rowCount=int(payload.get("rows") or 0),
                columnMetrics={str(column): {"nullCount": int(count)} for column, count in nulls.items()})
            assertions = []
            for result in payload.get("results", []):
                assertion = {"assertion": result["name"], "success": bool(result["passed"])}
                if result.get("column"):
                    assertion["column"] = result["column"]
                assertions.append(assertion)
            if assertions:
                dataset["facets"]["dataQualityAssertions"] = _ol_facet("dataQualityAssertions", producer,
                                                                       assertions=assertions)
        if published is not None:
            summary = published.payload.get("snapshot_summary") or {}
            stats: dict[str, Any] = {"rowCount": int(published.payload.get("rows_written") or 0)}
            for key, name in (("added-files-size", "size"), ("added-data-files", "fileCount")):
                try:
                    stats[name] = int(summary[key])
                except (KeyError, TypeError, ValueError):
                    pass
            dataset["outputFacets"] = {"outputStatistics": _ol_facet("outputStatistics", producer, **stats)}
        return [dataset]

    @classmethod
    def _ol_field(cls, item: Mapping[str, Any]) -> dict[str, Any]:
        field = {"name": str(item.get("name")), "type": str(item.get("type"))}
        if item.get("fields"):
            field["fields"] = [cls._ol_field(child) for child in item["fields"]]
        return field

    def _send(self, lineage: Mapping[str, Any]) -> None:
        text = json.dumps(lineage, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if self.path is not None:
            with self._lock:
                _append_line(self.path, text)
            return
        import requests

        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            token = os.environ.get(self.api_key_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                logger.warning("OpenLineage sink: environment variable %s is not set; sending without a token",
                               self.api_key_env)
        response = requests.post(self.url, data=text.encode("utf-8"), headers=headers, timeout=self.timeout)
        if response.status_code >= 300:
            raise LDPError(f"OpenLineage endpoint {redact_text(self.url, None)} answered HTTP {response.status_code}")


def read_openlineage(path: str | os.PathLike, base_dir: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    """Read the OpenLineage events an :class:`OpenLineageSink` wrote to a file, in order."""
    resolved = resolve_path(path, base_dir)
    with open(resolved, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ---------------------------------------------------------------------- Iceberg (_ldp namespace)


def _runs_arrow_schema():
    import pyarrow as pa

    return pa.schema([
        pa.field("event_id", pa.string()),
        pa.field("type", pa.string()),
        pa.field("schema_version", pa.int32()),
        pa.field("ts", pa.timestamp("us", tz="UTC")),
        pa.field("run_id", pa.string()),
        pa.field("attempt", pa.int32()),
        pa.field("seq", pa.int32()),
        pa.field("table_uuid", pa.string()),
        pa.field("pipeline", pa.string()),
        pa.field("table_identifier", pa.string()),
        pa.field("status", pa.string()),
        pa.field("idempotency_key", pa.string()),
        pa.field("snapshot_id", pa.int64()),
        pa.field("payload", pa.string()),
    ])


def _quality_arrow_schema():
    import pyarrow as pa

    return pa.schema([
        pa.field("event_id", pa.string()),
        pa.field("ts", pa.timestamp("us", tz="UTC")),
        pa.field("run_id", pa.string()),
        pa.field("attempt", pa.int32()),
        pa.field("pipeline", pa.string()),
        pa.field("table_identifier", pa.string()),
        pa.field("check_index", pa.int32()),
        pa.field("check_name", pa.string()),
        pa.field("column_name", pa.string()),
        pa.field("passed", pa.bool_()),
        pa.field("failing_rows", pa.int64()),
        pa.field("on_failure", pa.string()),
        pa.field("details", pa.string()),
        pa.field("metrics", pa.string()),
    ])


def _audit_arrow_schema():
    import pyarrow as pa

    return pa.schema([
        pa.field("event_id", pa.string()),
        pa.field("ts", pa.timestamp("us", tz="UTC")),
        pa.field("tool", pa.string()),
        pa.field("status", pa.string()),
        pa.field("duration_ms", pa.float64()),
        pa.field("sql", pa.string()),
        pa.field("table_identifier", pa.string()),
        pa.field("arguments", pa.string()),
        pa.field("rows", pa.int64()),
        pa.field("truncated", pa.bool_()),
        pa.field("error", pa.string()),
        pa.field("schema_version", pa.int32()),
    ])


def _audit_row(record: Mapping[str, Any]) -> dict[str, Any]:
    ts = record.get("ts")
    duration = record.get("duration_ms")
    truncated = record.get("truncated")
    return {
        "event_id": str(record.get("event_id") or uuid.uuid4().hex),
        "ts": _parse_ts(ts) if ts is not None else dt.datetime.now(_UTC),
        "tool": None if record.get("tool") is None else str(record["tool"]),
        "status": None if record.get("status") is None else str(record["status"]),
        "duration_ms": None if duration is None else float(duration),
        "sql": record.get("sql"),
        "table_identifier": record.get("table"),
        "arguments": _compact(record.get("arguments") or {}),
        "rows": _int_or_none(record.get("rows")),
        "truncated": None if truncated is None else bool(truncated),
        "error": None if record.get("error") is None else redact_text(str(record["error"])),
        "schema_version": _int_or_none(record.get("schema_version")),
    }


def _compact(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def _run_row(event: RunEvent) -> dict[str, Any]:
    payload = event.payload
    status = payload.get("status") if event.type == "run.finished" else None
    snapshot = payload.get("published_snapshot_id", payload.get("snapshot_id"))
    return {
        "event_id": event.event_id, "type": event.type, "schema_version": event.schema_version, "ts": event.ts,
        "run_id": event.run_id, "attempt": event.attempt, "seq": event.seq, "table_uuid": event.table_uuid,
        "pipeline": payload.get("pipeline"), "table_identifier": payload.get("table"), "status": status,
        "idempotency_key": payload.get("idempotency_key"), "snapshot_id": _int_or_none(snapshot),
        "payload": _compact(payload),
    }


def _quality_rows(event: RunEvent) -> list[dict[str, Any]]:
    payload = event.payload
    rows = []
    for index, result in enumerate(payload.get("results", []) or []):
        rows.append({
            "event_id": event.event_id, "ts": event.ts, "run_id": event.run_id, "attempt": event.attempt,
            "pipeline": payload.get("pipeline"), "table_identifier": payload.get("table"), "check_index": index,
            "check_name": result.get("name"), "column_name": result.get("column"),
            "passed": bool(result.get("passed")), "failing_rows": _int_or_none(result.get("failing_rows")),
            "on_failure": payload.get("on_failure"), "details": result.get("details"),
            "metrics": _compact(result.get("metrics") or {}),
        })
    return rows


_SYSTEM_TABLE_PROPERTIES = {
    "ldp.managed": "true",
    "ldp.system": "events",
    "write.metadata.delete-after-commit.enabled": "true",
    "write.metadata.previous-versions-max": "50",
}


class IcebergSink:
    """Appends events to ``_ldp.runs`` and quality results to ``_ldp.quality_results``.

    Both tables live in the pipeline's own catalog (SaaS design section 6.4), are
    partitioned by ``day(ts)``, and are created on the first flush. ``_ldp.runs``
    holds one row per event, with the payload as JSON and the useful fields promoted
    to columns. ``_ldp.quality_results`` holds one row per check of every
    ``quality.evaluated`` event. :meth:`emit_audit` takes the MCP server's audit
    records for ``_ldp.audit``, the same way.

    Events are buffered and appended in one commit per table on :meth:`flush`, which
    a pipeline calls when each run ends (the MCP server when it stops), or earlier once
    ``batch_size`` records are waiting. A buffer is cleared only after its append
    commits, so a failed flush is retried by the next one.

    Args:
        catalog_spec: The catalog block, e.g. a config's ``target.catalog``; built
            with :func:`~local_data_platform.catalog.provider.create_catalog`.
        base_dir: Folder relative paths in ``catalog_spec`` resolve against.
        catalog: An existing pyiceberg catalog to use instead of ``catalog_spec``.
        namespace: The system namespace, ``_ldp`` by default.
        batch_size: Flush automatically once this many events are buffered.

    Raises:
        ConfigError: If neither ``catalog_spec`` nor ``catalog`` is given.
    """

    def __init__(self, catalog_spec: Mapping[str, Any] | None = None, base_dir: str | os.PathLike | None = None, *,
                 catalog: Any = None, namespace: str = SYSTEM_NAMESPACE, batch_size: int = 500):
        if catalog_spec is None and catalog is None:
            raise ConfigError("IcebergSink needs a catalog spec (e.g. the target's 'catalog' block) or catalog=")
        if catalog_spec is not None and not isinstance(catalog_spec, Mapping):
            raise ConfigError(f"IcebergSink catalog spec must be an object, got {type(catalog_spec).__name__}")
        if not isinstance(batch_size, int) or batch_size < 1:
            raise ConfigError(f"IcebergSink batch_size must be a positive integer, got {batch_size!r}")
        self._spec = dict(catalog_spec) if catalog_spec is not None else None
        self._base_dir = base_dir
        self._catalog = catalog
        self.namespace = namespace
        self.batch_size = batch_size
        self._runs: list[RunEvent] = []
        self._quality: list[dict[str, Any]] = []
        self._audit: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        # Never the spec itself: it may name credentials.
        kind = (self._spec or {}).get("type", "local") if self._spec is not None else type(self._catalog).__name__
        return f"IcebergSink(namespace={self.namespace!r}, catalog_type={str(kind)!r})"

    @property
    def runs_identifier(self) -> str:
        """``"_ldp.runs"``."""
        return f"{self.namespace}.{RUNS_TABLE}"

    @property
    def quality_identifier(self) -> str:
        """``"_ldp.quality_results"``."""
        return f"{self.namespace}.{QUALITY_TABLE}"

    @property
    def audit_identifier(self) -> str:
        """``"_ldp.audit"``."""
        return f"{self.namespace}.{AUDIT_TABLE}"

    @property
    def catalog(self) -> Any:
        """The pyiceberg catalog, built on first use."""
        if self._catalog is None:
            from local_data_platform.catalog.provider import create_catalog

            self._catalog = create_catalog(self._spec, base_dir=self._base_dir)
        return self._catalog

    def emit(self, event: RunEvent) -> None:
        """Buffer the event (and its quality results); flush once ``batch_size`` are waiting."""
        with self._lock:
            self._runs.append(event)
            if event.type == "quality.evaluated":
                self._quality.extend(_quality_rows(event))
            full = len(self._runs) >= self.batch_size
        if full:
            self.flush()

    def emit_audit(self, record: Mapping[str, Any]) -> None:
        """Buffer one MCP audit record for ``_ldp.audit``; flush once ``batch_size`` are waiting.

        Args:
            record: An ``AuditRecord.to_dict()`` mapping: ``tool``, ``status``, ``duration_ms``,
                ``sql``, ``table``, ``arguments``, ``rows``, ``truncated``, ``error``,
                ``event_id``, ``ts`` and ``schema_version``. It never holds result data.
        """
        row = _audit_row(record)
        with self._lock:
            self._audit.append(row)
            full = len(self._audit) >= self.batch_size
        if full:
            self.flush()

    def flush(self) -> None:
        """Append the buffered events and audit records, one commit per table."""
        import pyarrow as pa

        with self._lock:
            if self._runs:
                rows = [_run_row(event) for event in self._runs]
                self._append(self.runs_identifier, pa.Table.from_pylist(rows, schema=_runs_arrow_schema()))
                self._runs.clear()
            if self._quality:
                self._append(self.quality_identifier,
                             pa.Table.from_pylist(self._quality, schema=_quality_arrow_schema()))
                self._quality.clear()
            if self._audit:
                self._append(self.audit_identifier,
                             pa.Table.from_pylist(self._audit, schema=_audit_arrow_schema()))
                self._audit.clear()

    def _append(self, identifier: str, rows: Any) -> None:
        from pyiceberg.exceptions import CommitFailedException

        table = self._load_or_create(identifier, rows.schema)
        for attempt in range(6):
            try:
                if attempt == 0 and not set(rows.schema.names) <= set(table.schema().column_names):
                    with table.update_schema() as update:
                        update.union_by_name(rows.schema)
                table.append(rows, snapshot_properties={"ldp.system": "events"})
                logger.debug("Appended %d rows to %s", rows.num_rows, identifier)
                return
            except CommitFailedException:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2 ** attempt) * (1 + secrets.randbelow(100) / 100))
                table = self.catalog.load_table(identifier)

    def _load_or_create(self, identifier: str, schema: Any) -> Any:
        from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError
        from pyiceberg.transforms import DayTransform

        catalog = self.catalog
        try:
            return catalog.load_table(identifier)
        except NoSuchTableError:
            pass
        catalog.create_namespace_if_not_exists(self.namespace)
        try:
            transaction = catalog.create_table_transaction(identifier, schema=schema,
                                                           properties=dict(_SYSTEM_TABLE_PROPERTIES))
            with transaction:
                with transaction.update_spec() as spec:
                    spec.add_field("ts", DayTransform(), "ts_day")
        except TableAlreadyExistsError:
            pass
        logger.info("Created system table %s", identifier)
        return catalog.load_table(identifier)


def _event_from_row(row: Mapping[str, Any]) -> RunEvent:
    return RunEvent.from_dict(row)


def read_iceberg_events(catalog: Any, *, namespace: str = SYSTEM_NAMESPACE, pipeline: str | None = None,
                        run_id: str | None = None, table: str | None = None) -> list[RunEvent]:
    """Read events back from ``<namespace>.runs``, oldest first.

    Args:
        catalog: A pyiceberg catalog, or a catalog spec for
            :func:`~local_data_platform.catalog.provider.create_catalog`.
        namespace: The system namespace.
        pipeline: Only events of this pipeline.
        run_id: Only events of this run.
        table: Only events whose target is this table identifier, e.g. ``"nyc.rides"``.

    Returns:
        The events, sorted by ``(ts, run_id, attempt, seq)``. Empty if the table doesn't exist.
    """
    from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError
    from pyiceberg.expressions import AlwaysTrue, And, EqualTo

    if isinstance(catalog, Mapping):
        from local_data_platform.catalog.provider import create_catalog

        catalog = create_catalog(catalog)
    try:
        runs = catalog.load_table(f"{namespace}.{RUNS_TABLE}")
    except (NoSuchTableError, NoSuchNamespaceError):
        return []
    row_filter: Any = AlwaysTrue()
    if pipeline is not None:
        row_filter = And(row_filter, EqualTo("pipeline", pipeline))
    if run_id is not None:
        row_filter = And(row_filter, EqualTo("run_id", run_id))
    if table is not None:
        row_filter = And(row_filter, EqualTo("table_identifier", table))
    rows = runs.scan(row_filter=row_filter).to_arrow().to_pylist()
    events = [_event_from_row(row) for row in rows]
    return sorted(events, key=lambda event: (event.ts, event.run_id, event.attempt, event.seq))


# ---------------------------------------------------------------------- run summaries


def _checks_text(payload: Mapping[str, Any]) -> str:
    run = int(payload.get("checks_run") or 0)
    return f"{run - int(payload.get('checks_failed') or 0)}/{run}"


def summarize_runs(events: Iterable[RunEvent]) -> list[dict[str, Any]]:
    """Fold events into one summary per ``(run_id, attempt)``, newest first.

    Returns:
        Dicts with ``run_id``, ``attempt``, ``pipeline``, ``table``, ``started_at``,
        ``status`` (``running`` until a terminal event is seen), ``rows_read``,
        ``rows_written``, ``snapshot_id``, ``idempotency_key``, ``duration_s`` and
        ``checks`` (``"passed/run"``).
    """
    runs: dict[tuple[str, int], dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: (item.run_id, item.attempt, item.seq)):
        run = runs.setdefault((event.run_id, event.attempt), {
            "run_id": event.run_id, "attempt": event.attempt, "pipeline": None, "table": None,
            "started_at": event.ts, "status": "running", "rows_read": None, "rows_written": None,
            "snapshot_id": None, "idempotency_key": None, "duration_s": None, "checks": None,
        })
        payload = event.payload
        run["pipeline"] = run["pipeline"] or payload.get("pipeline")
        run["table"] = run["table"] or payload.get("table")
        run["started_at"] = min(run["started_at"], event.ts)
        run["idempotency_key"] = run["idempotency_key"] or payload.get("idempotency_key")
        if event.type == "run.extracted":
            run["rows_read"] = payload.get("rows_read")
        elif event.type == "quality.evaluated":
            run["checks"] = _checks_text(payload)
        elif event.type in ("run.published", "run.skipped_duplicate", "run.blocked_quality", "run.failed"):
            run["status"] = event.type.split(".", 1)[1]
            run["snapshot_id"] = payload.get("snapshot_id", run["snapshot_id"])
            if "rows_written" in payload:
                run["rows_written"] = payload["rows_written"]
        elif event.type == "run.finished":
            run["status"] = payload.get("status") or run["status"]
            run["duration_s"] = payload.get("duration_s")
            for key in ("rows_read", "rows_written"):
                if payload.get(key) is not None:
                    run[key] = payload[key]
            if payload.get("published_snapshot_id") is not None:
                run["snapshot_id"] = payload["published_snapshot_id"]
    return sorted(runs.values(), key=lambda run: (run["started_at"], run["run_id"], run["attempt"]), reverse=True)


# ---------------------------------------------------------------------- emitting


class RunEmitter:
    """Builds the events of one run attempt and hands them to a sink.

    It numbers events (``seq``), stamps them, merges ``context`` (``pipeline`` and
    ``table``) into every payload, and never lets a sink error escape: failures are
    logged as warnings, because observability must not fail a data load.

    Args:
        sink: The sink.
        run_id: The run's id.
        attempt: The attempt number.
        context: Keys merged into every payload.
        clock: Returns the current time; ``datetime.now(UTC)`` by default.

    Raises:
        TypeError: If ``sink`` has no ``emit``/``flush`` methods.
    """

    def __init__(self, sink: EventSink | None, run_id: str, *, attempt: int = 1,
                 context: Mapping[str, Any] | None = None, clock: Callable[[], dt.datetime] | None = None):
        self.sink = sink if sink is not None else NullSink()
        _check_sink(self.sink)
        self.run_id = run_id
        self.attempt = attempt
        self.context = dict(context or {})
        self.clock = clock or (lambda: dt.datetime.now(_UTC))
        self.seq = 0
        self.table_uuid: str | None = None

    @property
    def enabled(self) -> bool:
        """``False`` for a :class:`NullSink`, so callers can skip building payloads."""
        return not isinstance(self.sink, NullSink)

    def emit(self, type_: str, payload: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None, *,
             table_uuid: str | None = None) -> RunEvent | None:
        """Emit one event.

        Args:
            type_: The event type.
            payload: The payload, or a callable returning it (only called when enabled).
            table_uuid: The target table's UUID, if known. It sticks for later events.

        Returns:
            The event, or ``None`` when disabled or when building it failed.
        """
        if not self.enabled:
            return None
        try:
            body = payload() if callable(payload) else (payload or {})
            if table_uuid:
                self.table_uuid = table_uuid
            event = RunEvent(event_id=uuid7(), type=type_, schema_version=SCHEMA_VERSION, ts=self.clock(),
                             run_id=self.run_id, attempt=self.attempt, seq=self.seq, table_uuid=self.table_uuid,
                             payload=_jsonable({**self.context, **dict(body)}))
            self.seq += 1
        except Exception as exc:  # noqa: BLE001 - never fail a run because of its events
            logger.warning("Could not build the %s event of run %s: %s", type_, self.run_id, redact_text(str(exc)))
            return None
        try:
            self.sink.emit(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Event sink %r failed to record %s: %s", self.sink, type_, redact_text(str(exc)))
        return event

    def flush(self) -> None:
        """Flush the sink, logging any error as a warning."""
        try:
            self.sink.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Event sink %r failed to flush: %s", self.sink, redact_text(str(exc)))


# ---------------------------------------------------------------------- config


_SINK_OPTIONS = {
    "null": set(),
    "jsonl": {"path"},
    "openlineage": {"path", "url", "namespace", "api_key_env", "timeout"},
    "iceberg": {"catalog", "namespace", "batch_size"},
}
_OBSERVABILITY_KEYS = {"sinks"} | set(_SINK_OPTIONS)


def _iceberg_catalog_block(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for section in ("target", "source"):
        block = metadata.get(section)
        if isinstance(block, Mapping) and str(block.get("format", "")).strip().upper() == "ICEBERG":
            catalog = block.get("catalog")
            if isinstance(catalog, Mapping):
                return catalog
    return None


def sink_specs(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse ``metadata.observability`` into a list of ``{"type", **options}``.

    ``sinks`` lists sink types (``"iceberg"``, ``"jsonl"``, ``"openlineage"``,
    ``"null"``) or objects with a ``type`` and options. Options can also sit in a
    block named after the type, e.g. ``"jsonl": {"path": "logs/events.jsonl"}``;
    options in the list item win. The ``iceberg`` sink's catalog defaults to the
    target's (else the source's) Iceberg catalog.

    Returns:
        The sink specs, in order. Empty when there is no ``observability`` block.

    Raises:
        ConfigError: If the block is malformed or names an unknown sink or option.
    """
    raw = metadata.get("observability") if isinstance(metadata, Mapping) else None
    if raw is None:
        return []
    where = "metadata.observability"
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be an object such as {{'sinks': ['jsonl']}}")
    unknown = sorted(set(raw) - _OBSERVABILITY_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown keys {unknown}; allowed: {sorted(_OBSERVABILITY_KEYS)}")
    items = raw.get("sinks", [])
    if isinstance(items, (str, Mapping)):
        items = [items]
    if not isinstance(items, (list, tuple)):
        raise ConfigError(f"{where}.sinks must be a list of sink types, got {type(items).__name__}")
    specs = []
    for position, item in enumerate(items):
        label = f"{where}.sinks[{position}]"
        options = dict(item) if isinstance(item, Mapping) else {"type": item}
        kind = options.pop("type", None)
        kind = kind.strip().lower() if isinstance(kind, str) else kind
        if kind == "none":
            kind = "null"
        if not isinstance(kind, str) or kind not in _SINK_OPTIONS:
            raise ConfigError(f"{label} has unknown sink type {kind!r}; expected one of {list(SINK_TYPES)}")
        shared = raw.get(kind) or {}
        if not isinstance(shared, Mapping):
            raise ConfigError(f"{where}.{kind} must be an object of sink options")
        merged = {**dict(shared), **options}
        bad = sorted(set(merged) - _SINK_OPTIONS[kind])
        if bad:
            raise ConfigError(f"{label} ({kind}) has unknown options {bad}; allowed: {sorted(_SINK_OPTIONS[kind])}")
        if kind == "openlineage" and merged.get("path") and merged.get("url"):
            raise ConfigError(f"{label} (openlineage) takes a 'path' or a 'url', not both")
        if kind == "iceberg" and merged.get("catalog") is None:
            catalog = _iceberg_catalog_block(metadata)
            if catalog is None:
                raise ConfigError(f"{label} (iceberg) needs a 'catalog': neither the source nor the target is an "
                                  "Iceberg table")
            merged["catalog"] = dict(catalog)
        specs.append({"type": kind, **merged})
    return specs


def build_sink(spec: Mapping[str, Any], base_dir: str | os.PathLike | None = None) -> EventSink:
    """Build one sink from a spec returned by :func:`sink_specs`. Nothing is opened or written yet."""
    kind = spec["type"]
    if kind == "null":
        return NullSink()
    if kind == "jsonl":
        return JsonlSink(spec.get("path") or DEFAULT_JSONL_PATH, base_dir=base_dir)
    if kind == "openlineage":
        target = spec.get("url") or spec.get("path") or DEFAULT_OPENLINEAGE_PATH
        return OpenLineageSink(target, namespace=spec.get("namespace") or "ldp", api_key_env=spec.get("api_key_env"),
                               timeout=float(spec.get("timeout") or 10.0), base_dir=base_dir)
    if kind == "iceberg":
        return IcebergSink(spec["catalog"], base_dir, namespace=spec.get("namespace") or SYSTEM_NAMESPACE,
                           batch_size=int(spec.get("batch_size") or 500))
    raise ConfigError(f"unknown sink type {kind!r}")  # pragma: no cover - sink_specs rejects it first


def sink_from_config(config: Any) -> EventSink:
    """Build the default sink of a :class:`~local_data_platform.Config` from ``metadata.observability``.

    Returns:
        A :class:`NullSink` when the block is absent or lists no sinks, the one sink
        it lists, or a :class:`MultiSink`.

    Raises:
        ConfigError: If the block is invalid.
    """
    if config is None:
        return NullSink()
    sinks = [build_sink(spec, getattr(config, "base_dir", None)) for spec in sink_specs(config.metadata)]
    sinks = [sink for sink in sinks if not isinstance(sink, NullSink)]
    if not sinks:
        return NullSink()
    return sinks[0] if len(sinks) == 1 else MultiSink(sinks)


# ---------------------------------------------------------------------- CLI: ldp runs


def _local_catalog_missing(spec: Mapping[str, Any], base_dir: Path | None) -> bool:
    """Whether the catalog is a SQLite file (``local``, or ``sql`` on SQLite) that doesn't exist yet."""
    from local_data_platform.catalog.provider import catalog_database_file

    database = catalog_database_file(spec, base_dir)
    return database is not None and not database.is_file()


def load_run_events(config_or_path: str | os.PathLike, *, jsonl: str | os.PathLike | None = None,
                    run_id: str | None = None) -> tuple[list[RunEvent], str]:
    """Load the events ``ldp runs`` shows.

    Args:
        config_or_path: A dataset config file, or a ``.jsonl`` events file.
        jsonl: Read this events file instead of what the config names.
        run_id: Only this run's events.

    Returns:
        ``(events, where)``, where ``where`` describes the source read. With a config,
        the events are those of its pipeline: from ``_ldp.runs`` when the config lists
        an ``iceberg`` sink or its catalog already has the table, else from its
        ``jsonl`` sink's file. Nothing is created while looking.
    """
    path = resolve_path(config_or_path)
    if jsonl is not None or path.suffix.lower() == ".jsonl":
        file = resolve_path(jsonl) if jsonl is not None else path
        if not file.is_file():
            return [], str(file)
        events = read_jsonl(file)
        return [e for e in events if run_id is None or e.run_id == run_id], str(file)

    from local_data_platform.etl import load_config

    config = load_config(path)
    specs = sink_specs(config.metadata)
    iceberg_specs = [spec for spec in specs if spec["type"] == "iceberg"]
    jsonl_specs = [spec for spec in specs if spec["type"] == "jsonl"]
    catalog_spec = iceberg_specs[0]["catalog"] if iceberg_specs else _iceberg_catalog_block(config.metadata)
    namespace = (iceberg_specs[0].get("namespace") if iceberg_specs else None) or SYSTEM_NAMESPACE
    if catalog_spec is not None and not _local_catalog_missing(catalog_spec, config.base_dir):
        from local_data_platform.catalog.provider import create_catalog

        catalog = create_catalog(catalog_spec, base_dir=config.base_dir)
        events = read_iceberg_events(catalog, namespace=namespace, pipeline=config.identifier, run_id=run_id)
        if events or iceberg_specs or not jsonl_specs:
            return events, f"{namespace}.{RUNS_TABLE}"
    file = config.resolve((jsonl_specs[0].get("path") if jsonl_specs else None) or DEFAULT_JSONL_PATH)
    if not file.is_file():
        return [], str(file)
    events = [e for e in read_jsonl(file) if e.payload.get("pipeline") in (None, config.identifier)]
    return [e for e in events if run_id is None or e.run_id == run_id], str(file)


def _short(value: Any, width: int = 12) -> Any:
    return value[:width] if isinstance(value, str) and len(value) > width else value


def _cmd_runs(args: Any) -> int:
    import sys

    from local_data_platform.cli import format_table

    events, where = load_run_events(args.config, jsonl=args.jsonl, run_id=args.run)
    if not events:
        print(f"No runs recorded in {where}. Add metadata.observability, e.g. "
              '{"sinks": ["iceberg", "jsonl"]}, and run the pipeline.', file=sys.stdout)
        return 0
    if args.run is not None or args.events:
        rows = [{"run_id": _short(e.run_id, 13), "attempt": e.attempt, "seq": e.seq,
                 "ts_utc": e.ts.replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds"), "type": e.type,
                 "detail": _event_detail(e)} for e in sorted(events, key=lambda x: (x.ts, x.run_id, x.seq))]
        print(format_table(rows[-args.limit:] if args.run is None else rows), file=sys.stdout)
        return 0
    rows = []
    for run in summarize_runs(events)[: args.limit]:
        rows.append({
            "run_id": run["run_id"], "attempt": run["attempt"], "pipeline": run["pipeline"],
            "started_utc": run["started_at"].replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),
            "status": run["status"], "rows_read": run["rows_read"], "rows_written": run["rows_written"],
            "checks": run["checks"], "snapshot_id": run["snapshot_id"],
            "idempotency_key": _short(run["idempotency_key"]), "duration_s": run["duration_s"],
        })
    print(f"Runs recorded in {where}, newest first:", file=sys.stdout)
    print(format_table(rows), file=sys.stdout)
    return 0


def _event_detail(event: RunEvent) -> str:
    payload = event.payload
    if event.type == "run.started":
        return f"mode={payload.get('mode')} publish={payload.get('publish')}"
    if event.type == "run.extracted":
        return f"rows_read={payload.get('rows_read')}"
    if event.type == "quality.evaluated":
        return f"checks {_checks_text(payload)}"
    if event.type in ("run.published", "run.skipped_duplicate"):
        return f"rows_written={payload.get('rows_written')} snapshot={payload.get('snapshot_id')}"
    if event.type == "run.blocked_quality":
        return "failed: " + ", ".join(payload.get("failures", []))
    if event.type == "run.failed":
        return f"{payload.get('stage')}: {payload.get('error_type')}: {_short(payload.get('message'), 80)}"
    if event.type == "run.finished":
        return f"status={payload.get('status')} duration_s={payload.get('duration_s')}"
    return ""


def _positive(text: str) -> int:
    import argparse

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a number of at least 1, got {value}")
    return value


def add_cli(subparsers: Any, parents: Sequence[Any] = ()) -> None:
    """Register ``ldp runs`` on an ``argparse`` subparsers object.

    Args:
        subparsers: What ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to inherit options from (e.g. the CLI's ``-v``).
    """
    runs = subparsers.add_parser(
        "runs", parents=list(parents), help="list recorded pipeline runs",
        description="List the runs recorded by a config's event sinks: _ldp.runs in its Iceberg catalog, or its "
                    "JSONL events file. CONFIG may also be a .jsonl events file.")
    runs.add_argument("config", metavar="CONFIG", help="a JSON dataset config, or a .jsonl events file")
    runs.add_argument("--jsonl", metavar="FILE", help="read this JSONL events file instead")
    runs.add_argument("--run", metavar="RUN_ID", help="show every event of one run")
    runs.add_argument("--events", action="store_true", help="show events instead of one line per run")
    runs.add_argument("--limit", type=_positive, default=20, help="show at most this many runs (default 20)")
    runs.set_defaults(handler=_cmd_runs)


__all__ = [
    "AUDIT_TABLE",
    "DEFAULT_JSONL_PATH",
    "DEFAULT_OPENLINEAGE_PATH",
    "EVENT_TYPES",
    "EventSink",
    "IcebergSink",
    "JsonlSink",
    "MemorySink",
    "MultiSink",
    "NullSink",
    "OPENLINEAGE_SCHEMA_URL",
    "OpenLineageSink",
    "QUALITY_TABLE",
    "RUNS_TABLE",
    "RUN_STATUSES",
    "RunEmitter",
    "RunEvent",
    "SCHEMA_VERSION",
    "SINK_TYPES",
    "SYSTEM_NAMESPACE",
    "add_cli",
    "build_sink",
    "dataset_ref",
    "event_json_schema",
    "load_run_events",
    "quality_payload",
    "read_iceberg_events",
    "read_jsonl",
    "read_openlineage",
    "redact_text",
    "schema_payload",
    "sink_from_config",
    "sink_specs",
    "summarize_runs",
    "uuid7",
]
