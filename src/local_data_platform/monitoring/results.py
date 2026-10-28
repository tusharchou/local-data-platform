"""The result of one monitor: what it saw, what it expected and whether that is fine.

A monitor checks one rule of the SLA of a table, its ``metadata.sla``, at one moment, and says what it found as a
:class:`MonitorResult`::

    MonitorResult(monitor="freshness", kind="freshness", status="error", evaluated_at=now, pipeline="orders",
                  table_identifier="shop.orders", observed="30.1 hour", expected="error after 26 hour",
                  details="the last commit was 30.1 hour ago, at 2026-10-27T10:00:00Z",
                  metrics={"age_seconds": 108360.0, "source": "snapshot"})

``status`` is one of :data:`STATUSES`. ``ok`` says that the rule holds and ``warn`` that it is broken in a way that
the SLA calls a warning. ``error`` says that it is broken in a way that the SLA calls an error, or that the monitor
cannot work because the table is not there. ``unknown`` says that the monitor could not tell, which is not a breach,
so it never raises an alert. ``observed`` and ``expected`` are short texts for a table of results, ``details`` is one
sentence, and ``metrics`` holds the numbers behind them. None of them holds rows of the table, only counts, times and
names.

This module imports nothing outside the standard library, so the SLA settings and the config can import the package.
"""

import datetime as dt
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
"""The version of :meth:`MonitorResult.to_dict`, which is what the history stores."""

STATUSES = ("ok", "warn", "error", "unknown")
"""What a monitor can say about a rule."""

SEVERITY = {"ok": 0, "unknown": 1, "warn": 2, "error": 3}
"""How bad each status is, so that :func:`worst` can pick one."""

_UTC = dt.timezone.utc


def format_moment(moment: dt.datetime, *, seconds: bool = False) -> str:
    """A moment as ISO 8601 in UTC with a ``Z``, such as ``2026-10-27T10:00:00Z``.

    A part of a second is kept, because two results of one table can be a part of a second apart. With ``seconds`` it is
    dropped, which is what a line of text for a person wants.
    """
    moment = moment.astimezone(_UTC)
    return (moment.replace(microsecond=0) if seconds else moment).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class MonitorResult:
    """What one monitor found about one table at one moment.

    Attributes:
        monitor: The name of the monitor, such as ``freshness``. It is stable, so the history of a monitor can be told
            from the history of another one of the same table.
        kind: The family of the monitor, such as ``freshness``.
        status: One of :data:`STATUSES`.
        evaluated_at: The moment that the monitor was asked about, with a time zone.
        pipeline: The identifier of the config that the SLA is in.
        table_identifier: The table, such as ``shop.orders``.
        table_uuid: The UUID of the table, or ``None`` when it does not exist yet.
        observed: What the monitor saw, in a few words.
        expected: What the rule asks for, in a few words.
        details: One sentence that says what happened.
        metrics: Numbers and names behind the result. Never rows of the table.
    """

    monitor: str
    kind: str
    status: str
    evaluated_at: dt.datetime
    pipeline: str | None = None
    table_identifier: str | None = None
    table_uuid: str | None = None
    observed: str | None = None
    expected: str | None = None
    details: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {list(STATUSES)}, got {self.status!r}")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at needs a time zone")

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives, with ``evaluated_at`` as ISO 8601 in UTC."""
        return {"monitor": self.monitor, "kind": self.kind, "status": self.status, "observed": self.observed,
                "expected": self.expected, "details": self.details, "metrics": dict(self.metrics),
                "evaluated_at": format_moment(self.evaluated_at), "pipeline": self.pipeline,
                "table_identifier": self.table_identifier, "table_uuid": self.table_uuid,
                "schema_version": SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MonitorResult":
        """Read a result back from :meth:`to_dict`, or from a row of ``_ldp.monitor_results``.

        ``evaluated_at`` may be the ``ts`` of a row, as text or as a ``datetime``, and ``metrics`` may be JSON text.
        Keys that the result does not have are ignored, as a reader of a stored history has to ignore the keys of a
        newer one.

        Raises:
            ValueError: If the data is not a result.
        """
        moment = data.get("evaluated_at", data.get("ts"))
        if isinstance(moment, str):
            moment = dt.datetime.fromisoformat(moment)
        if not isinstance(moment, dt.datetime):
            raise ValueError(f"a monitor result needs an evaluated_at moment, got {moment!r}")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_UTC)
        metrics = data.get("metrics") or {}
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        if not isinstance(metrics, Mapping):
            raise ValueError(f"metrics must be an object, got {metrics!r}")
        return cls(monitor=str(data["monitor"]), kind=str(data["kind"]), status=str(data["status"]),
                   evaluated_at=moment, pipeline=data.get("pipeline"), table_identifier=data.get("table_identifier"),
                   table_uuid=data.get("table_uuid"), observed=data.get("observed"), expected=data.get("expected"),
                   details=data.get("details") or "", metrics=dict(metrics))


def worst(results: Iterable[MonitorResult]) -> str | None:
    """The status of the worst result, or ``None`` when there are none. ``error`` is worse than ``warn``."""
    statuses = [result.status for result in results]
    return max(statuses, key=SEVERITY.__getitem__) if statuses else None


__all__ = [
    "MonitorResult",
    "SCHEMA_VERSION",
    "SEVERITY",
    "STATUSES",
    "format_moment",
    "worst",
]
