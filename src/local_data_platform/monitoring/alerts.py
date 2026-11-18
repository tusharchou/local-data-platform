"""The alerts of ``ldp monitor``: say it once when a monitor changes state, and say when it has recovered.

A monitor says ``ok``, ``warn``, ``error`` or ``unknown`` each time that ``ldp monitor`` runs, and one that stays in
``error`` for a day must not send a message every time it is looked at. So an alert goes out for a change of state and
not for a state. Each channel sees the monitor through its own ``on``. A channel that wants ``monitor.error`` sees a
monitor as alerting while it is in ``error``, and one that wants ``monitor.warn`` as well sees ``warn`` as alerting
too.

* A monitor that becomes alerting, or changes from one alerting status to another, sends the alert for its new status.
  A monitor that has never been looked at before counts as ``ok``, so a table that is broken at the first look alerts.
* A monitor that stops being alerting sends ``monitor.resolved``, if the channel wants it.
* A monitor that stays in the same alerting status sends nothing, unless the channel has ``repeat_after_minutes``. Then
  the alert is sent again each time that a multiple of that many minutes, counted from the first look that saw the
  state, falls between the look before and this one, so it is said at most once for each stretch of minutes, and again
  at the next look after a gap.
* ``unknown`` neither raises an alert nor clears one. The previous state of a monitor is the last of its results that
  is not ``unknown``.

The previous state comes from the history of the monitors, which :mod:`local_data_platform.monitoring.history` reads,
and there is no file of alert state. The results of the look that is being made are recorded after the alerts are
decided and before they are sent, so a run that fails to record sends nothing and says it again at the next look, and a
channel that is down loses the one alert of that change, which ``repeat_after_minutes`` brings back.

``monitor.heartbeat`` is a ping after each completed look at a config, so a dead man's switch outside, such as
Healthchecks.io, can raise an alarm when the scheduler stops.

This module needs only the standard library and :mod:`local_data_platform.notify` when it is imported.
"""

import dataclasses
import datetime as dt
from collections.abc import Sequence

from local_data_platform.monitoring.results import MonitorResult
from local_data_platform.notify import Alert, Channel, Delivery, Notifier

KNOWN = ("ok", "warn", "error")
"""The statuses that say how a monitor is. ``unknown`` says that it could not tell."""

_ALERTING = (("warn", "monitor.warn"), ("error", "monitor.error"))


@dataclasses.dataclass(frozen=True)
class Before:
    """What the history says about a monitor before the look that is being made.

    Attributes:
        status: The last status that was not ``unknown``, or ``None`` for a monitor that was never looked at.
        entered: The time of the first look of the stretch of looks that have that status, ``unknown`` ones left out.
        last: The time of the last look, of any status.
    """

    status: str | None = None
    entered: dt.datetime | None = None
    last: dt.datetime | None = None


def before(history: Sequence[MonitorResult], monitor: str, now: dt.datetime) -> Before:
    """The state of a monitor as the history leaves it, from the results made at or before ``now``.

    Args:
        history: The results of the monitors of a config, which ``monitoring.history.read_results`` returns.
        monitor: The name of the monitor.
        now: The moment of the look that is being made.
    """
    mine = sorted((result for result in history if result.monitor == monitor and result.evaluated_at <= now),
                  key=lambda result: result.evaluated_at)
    last = mine[-1].evaluated_at if mine else None
    known = [result for result in mine if result.status in KNOWN]
    if not known:
        return Before(None, None, last)
    status, entered = known[-1].status, known[-1].evaluated_at
    for result in reversed(known[:-1]):
        if result.status != status:
            break
        entered = result.evaluated_at
    return Before(status, entered, last)


def repeat_due(entered: dt.datetime, last: dt.datetime, now: dt.datetime, minutes: int) -> bool:
    """Whether a multiple of ``minutes`` since ``entered`` falls after ``last`` and by ``now``."""
    period = minutes * 60
    return (now - entered).total_seconds() // period > (last - entered).total_seconds() // period


def decide(channel: Channel, earlier: Before, status: str, now: dt.datetime) -> tuple[str, bool] | None:
    """What a channel sends for a monitor that was in ``earlier`` and is in ``status`` now.

    Returns:
        ``(event, repeat)``, where ``repeat`` says that it is a state that was said already, or ``None`` when the
        channel sends nothing.
    """
    if status not in KNOWN:
        return None
    alerting = tuple(name for name, event in _ALERTING if channel.wants(event))
    was = earlier.status if earlier.status in alerting else None
    is_now = status if status in alerting else None
    if is_now is not None and is_now != was:
        return f"monitor.{is_now}", False
    if is_now is None and was is not None and channel.wants("monitor.resolved"):
        return "monitor.resolved", False
    minutes = channel.repeat_after_minutes
    if (is_now is not None and minutes is not None and earlier.entered is not None and earlier.last is not None
            and repeat_due(earlier.entered, earlier.last, now, minutes)):
        return f"monitor.{is_now}", True
    return None


def plan_alerts(notifier: Notifier, results: Sequence[MonitorResult], history: Sequence[MonitorResult],
                now: dt.datetime) -> list[tuple[Channel, Alert]]:
    """The alerts that the results of a look call for, with the channel of each, and nothing is sent.

    Args:
        notifier: The channels of the config.
        results: What the monitors said at ``now``.
        history: The results of the earlier looks, read before the results are recorded.
        now: The moment of the look.
    """
    planned: list[tuple[Channel, Alert]] = []
    for result in results:
        earlier = before(history, result.monitor, now)
        for channel in notifier.channels:
            decided = decide(channel, earlier, result.status, now)
            if decided is not None:
                event, repeat = decided
                planned.append((channel, notifier.monitor_alert(result, event, earlier.status, repeat)))
    return planned


@dataclasses.dataclass(frozen=True)
class Sent:
    """What went out after a look at a config.

    Attributes:
        alerts: What happened to each alert that a change of state called for.
        heartbeats: What happened to the ping of each channel that wants ``monitor.heartbeat``.
    """

    alerts: tuple[Delivery, ...] = ()
    heartbeats: tuple[Delivery, ...] = ()


def send_alerts(notifier: Notifier, planned: Sequence[tuple[Channel, Alert]], results: Sequence[MonitorResult],
                now: dt.datetime) -> Sent:
    """Send the alerts that :func:`plan_alerts` decided, then the heartbeat. A channel that fails is a warning."""
    alerts = tuple(notifier.send(channel, alert) for channel, alert in planned)
    heartbeats = tuple(notifier.broadcast(notifier.heartbeat_alert(results, now)))
    return Sent(alerts, heartbeats)


__all__ = [
    "Before",
    "KNOWN",
    "Sent",
    "before",
    "decide",
    "plan_alerts",
    "repeat_due",
    "send_alerts",
]
