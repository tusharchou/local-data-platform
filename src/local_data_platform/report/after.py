"""``metadata.after``: the pipelines that have to have a published run for the window before a report is sent.

A report that goes out at eight in the morning has to wait for the pipelines that load its tables. ``metadata.after``
lists their identifiers. Before a send runs any query, :func:`waiting_for` asks, for each one, whether a run of it for
the report's window published or found the window already published, from the run events of the pipeline, where
``ldp status`` reads them. A pipeline that has no such run holds the report back with
:class:`~local_data_platform.report.health.ReportHeld`, which ``ldp report send`` turns into exit status 3, and the
report is not run, so nothing is sent. The window has no delivery yet, so the next ``ldp schedule tick`` runs it again,
and the report goes out once the pipelines have published.

* A run counts when its window is the window of the report, to the instant. The windows of ``ldp schedule tick`` are
  the windows of the pipeline tasks of a workflow, so a report whose workflow counts its days in the same time zone
  as the workflow of the pipelines finds them. A run that has no window, such as one that ``ldp run`` made without
  ``--window``, does not count.
* A run counts when it ended ``published`` or ``skipped_duplicate``, which is a run that found the window already
  published. Any run of the window that did, however many came before or after it, is enough.
* The runs are read from the sinks of the pipeline, its ``_ldp.runs`` table or its events file. A pipeline whose
  ``metadata.observability`` has no ``iceberg`` and no ``jsonl`` sink records no run that can be read, so the report
  waits for it for ever, and the reason says so.
* A pipeline that no config in the ``metadata.tables.config`` folders of the report has is a mistake in the report, and
  it is a :class:`~local_data_platform.exceptions.ConfigError`, which is not a hold.

Nothing is written. The check only reads the events, as ``ldp status`` does.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from local_data_platform.events import redact_text, run_source, summarize_runs
from local_data_platform.exceptions import ConfigError
from local_data_platform.monitoring.status import SUCCESS
from local_data_platform.report.health import Waiting, config_index

if TYPE_CHECKING:
    from local_data_platform.events import RunEvent
    from local_data_platform.report.spec import Report

NO_SINK = "no run of it is recorded, so its config needs an iceberg or a jsonl sink in metadata.observability"
NOT_RUN = "it has not run for the window"
RUNNING = "it is still running for the window"


def window_of(payload: Mapping[str, Any]) -> tuple[dt.datetime, dt.datetime] | None:
    """The window that a ``run.started`` payload says, as two datetimes, or ``None`` when it has none or is no time."""
    span = payload.get("logical_window")
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    try:
        return dt.datetime.fromisoformat(str(span[0])), dt.datetime.fromisoformat(str(span[1]))
    except ValueError:
        return None


def runs_for_window(events: Iterable[RunEvent], bounds: tuple[dt.datetime, dt.datetime]) -> list[dict[str, Any]]:
    """The runs of a pipeline whose window is ``bounds``, newest first.

    A run is a run of the window when an event of it says that window, as the ``run.started`` of a pipeline does with
    its ``logical_window``. The events of a workflow say their window as ``window``, so they are not runs of a pipeline,
    also when they are in the same file. The runs are folded as :func:`~local_data_platform.events.summarize_runs`
    folds the events of runs, so a run that was retried is a row for each attempt.
    """
    found = list(events)
    wanted = {event.run_id for event in found if window_of(event.payload) == bounds}
    return [run for run in summarize_runs(found) if run["run_id"] in wanted]


def _records_runs(config: Any) -> bool:
    """Whether the config has a sink that keeps its run events where ``ldp status`` and this check read them.

    That is an ``iceberg`` sink, which keeps them in ``_ldp.runs``, or a ``jsonl`` sink, which keeps them in a file. A
    pipeline with no ``metadata.observability``, or with only a ``null`` or an ``openlineage`` sink, never writes a run
    there, whatever an earlier configuration of it left behind.
    """
    source = run_source(config)
    return source.iceberg or source.jsonl_listed


def _reason(runs: list[dict[str, Any]], config: Any) -> str:
    if runs:
        status = runs[0]["status"]
        return RUNNING if status == "running" else f"its last run for the window ended {status}"
    return NOT_RUN if _records_runs(config) else NO_SINK


def waiting_for(report: Report, bounds: tuple[dt.datetime, dt.datetime]) -> list[Waiting]:
    """The pipelines of ``metadata.after`` that have no published run for the window, each with why.

    Args:
        report: The report, whose ``metadata.after`` lists the pipelines and whose ``metadata.tables.config`` folders
            hold their configs.
        bounds: The window of the report as UTC ``(start, end)``.

    Returns:
        A :class:`~local_data_platform.report.health.Waiting` for each pipeline that has not published the window, in
        the order of ``metadata.after``. It is empty when every pipeline has, and for a report that has no ``after``.

    Raises:
        ConfigError: If a pipeline of ``metadata.after`` has no config in the folders of ``metadata.tables.config``.
    """
    if not report.after:
        return []
    from local_data_platform.monitoring.ledger import Ledger

    index = config_index(report)
    missing = [name for name in report.after if name not in index]
    if missing:
        raise ConfigError(f"metadata.after of the report {report.identifier} names {', '.join(missing)}, which no "
                          "config in metadata.tables.config has")
    ledger = Ledger([index[name][1] for name in report.after])
    waiting: list[Waiting] = []
    for name in report.after:
        try:
            events = list(ledger.run_events(index[name][1]))
        except Exception as error:  # noqa: BLE001 - runs that cannot be read are runs that cannot be shown to be there
            problem = redact_text(f"{type(error).__name__}: {error}", 200)
            waiting.append(Waiting(name, f"its runs could not be read ({problem})"))
            continue
        runs = runs_for_window(events, bounds)
        if not any(run["status"] in SUCCESS for run in runs):
            waiting.append(Waiting(name, _reason(runs, index[name][1])))
    return waiting


__all__ = ["NOT_RUN", "NO_SINK", "RUNNING", "runs_for_window", "waiting_for", "window_of"]
