"""The SLA verdict of a table, which ``describe_table`` gives an agent.

An agent that is asked whether a table is up to date should say what the SLA says, and not guess from a timestamp. The
verdict of a table is made of what the server can see.

* **The SLA of the table**, when the server was started with the config that writes it. It is the ``metadata.sla`` block
  with its defaults, and the same SLA in a line of words. A table that came from a catalog spec, or that a config only
  reads, has none.
* **The latest result of each monitor** in ``_ldp.monitor_results`` of the catalog, which ``ldp monitor`` records. A row
  is about the table when its ``table_uuid`` or its ``table_identifier`` is the table's, as a row of ``_ldp.runs`` is.
* **Freshness worked out now** from the metadata of the table, when its SLA has a freshness rule and no freshness result
  is stored. It is the answer that ``ldp monitor`` would give, and a result that nothing has recorded.

The server only reads. It records no result and it sends no alert, and a table whose stored results cannot be read still
gets its freshness and a line that says what went wrong.

This module needs only the standard library and the code that ``describe_table`` already imports.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from local_data_platform.events import MONITOR_TABLE
from local_data_platform.gold.status import ldp_rows
from local_data_platform.logger import get_logger
from local_data_platform.monitoring.results import MonitorResult, format_moment, worst
from local_data_platform.monitoring.target import Target

from .discovery import ExposedTable

logger = get_logger(__name__)

_ORDER = {"freshness": 0, "volume": 1, "schema": 2}
"""The order of the results, the same as the order that ``ldp monitor`` prints them in. The metrics come last, and so
does a kind that this server does not know."""


def stored_results(catalog: Any, table_uuid: str, identifier: str) -> list[MonitorResult]:
    """The latest result of each monitor of a table in ``_ldp.monitor_results``, oldest first.

    Args:
        catalog: The pyiceberg catalog of the table.
        table_uuid: The UUID of the table.
        identifier: ``<namespace>.<name>``, which the rows of a table that had no UUID yet carry.

    Returns:
        The results. Nothing when the table is not there or has no row of this table. A row that is not a result is
        left out with a warning.
    """
    latest: dict[str, MonitorResult] = {}
    results = []
    for row in ldp_rows(catalog, MONITOR_TABLE, table_uuid, identifier) or []:
        try:
            results.append(MonitorResult.from_dict(row))
        except ValueError as error:
            logger.warning("Skipping a row of _ldp.%s that is not a monitor result: %s", MONITOR_TABLE, error)
    for result in sorted(results, key=lambda item: item.evaluated_at):
        latest[result.monitor] = result
    return sorted(latest.values(), key=lambda item: item.evaluated_at)


def _freshness(exposed: ExposedTable, iceberg_table: Any, moment: dt.datetime) -> MonitorResult:
    from local_data_platform.monitoring.freshness import evaluate_freshness

    target = Target(pipeline=exposed.pipeline or exposed.identifier, identifier=exposed.identifier, iceberg=True,
                    table=iceberg_table)
    return evaluate_freshness(exposed.sla.freshness, target, moment)


def _entry(result: MonitorResult, *, recorded: bool) -> dict[str, Any]:
    return {"monitor": result.monitor, "kind": result.kind, "status": result.status, "observed": result.observed,
            "expected": result.expected, "details": result.details,
            "evaluated_at": format_moment(result.evaluated_at, seconds=True), "recorded": recorded}


def _message(defined: bool, entries: list[dict[str, Any]], status: str | None) -> str:
    if not entries:
        if not defined:
            return "This table has no SLA, so there is no verdict."
        return ("The SLA of this table has no stored result, and the server works out only its freshness, so there "
                "is no verdict yet. ldp monitor records a result for each monitor.")
    shown = next(entry for entry in entries if entry["status"] == status)
    return f"{status}: {shown['details'] or shown['observed'] or shown['monitor']}"


def sla_status(exposed: ExposedTable, iceberg_table: Any, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """What the SLA says about a table, for an agent. It never raises, records nothing and sends nothing.

    Args:
        exposed: The table, with the SLA of the config that writes it when there is one.
        iceberg_table: The pyiceberg table, freshly loaded.
        now: The moment to work freshness out at, with a time zone. The current time by default.

    Returns:
        An object with ``defined`` (whether the server knows an SLA for the table, from its config or from stored
        results), ``pipeline`` (the config that writes it), ``summary`` and ``settings`` (the SLA of the config, in
        words and as the block with its defaults), ``status`` (the worst of the results, or ``None``),
        ``last_evaluated_at`` and ``evaluated_age_seconds`` (when the newest result was evaluated and how long ago that
        was), ``source``
        (``recorded`` when a stored result is in ``results``, ``computed`` when only freshness worked out now is, and
        ``none``), ``message`` (the verdict in a sentence) and ``results``. Each result has its ``monitor``, ``kind``,
        ``status``, ``observed``, ``expected``, ``details``, ``evaluated_at`` and ``recorded``, which is false for
        freshness that was worked out now. ``error`` is there only when the stored results could not be read, or
        freshness could not be worked out, and it says which.
    """
    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    sla = exposed.sla
    problems: list[str] = []
    try:
        stored = stored_results(exposed.catalog.catalog, str(iceberg_table.metadata.table_uuid), exposed.identifier)
    except Exception as error:  # noqa: BLE001 - describe the table even if _ldp cannot be read
        logger.warning("Could not read the monitor results of %s: %s", exposed.identifier, error)
        stored = []
        problems.append(f"the stored results could not be read ({type(error).__name__})")
    computed: list[MonitorResult] = []
    if sla is not None and sla.freshness is not None and not any(result.monitor == "freshness" for result in stored):
        try:
            computed.append(_freshness(exposed, iceberg_table, moment))
        except Exception as error:  # noqa: BLE001 - a verdict that cannot be worked out is no verdict
            logger.warning("Could not work out the freshness of %s: %s", exposed.identifier, error)
            problems.append(f"freshness could not be worked out ({type(error).__name__})")
    entries = [*(_entry(result, recorded=True) for result in stored),
               *(_entry(result, recorded=False) for result in computed)]
    entries.sort(key=lambda entry: (_ORDER.get(entry["kind"], len(_ORDER)), entry["monitor"]))
    seen = [*stored, *computed]
    status = worst(seen)
    last = max((result.evaluated_at for result in seen), default=None)
    defined = sla is not None or bool(stored)
    verdict: dict[str, Any] = {
        "defined": defined, "pipeline": exposed.pipeline, "summary": sla.summary() if sla is not None else None,
        "settings": sla.to_dict() if sla is not None else None, "status": status,
        "last_evaluated_at": format_moment(last, seconds=True) if last is not None else None,
        "evaluated_age_seconds": None if last is None else round(max((moment - last).total_seconds(), 0.0), 3),
        "source": "recorded" if stored else "computed" if computed else "none",
        "message": _message(defined, entries, status), "results": entries}
    if problems:
        verdict["error"] = " and ".join(problems)
    return verdict


__all__ = [
    "sla_status",
    "stored_results",
]
