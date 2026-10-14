"""Windows: schedules, time zones, window shortcuts, path placeholders and the filters of a windowed run.

A *window* is the half-open interval ``[start, end)`` that one run is responsible for. A config says how its runs
are windowed in ``metadata``::

    "when": "daily",
    "metadata": {
        "schedule": "5 2 * * *",
        "window": {"size": "day", "column": "ordered_at", "timezone": "Europe/Paris"},
        "source": {"name": "orders", "format": "CSV", "path": "drops/orders_{window_start:%Y-%m-%d}.csv"},
        ...
    }

``schedule`` is a 5-field cron expression, or ``@hourly``, ``@daily``, ``@weekly`` or ``@monthly``. Without it, a
``when`` of ``hourly``, ``daily``, ``weekly`` or ``monthly`` means the matching alias, and any other ``when`` text is
a label that nothing reads.

``window`` is ``"none"``, or an object with a ``size`` (``day``, ``hour`` or ``15m``), an optional ``column`` and an
optional ``timezone`` (an IANA name, UTC by default). The time zone is the one the window's days are counted in: a
``yesterday`` window runs from local midnight to local midnight, the placeholders show local time, and a timestamp
column without a time zone is read as local time. A windowed run selects its rows in two ways, which a config can
combine:

* ``{window_start:FORMAT}`` and ``{window_end:FORMAT}`` in ``metadata.source.path``, where FORMAT is a Python
  ``strftime`` format, name the file or folder of the window, for example ``orders_{window_start:%Y-%m-%d}.csv``.
* ``window.column`` names a date or timestamp column, and the run keeps the rows whose value is at or after the start
  of the window and before its end. With the write mode ``overwrite`` the run then replaces only those rows of an
  Iceberg table, so re-running one day leaves every other day alone.

The window shortcuts ``yesterday``, ``last-week`` (the last full Monday-to-Monday week) and ``last-month`` stand for
a window that depends on today's date, and :func:`shortcut_window` computes them.

A range of windows, such as the days of a backfill, is the windows of one size laid end to end between two instants that
each start a window. :func:`iter_windows` lists them, :func:`is_aligned` says whether an instant starts one, and
:func:`window_size_of` says how long the windows of a config or a workflow are.

This module needs only the standard library when it is imported, so the spec and the config can use it. PyArrow and
pyiceberg are imported inside the functions that need them.
"""

import dataclasses
import datetime as dt
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from local_data_platform.exceptions import ConfigError

WINDOW_SIZES = ("day", "hour", "15m")
"""The values of ``metadata.window.size``."""

WINDOW_KEYS = ("size", "column", "timezone")
"""The keys of the ``metadata.window`` object."""

SHORTCUTS = ("yesterday", "last-week", "last-month")
"""Names that stand for a window computed from today's date, in the time zone of the config."""

SCHEDULE_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}
"""The aliases of ``metadata.schedule`` and the 5-field expressions they stand for."""

WHEN_SCHEDULES = {"hourly": "@hourly", "daily": "@daily", "weekly": "@weekly", "monthly": "@monthly"}
"""The ``when`` texts that mean a schedule when ``metadata.schedule`` is absent."""

PLACEHOLDER_NAMES = ("window_start", "window_end")
"""The names that a source path may use between braces."""

_UTC = dt.timezone.utc
_PLACEHOLDER = re.compile(r"\{(window_start|window_end)(?::([^{}]*))?\}")
_PLACEHOLDER_START = re.compile(r"\{window_(?:start|end)")


# ---------------------------------------------------------------------- time


def now_utc() -> dt.datetime:
    """The current time in UTC. Tests replace it with a fixed clock."""
    return dt.datetime.now(_UTC)


def get_timezone(name: Any = None) -> dt.tzinfo:
    """Return the time zone called ``name``: UTC for ``None`` and ``"UTC"``, else an IANA zone.

    Raises:
        ConfigError: If ``name`` is not the name of a time zone this machine knows. On Windows, an IANA name needs
            the ``tzdata`` package.
    """
    if name is None or name == "UTC":
        return _UTC
    if isinstance(name, dt.tzinfo):
        return name
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"time zone must be an IANA name such as 'Europe/Paris' or 'UTC', got {name!r}")
    try:
        return ZoneInfo(name.strip())
    except (ValueError, OSError, LookupError):
        raise ConfigError(f"unknown time zone {name!r}; use an IANA name such as 'Europe/Paris' or 'UTC' "
                          "(on Windows, pip install tzdata)") from None


def timezone_of(metadata: Any) -> dt.tzinfo:
    """The time zone of a config's ``metadata.window``, UTC when it names none."""
    window = metadata.get("window") if isinstance(metadata, Mapping) else None
    return get_timezone(window.get("timezone") if isinstance(window, Mapping) else None)


def is_shortcut(text: Any) -> bool:
    """Whether ``text`` is one of the window :data:`SHORTCUTS`."""
    return isinstance(text, str) and text.strip() in SHORTCUTS


def _midnight(day: dt.date, tz: dt.tzinfo) -> dt.datetime:
    # A midnight that a clock change skips reads as the moment the change happens, and one that happens twice reads
    # as its first time (fold 0), so a day starts when the local date changes.
    return dt.datetime(day.year, day.month, day.day, tzinfo=tz)


def shortcut_window(name: str, timezone: Any = None, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """Compute a window shortcut: the window that ends at the last local midnight before ``now``.

    ``yesterday`` is the local day before today. ``last-week`` is the last full week from Monday to Monday, so on a
    Monday it is the week that ended at that midnight. ``last-month`` is the calendar month before this one. The
    bounds are local midnights in ``timezone``, so a window across a clock change is 23 or 25 hours long.

    Args:
        name: One of :data:`SHORTCUTS`.
        timezone: A time zone name or ``tzinfo``; UTC by default.
        now: The current time, for tests. Naive values are read as UTC.

    Returns:
        ``(start, end)`` as aware UTC datetimes.

    Raises:
        ConfigError: If ``name`` is not a shortcut, or the time zone is unknown.
    """
    tz = get_timezone(timezone)
    if not is_shortcut(name):
        raise ConfigError(f"unknown window {name!r}; the shortcuts are {', '.join(SHORTCUTS)}")
    moment = now if now is not None else now_utc()
    moment = moment.replace(tzinfo=_UTC) if moment.tzinfo is None else moment
    today = moment.astimezone(tz).date()
    name = name.strip()
    if name == "yesterday":
        first, end = today - dt.timedelta(days=1), today
    elif name == "last-week":
        end = today - dt.timedelta(days=today.weekday())
        first = end - dt.timedelta(days=7)
    else:
        end = today.replace(day=1)
        first = (end - dt.timedelta(days=1)).replace(day=1)
    return _midnight(first, tz).astimezone(_UTC), _midnight(end, tz).astimezone(_UTC)


# ---------------------------------------------------------------------- ranges of windows

STEP_MINUTES = {"hour": 60, "15m": 15}
"""How long, in minutes, a window of each size is, but for a day, which is counted in the calendar of its zone."""


def parse_boundary(value: Any, timezone: Any = None, what: str = "boundary") -> dt.datetime:
    """Read one end of a range of windows as an aware UTC datetime.

    A date is midnight of that day in ``timezone``. A datetime, or ISO-8601 text, with no offset is read in
    ``timezone`` too, so ``2026-06-01`` starts the day that a ``Europe/Paris`` workflow calls June 1st. One with an
    offset, such as ``+02:00`` or ``Z``, is taken as written.

    Args:
        value: A ``date``, a ``datetime``, or ISO-8601 text.
        timezone: The time zone a value without an offset is read in: a name or a ``tzinfo``; UTC by default.
        what: What the value is, for the message of an error.

    Raises:
        ConfigError: If ``value`` is not a date, a datetime or ISO-8601 text, or the time zone is unknown.
    """
    tz = get_timezone(timezone)
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        return _midnight(value, tz).astimezone(_UTC)
    elif isinstance(value, str) and value.strip():
        try:
            moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ConfigError(f"{what} {value!r} is not an ISO-8601 date or datetime, such as 2026-06-01 or "
                              "2026-06-01T06:00:00+02:00") from None
    else:
        raise ConfigError(f"{what} must be a date, a datetime or ISO-8601 text, got {value!r}")
    return (moment.replace(tzinfo=tz) if moment.tzinfo is None else moment).astimezone(_UTC)


def is_aligned(moment: dt.datetime, size: str, timezone: Any = None) -> bool:
    """Whether ``moment`` starts a window of ``size`` in ``timezone``.

    A day starts at local midnight, an hour at the start of a local hour and a ``15m`` window at a quarter of one.
    """
    tz = get_timezone(timezone)
    moment = moment.astimezone(_UTC)
    local = moment.astimezone(tz)
    if size == "day":
        return _midnight(local.date(), tz).astimezone(_UTC) == moment
    return local.second == 0 and local.microsecond == 0 and local.minute % STEP_MINUTES[size] == 0


def floor_boundary(moment: dt.datetime, size: str, timezone: Any = None) -> dt.datetime:
    """The start of the window of ``size`` that ``moment`` falls in, as an aware UTC datetime."""
    tz = get_timezone(timezone)
    local = moment.astimezone(tz)
    if size == "day":
        return _midnight(local.date(), tz).astimezone(_UTC)
    return local.replace(minute=local.minute - local.minute % STEP_MINUTES[size], second=0,
                         microsecond=0).astimezone(_UTC)


def iter_windows(start: dt.datetime, end: dt.datetime, size: str,
                 timezone: Any = None) -> Iterator[tuple[dt.datetime, dt.datetime]]:
    """The windows of ``size`` from ``start`` to ``end``, oldest first, as ``(start, end)`` pairs in aware UTC.

    The windows are laid end to end and each one is whole, so the last window ends at ``end`` when ``end`` starts a
    window and before it when it does not. A day runs from one local midnight to the next, so across a clock change it
    is 23 or 25 hours long. An hour and a quarter of an hour are as long as they say.

    Args:
        start: Where the first window starts. It should start a window, see :func:`is_aligned`.
        end: Where the range ends. No window ends after it.
        size: One of :data:`WINDOW_SIZES`.
        timezone: The time zone the days are counted in: a name or a ``tzinfo``; UTC by default.
    """
    tz = get_timezone(timezone)
    if size == "day":
        day = start.astimezone(tz).date()
        while True:
            lower = _midnight(day, tz).astimezone(_UTC)
            day += dt.timedelta(days=1)
            upper = _midnight(day, tz).astimezone(_UTC)
            if upper > end:
                return
            yield lower, upper
    else:
        step = dt.timedelta(minutes=STEP_MINUTES[size])
        lower = start.astimezone(_UTC)
        while lower + step <= end:
            yield lower, lower + step
            lower += step


# ---------------------------------------------------------------------- schedules


@dataclass(frozen=True)
class Schedule:
    """A schedule, as written and as a 5-field cron expression.

    Attributes:
        expression: What the config wrote, with blanks collapsed, e.g. ``@daily`` or ``5 2 * * *``.
        cron: The 5-field cron expression, e.g. ``0 0 * * *``.
        source: ``metadata.schedule`` when the config sets it, or ``when`` when the schedule comes from its
            ``when`` text.
    """

    expression: str
    cron: str
    source: str = "metadata.schedule"


def parse_schedule(text: Any, source: str = "metadata.schedule") -> Schedule:
    """Parse a schedule: a 5-field cron expression, or ``@hourly``, ``@daily``, ``@weekly`` or ``@monthly``.

    Raises:
        ConfigError: If it is neither.
    """
    if not isinstance(text, str) or not text.strip():
        raise ConfigError(f"schedule must be a 5-field cron expression or one of {', '.join(SCHEDULE_ALIASES)}, "
                          f"got {text!r}")
    expression = " ".join(text.split())
    if expression.startswith("@") and expression not in SCHEDULE_ALIASES:
        raise ConfigError(f"schedule {expression!r} is not one of {', '.join(SCHEDULE_ALIASES)}; "
                          "write a 5-field cron expression for anything else")
    cron = SCHEDULE_ALIASES.get(expression, expression)
    from cronsim import CronSim, CronSimError

    try:
        next(CronSim(cron, dt.datetime(2000, 1, 1, tzinfo=_UTC)))
    except CronSimError as exc:
        raise ConfigError(f"schedule {expression!r} is not a valid cron expression: {exc}") from None
    except StopIteration:
        raise ConfigError(f"schedule {expression!r} never fires") from None
    return Schedule(expression, cron, source)


def effective_schedule(metadata: Any, when: Any = None) -> Schedule | None:
    """The schedule of a config: its ``metadata.schedule``, else the one its ``when`` text means, else ``None``.

    Raises:
        ConfigError: If ``metadata.schedule`` is not a valid schedule.
    """
    explicit = metadata.get("schedule") if isinstance(metadata, Mapping) else None
    if explicit is not None:
        return parse_schedule(explicit)
    alias = WHEN_SCHEDULES.get(when.strip().lower()) if isinstance(when, str) else None
    return parse_schedule(alias, source="when") if alias else None


def ignored_when(metadata: Any, when: Any = None) -> str | None:
    """A note for a ``when`` text that no schedule is read from, or ``None`` when there is nothing to say."""
    explicit = metadata.get("schedule") if isinstance(metadata, Mapping) else None
    if explicit is not None or not isinstance(when, str) or not when.strip():
        return None
    if when.strip().lower() in WHEN_SCHEDULES:
        return None
    return (f"when {when!r} is a label, so no schedule is read from it; set metadata.schedule, or write "
            f"{' / '.join(repr(word) for word in WHEN_SCHEDULES)} in when")


# ---------------------------------------------------------------------- the window declaration


@dataclass(frozen=True)
class WindowSpec:
    """The ``metadata.window`` object of a config.

    Attributes:
        size: ``day``, ``hour`` or ``15m``: how long the windows of a scheduled run are.
        column: The date or timestamp column that selects the rows of a window, or ``None``.
        timezone: The IANA name of the time zone the window's days are counted in, ``UTC`` by default.
    """

    size: str
    column: str | None = None
    timezone: str = "UTC"

    @property
    def tz(self) -> dt.tzinfo:
        """The time zone as a ``tzinfo``."""
        return get_timezone(self.timezone)


def window_problems(value: Any) -> list[tuple[str, str]]:
    """Check a ``metadata.window`` value, and return ``(path, message)`` for each problem.

    The path is ``""`` for the value itself, or ``".size"``, ``".column"`` or ``".timezone"``. A value of ``None``
    (no window) has no problems.
    """
    if value is None or value == "none":
        return []
    if isinstance(value, str):
        return [("", f"must be 'none' or an object with a 'size', got {value!r}")]
    if not isinstance(value, Mapping):
        return [("", f"must be 'none' or an object with a 'size', got {type(value).__name__}")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(set(map(str, value)) - set(WINDOW_KEYS))
    if unknown:
        problems.append(("", f"has unknown keys {unknown}; allowed: {list(WINDOW_KEYS)}"))
    size = value.get("size")
    if size is None:
        problems.append((".size", f"is required: one of {list(WINDOW_SIZES)}"))
    elif size not in WINDOW_SIZES:
        problems.append((".size", f"must be one of {list(WINDOW_SIZES)}, got {size!r}"))
    column = value.get("column")
    if column is not None and (not isinstance(column, str) or not column.strip()):
        problems.append((".column", f"must be a column name, got {column!r}"))
    zone = value.get("timezone")
    if zone is not None:
        try:
            get_timezone(zone)
        except ConfigError as exc:
            problems.append((".timezone", str(exc)))
    return problems


def parse_window_spec(value: Any) -> WindowSpec | None:
    """Read a ``metadata.window`` value: ``None`` for no value or ``"none"``, else a :class:`WindowSpec`.

    Raises:
        ConfigError: If the value is not valid, with the path of the first problem.
    """
    problems = window_problems(value)
    if problems:
        path, message = problems[0]
        raise ConfigError(f"metadata.window{path}: {message}")
    if value is None or value == "none":
        return None
    return WindowSpec(size=value["size"], column=value.get("column"), timezone=value.get("timezone") or "UTC")


SCHEDULE_SIZES = {"0 * * * *": "hour", "0 0 * * *": "day"}
"""The size that a schedule means when ``metadata.window`` gives none: ``@hourly`` is an hour and ``@daily`` a day."""


def window_size_of(metadata: Any, when: Any = None) -> str | None:
    """How long the windows of a config or a workflow are: its ``metadata.window.size``, else what its schedule means.

    A schedule of ``@hourly`` or ``@daily`` (or the cron expression that stands for it) means windows of an hour or a
    day. Any other schedule says when a run starts, and not how long the window it reads is, so there the size is
    ``None`` unless ``metadata.window`` gives it.

    Args:
        metadata: The ``metadata`` of the config or the workflow.
        when: Its ``when``, which means a schedule when ``metadata.schedule`` is absent.

    Raises:
        ConfigError: If ``metadata.window`` or ``metadata.schedule`` is not valid.
    """
    declared = parse_window_spec(metadata.get("window") if isinstance(metadata, Mapping) else None)
    if declared is not None:
        return declared.size
    schedule = effective_schedule(metadata, when)
    return SCHEDULE_SIZES.get(schedule.cron) if schedule is not None else None


# ---------------------------------------------------------------------- path placeholders


def placeholders(text: str) -> list[str]:
    """The window placeholders in ``text``, as written, e.g. ``['{window_start:%Y-%m-%d}']``."""
    return [match.group(0) for match in _PLACEHOLDER.finditer(text)] if isinstance(text, str) else []


def placeholder_problems(text: Any) -> list[str]:
    """What is wrong with the window placeholders in a source path: a malformed one, or one without a format."""
    if not isinstance(text, str):
        return []
    problems = []
    for match in _PLACEHOLDER.finditer(text):
        name, fmt = match.group(1), match.group(2)
        if not fmt:
            problems.append(f"{match.group(0)} needs a strftime format, for example {{{name}:%Y-%m-%d}}")
        elif "%" not in fmt:
            problems.append(f"{match.group(0)} has the format {fmt!r}, which has no strftime directive such as %Y; "
                            f"for example {{{name}:%Y-%m-%d}}")
    if _PLACEHOLDER_START.search(_PLACEHOLDER.sub("", text)):
        problems.append("has a placeholder that is not closed; write it as {window_start:FORMAT} or "
                        "{window_end:FORMAT}")
    return problems


def mentions_placeholder(text: Any) -> bool:
    """Whether ``text`` has the start of a window placeholder, written correctly or not."""
    return isinstance(text, str) and _PLACEHOLDER_START.search(text) is not None


def resolve_placeholders(text: str, bounds: tuple[dt.datetime, dt.datetime], timezone: Any = None) -> str:
    """Fill the window placeholders of ``text`` in, showing the bounds in ``timezone``.

    Args:
        text: A path such as ``drops/orders_{window_start:%Y-%m-%d}.csv``.
        bounds: The window, ``(start, end)``.
        timezone: The time zone to show the bounds in: a name or a ``tzinfo``; UTC by default.

    Returns:
        ``text`` with every placeholder replaced by the ``strftime`` of its bound.

    Raises:
        ConfigError: If a placeholder has no format.
    """
    tz = get_timezone(timezone)

    def fill(match: re.Match) -> str:
        name, fmt = match.group(1), match.group(2)
        if not fmt:
            raise ConfigError(f"{match.group(0)} needs a strftime format, for example {{{name}:%Y-%m-%d}}")
        return bounds[0 if name == "window_start" else 1].astimezone(tz).strftime(fmt)

    return _PLACEHOLDER.sub(fill, text)


def source_path(config: Any) -> str | None:
    """The ``metadata.source.path`` of a config, a spec dict or a metadata dict, or ``None``."""
    metadata = getattr(config, "metadata", None)
    if metadata is None and isinstance(config, Mapping):
        metadata = config.get("metadata", config)
    source = metadata.get("source") if isinstance(metadata, Mapping) else None
    path = source.get("path") if isinstance(source, Mapping) else None
    return path if isinstance(path, str) else None


def bind_config(config: Any, bounds: tuple[dt.datetime, dt.datetime] | None) -> Any:
    """Return ``config`` with the placeholders of its source path filled in for the window.

    The config itself is not changed, and a config without placeholders is returned as it is.

    Raises:
        ConfigError: If the path has placeholders and there is no window.
    """
    if config is None:
        return None
    path = source_path(config)
    found = placeholders(path) if path is not None else []
    if not found:
        return config
    if bounds is None:
        raise ConfigError(f"metadata.source.path {path!r} has the placeholder {found[0]}, which needs a window: "
                          "pass window=, or --window START/END (or yesterday) on the command line")
    metadata = config.metadata
    resolved = resolve_placeholders(path, bounds, timezone_of(metadata))
    return dataclasses.replace(config, metadata={**metadata, "source": {**metadata["source"], "path": resolved}})


def lacks_window_selection(metadata: Any, when: Any = None, mode: str | None = None) -> bool:
    """Whether a scheduled config appends the same rows to an Iceberg table in every window.

    That happens when the config has a schedule (its own, or one its ``when`` means), writes in ``append`` mode
    to an Iceberg table, and neither names a window placeholder in its source path nor a ``window.column``.

    Args:
        metadata: The ``metadata`` of the config.
        when: The ``when`` of the config.
        mode: The write mode of this run, when it overrides the config's.
    """
    if not isinstance(metadata, Mapping) or effective_schedule(metadata, when) is None:
        return False
    target = metadata.get("target")
    if not isinstance(target, Mapping) or str(target.get("format", "")).strip().upper() != "ICEBERG":
        return False
    write_mode = str(mode or target.get("write_mode") or "append").strip().lower()
    if write_mode != "append" or placeholders(source_path({"metadata": metadata}) or ""):
        return False
    window = metadata.get("window")
    return not (isinstance(window, Mapping) and window.get("column"))


# ---------------------------------------------------------------------- selecting the rows of a window


def _limits(column: str, column_type: Any, bounds: tuple[dt.datetime, dt.datetime], tz: dt.tzinfo) -> tuple[Any, Any]:
    """The bounds of the window as values that compare with a column of this Arrow type."""
    import pyarrow as pa

    if pa.types.is_timestamp(column_type):
        if column_type.tz is not None:
            return bounds[0].astimezone(_UTC), bounds[1].astimezone(_UTC)
        return tuple(bound.astimezone(tz).replace(tzinfo=None) for bound in bounds)  # type: ignore[return-value]
    if pa.types.is_date(column_type):
        local = [bound.astimezone(tz) for bound in bounds]
        if any(moment.time() != dt.time(0) for moment in local):
            raise ConfigError(f"metadata.window.column {column!r} is a date column, so a window must start and end "
                              f"at midnight in {getattr(tz, 'key', 'UTC')}, got "
                              f"{bounds[0].isoformat()}/{bounds[1].isoformat()}")
        return local[0].date(), local[1].date()
    raise ConfigError(f"metadata.window.column {column!r} must be a date or timestamp column, but it is {column_type}")


def filter_rows(table: Any, column: str, bounds: tuple[dt.datetime, dt.datetime], timezone: Any = None) -> Any:
    """Keep the rows of ``table`` whose ``column`` is at or after the start of the window and before its end.

    A timestamp column with a time zone is compared as an instant. A timestamp column without one is read as local
    time in ``timezone``. A date column is compared as a date. A row with a null in the column is in no window.

    Args:
        table: A ``pyarrow.Table``.
        column: The date or timestamp column.
        bounds: The window, ``(start, end)``, as aware datetimes.
        timezone: The time zone of the window's days: a name or a ``tzinfo``; UTC by default.

    Returns:
        The rows inside the window, as a ``pyarrow.Table``.

    Raises:
        ConfigError: If the column is missing, or is not a date or timestamp column.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    if column not in table.column_names:
        raise ConfigError(f"metadata.window.column {column!r} is not a column of the source "
                          f"(columns: {', '.join(table.column_names)})")
    values = table.column(column)
    low, high = _limits(column, values.type, bounds, get_timezone(timezone))
    inside = pc.and_(pc.greater_equal(values, pa.scalar(low, type=values.type)),
                     pc.less(values, pa.scalar(high, type=values.type)))
    return table.filter(pc.fill_null(inside, False))


def overwrite_filter(column: str, bounds: tuple[dt.datetime, dt.datetime], timezone: Any, column_type: Any) -> Any:
    """The Iceberg expression that matches the rows of the window, for ``Iceberg.put(overwrite_filter=...)``.

    It is ``column >= start AND column < end`` with the bounds written the way the column's type reads them, so a
    window overwrite replaces the rows that :func:`filter_rows` keeps.

    Args:
        column: The date or timestamp column.
        bounds: The window, ``(start, end)``, as aware datetimes.
        timezone: The time zone of the window's days.
        column_type: The ``pyarrow`` type of the column in the data to write.

    Raises:
        ConfigError: If the column is not a date or timestamp column.
    """
    from pyiceberg.expressions import And, GreaterThanOrEqual, LessThan

    low, high = _limits(column, column_type, bounds, get_timezone(timezone))
    return And(GreaterThanOrEqual(column, low.isoformat()), LessThan(column, high.isoformat()))


__all__ = [
    "PLACEHOLDER_NAMES",
    "SCHEDULE_ALIASES",
    "SCHEDULE_SIZES",
    "SHORTCUTS",
    "STEP_MINUTES",
    "WHEN_SCHEDULES",
    "WINDOW_KEYS",
    "WINDOW_SIZES",
    "Schedule",
    "WindowSpec",
    "bind_config",
    "effective_schedule",
    "filter_rows",
    "floor_boundary",
    "get_timezone",
    "ignored_when",
    "is_aligned",
    "is_shortcut",
    "iter_windows",
    "lacks_window_selection",
    "mentions_placeholder",
    "now_utc",
    "overwrite_filter",
    "parse_boundary",
    "parse_schedule",
    "parse_window_spec",
    "placeholder_problems",
    "placeholders",
    "resolve_placeholders",
    "shortcut_window",
    "source_path",
    "timezone_of",
    "window_problems",
    "window_size_of",
]
