"""The schema monitor: whether the columns of a table, or of its source, changed since the last look.

It compares two things. The columns of the table now are compared with the columns that the previous evaluation of
this monitor kept in its result, a name and a type for each column and a fingerprint of the whole schema, so the
history is the baseline and there is no file to keep. The first evaluation has nothing to compare with, so it reports
``ok`` and records that baseline. The columns of the source in the latest ``run.extracted`` event are compared with
those of the run before it, so that a source that changed in a way that made the run fail is reported too, though the
table did not change. A report of the source stays until a later run extracts its columns.

A change is an added column, a removed column, a type that became wider (``int`` to ``long``, ``float`` to ``double``,
a ``decimal`` with more digits), a type that became another one, or something else about the schema that the
fingerprint sees, such as the nullability or the order of the columns. A change that ``allow`` lists, which can be
``added_column`` or ``widened_type``, stays ``ok``. Any other change is what ``on_change`` says, ``ignore``, ``warn`` or
``error``. A table that does not exist yet has no schema, so there is nothing to compare, and the result is ``unknown``.

This module needs only the standard library when it is imported.
"""

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from local_data_platform.monitoring.results import MonitorResult
from local_data_platform.monitoring.sla import SchemaDrift
from local_data_platform.monitoring.target import Target

MONITOR = "schema"
"""The name and the kind of the schema monitor."""

ADDED = "added_column"
REMOVED = "removed_column"
WIDENED = "widened_type"
CHANGED = "changed_type"
OTHER = "other"
"""The kinds of change. ``added_column`` and ``widened_type`` are the ones that ``allow`` can list."""

STATUS_OF = {"ignore": "ok", "warn": "warn", "error": "error"}
"""The status of a change that is not allowed, for each ``on_change``."""

_WIDER = {
    "int": {"long"}, "float": {"double"}, "halffloat": {"float", "double"},
    "int8": {"int16", "int32", "int64"}, "int16": {"int32", "int64"}, "int32": {"int64"},
    "uint8": {"uint16", "uint32", "uint64"}, "uint16": {"uint32", "uint64"}, "uint32": {"uint64"},
}
"""The types that a type can become and stay a wider version of itself, in the names of Iceberg and of Arrow."""

_DECIMAL = re.compile(r"^decimal(?:128|256)?\((\d+),\s*(\d+)\)$")


def widens(old: str, new: str) -> bool:
    """Whether ``new`` is a wider version of ``old``, as ``int`` is of ``long`` and a decimal is with more digits."""
    if new in _WIDER.get(old, ()):
        return True
    before, after = _DECIMAL.match(old), _DECIMAL.match(new)
    return bool(before and after and before.group(2) == after.group(2) and int(after.group(1)) > int(before.group(1)))


@dataclass(frozen=True)
class Change:
    """One change of a schema.

    Attributes:
        where: ``table`` for the columns of the table, ``source`` for the columns that the source gave a run.
        kind: One of ``added_column``, ``removed_column``, ``widened_type``, ``changed_type`` and ``other``.
        column: The column, or ``None`` for a change that is not about one column.
        old: Its type before, or ``None``.
        new: Its type now, or ``None``.
        allowed: Whether ``allow`` lists this kind of change, so that it stays ``ok``.
    """

    where: str
    kind: str
    column: str | None = None
    old: str | None = None
    new: str | None = None
    allowed: bool = False

    def sentence(self) -> str:
        """The change in a few words."""
        place = "the table" if self.where == "table" else "the source"
        later = "" if self.where == "table" else " in its latest run"
        if self.kind == ADDED:
            return f"{place} gained the column {self.column} ({self.new}){later}"
        if self.kind == REMOVED:
            return f"{place} lost the column {self.column}{later}"
        if self.kind == WIDENED:
            return f"the type of {self.column} in {place} became wider, from {self.old} to {self.new}{later}"
        if self.kind == CHANGED:
            return f"the type of {self.column} in {place} changed from {self.old} to {self.new}{later}"
        return "the schema of the table changed in something other than a column or a type, such as nullability"

    def to_dict(self) -> dict[str, Any]:
        values = {"where": self.where, "change": self.kind, "column": self.column, "from": self.old, "to": self.new,
                  "allowed": self.allowed}
        return {key: value for key, value in values.items() if value is not None}


def diff(where: str, old: Mapping[str, str], new: Mapping[str, str], allow: set[str]) -> list[Change]:
    """The changes from the columns ``old`` to the columns ``new``, each a name and a type.

    Added columns come first, in the order of ``new``, then removed columns and changed types, in the order of ``old``.
    """
    changes = [Change(where, ADDED, name, None, new[name], ADDED in allow) for name in new if name not in old]
    changes += [Change(where, REMOVED, name, old[name]) for name in old if name not in new]
    for name in old:
        if name in new and old[name] != new[name]:
            kind = WIDENED if widens(old[name], new[name]) else CHANGED
            changes.append(Change(where, kind, name, old[name], new[name], kind == WIDENED and WIDENED in allow))
    return changes


def _previous(config: Any, now: dt.datetime) -> Mapping[str, Any] | None:
    """The metrics of the latest evaluation of this monitor at or before ``now`` that kept the columns of the table."""
    from local_data_platform.monitoring.history import read_results

    for result in reversed(read_results(config, monitor=MONITOR)):
        if result.evaluated_at <= now and isinstance(result.metrics.get("columns"), Mapping):
            return result.metrics
    return None


def _source_columns(config: Any) -> tuple[dict[str, str], dict[str, str]] | None:
    """The columns of the source in the last two ``run.extracted`` events of the pipeline, or ``None`` with fewer."""
    from local_data_platform.events import run_events

    events, _ = run_events(config)
    extracted = sorted((event for event in events if event.type == "run.extracted"
                        and isinstance(event.payload.get("schema"), list)),
                       key=lambda event: (event.ts, event.run_id, event.attempt, event.seq))
    if len(extracted) < 2:
        return None
    return tuple({item["name"]: item["type"] for item in event.payload["schema"]} for event in extracted[-2:])


def evaluate_schema(rule: SchemaDrift, config: Any, target: Target, now: dt.datetime) -> MonitorResult:
    """Check the columns of the table and of its source for a change since the last look.

    Args:
        rule: The ``schema`` section.
        config: The :class:`~local_data_platform.Config`, whose history holds the baseline and the runs.
        target: The table, from :func:`~local_data_platform.monitoring.target.load_target`.
        now: The moment of the evaluation, with a time zone.

    Returns:
        One result. ``unknown`` when there is nothing to compare, because the table does not exist yet or is a file and
        the source has no two runs. ``ok`` for the baseline, for no change, and for changes that are allowed or that
        ``on_change`` ignores. Otherwise the status that ``on_change`` says.
    """
    from local_data_platform.datasets import schema_fingerprint

    allow = set(rule.allow)
    changes: list[Change] = []
    metrics: dict[str, Any] = {}
    baseline = False
    table = target.table
    if table is not None:
        current = {field.name: str(field.field_type) for field in table.schema().fields}
        fingerprint = schema_fingerprint(table.schema())
        metrics.update({"fingerprint": fingerprint, "columns": current})
        previous = _previous(config, now)
        if previous is None:
            baseline = True
        else:
            metrics["previous_fingerprint"] = previous.get("fingerprint")
            changes = diff("table", previous["columns"], current, allow)
            if not changes and previous.get("fingerprint") not in (None, fingerprint):
                changes = [Change("table", OTHER)]
    source = _source_columns(config)
    if source is not None:
        changes += diff("source", source[0], source[1], allow)

    def result(status: str, observed: str | None, details: str) -> MonitorResult:
        found = {**metrics, **({"baseline": True} if baseline else {}),
                 **({"changes": [change.to_dict() for change in changes]} if changes else {})}
        return MonitorResult(monitor=MONITOR, kind=MONITOR, status=status, evaluated_at=now, pipeline=target.pipeline,
                             table_identifier=target.identifier, table_uuid=target.table_uuid, observed=observed,
                             expected=rule.summary(), details=details, metrics=found)

    if table is None and source is None:
        if not target.iceberg:
            return result("unknown", None, target.why)
        metrics["reason"] = target.why
        return result("unknown", None, "the table does not exist yet, so it has no schema")
    blocking = [change for change in changes if not change.allowed]
    status = STATUS_OF[rule.on_change] if blocking else "ok"
    if not changes:
        columns = len(metrics.get("columns", ()))
        if table is None:
            details = "the source of its latest run has the same columns as the run before it"
        elif baseline:
            details = (f"baseline recorded for the {columns} columns of the table, so there is nothing to compare "
                       "them with yet")
        else:
            details = f"the {columns} columns of the table are as the last evaluation found them"
        return result("ok", "baseline" if baseline else "no change", details)
    words = []
    for change in changes:
        note = " (allowed)" if change.allowed else " (ignored)" if rule.on_change == "ignore" else ""
        words.append(change.sentence() + note)
    return result(status, f"{len(changes)} change" + ("" if len(changes) == 1 else "s"), ", ".join(words))


__all__ = [
    "ADDED",
    "CHANGED",
    "Change",
    "MONITOR",
    "OTHER",
    "REMOVED",
    "STATUS_OF",
    "WIDENED",
    "diff",
    "evaluate_schema",
    "widens",
]
