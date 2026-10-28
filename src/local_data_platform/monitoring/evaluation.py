"""Evaluate the SLA of a config: every monitor that it asks for, at one moment.

:func:`evaluate` reads the ``metadata.sla`` block of a config and runs one monitor for each section that it has, and
returns what they found. It reads table metadata and nothing else. It does not record the results, which
:func:`~local_data_platform.monitoring.history.record` does, so it can be called as often as a person wants.

This module needs only the standard library when it is imported. The monitors import what they need when they run.
"""

import datetime as dt
from typing import Any

from local_data_platform.monitoring.results import MonitorResult


def _moment(now: dt.datetime | str | None) -> dt.datetime:
    """``now`` as a moment in UTC, or the current time when it is ``None``."""
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    from local_data_platform.lake import parse_moment

    return parse_moment(now)


def evaluate(config: Any, now: dt.datetime | str | None = None) -> list[MonitorResult]:
    """Run the monitors that the SLA of ``config`` asks for.

    Args:
        config: A :class:`~local_data_platform.Config`.
        now: The moment to measure at, as a ``datetime`` or as ISO 8601 text, where a time with no offset is UTC. The
            current time by default.

    Returns:
        One :class:`~local_data_platform.monitoring.results.MonitorResult` for each monitor, in the order freshness,
        and empty when the config has no SLA, or an SLA with no section that a monitor runs for.

    Raises:
        ConfigError: If ``now`` is not a time.
    """
    sla = config.sla
    if sla is None:
        return []
    moment = _moment(now)
    results: list[MonitorResult] = []
    if sla.freshness is not None:
        from local_data_platform.monitoring.freshness import evaluate_freshness
        from local_data_platform.monitoring.target import load_target

        results.append(evaluate_freshness(sla.freshness, load_target(config), moment))
    return results


__all__ = [
    "evaluate",
]
