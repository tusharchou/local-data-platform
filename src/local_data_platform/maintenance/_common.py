"""Helpers shared by the maintenance modules: table coercion, time handling and the error type."""

import datetime as dt
import numbers
from typing import Any

from pyiceberg.table import Table as PyIcebergTable

from local_data_platform.exceptions import LDPError

IDEMPOTENCY_KEY = "ldp.idempotency-key"
"""Snapshot-summary key that carries a publish's idempotency key (SaaS design §6.3)."""

HORIZON_PROPERTY = "ldp.idempotency.horizon-days"
"""Table property naming how many days of idempotency keys must stay findable (SaaS design §6.3)."""

DEFAULT_HORIZON_DAYS = 7.0
"""The idempotency horizon when a table doesn't set :data:`HORIZON_PROPERTY`."""


class MaintenanceError(LDPError):
    """Raised when maintenance cannot prove an operation is safe, so it changes nothing."""


def as_table(table: Any) -> PyIcebergTable:
    """Return the pyiceberg table behind ``table``.

    Args:
        table: A pyiceberg ``Table``, or an object with a ``table()`` method that returns one, such as
            :class:`local_data_platform.format.iceberg.Iceberg`.

    Returns:
        The pyiceberg table.

    Raises:
        TypeError: If ``table`` is neither.
        TableNotFound: If ``table`` is an ``Iceberg`` format whose table doesn't exist yet.
    """
    if isinstance(table, PyIcebergTable):
        return table
    loader = getattr(table, "table", None)
    if callable(loader):
        loaded = loader()
        if isinstance(loaded, PyIcebergTable):
            return loaded
    raise TypeError(f"expected a pyiceberg Table or an Iceberg format, got {type(table).__name__}")


def table_name(table: PyIcebergTable) -> str:
    """``"<namespace>.<name>"`` for a pyiceberg table."""
    name = table.name()
    return ".".join(name) if isinstance(name, tuple) else str(name)


def utc_now() -> dt.datetime:
    """The current time, tz-aware in UTC. Tests monkeypatch this to pin the clock."""
    return dt.datetime.now(dt.timezone.utc)


def to_utc(value: dt.datetime) -> dt.datetime:
    """Return ``value`` as a tz-aware UTC datetime; a naive value is taken to be UTC already."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def to_millis(value: dt.datetime) -> int:
    """Milliseconds since the epoch, the unit of Iceberg snapshot timestamps."""
    utc = to_utc(value)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    delta = utc - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000


def iso_millis(millis: int) -> str:
    """An ISO-8601 UTC string, to the millisecond, for a millisecond timestamp."""
    moment = dt.datetime.fromtimestamp(millis / 1000, tz=dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_instant(value: Any, now: dt.datetime, what: str) -> dt.datetime:
    """Turn a ``datetime`` (absolute) or a ``timedelta`` (that long before ``now``) into a UTC datetime.

    Raises:
        TypeError: If ``value`` is neither.
        ValueError: If a ``timedelta`` is negative.
    """
    if isinstance(value, dt.timedelta):
        if value < dt.timedelta(0):
            raise ValueError(f"{what} must not be a negative timedelta, got {value}")
        return now - value
    if isinstance(value, dt.datetime):
        return to_utc(value)
    raise TypeError(f"{what} must be a datetime or a timedelta, got {type(value).__name__}")


def check_non_negative(value: Any, what: str) -> float:
    """Return ``value`` as a float, rejecting booleans, non-numbers and negatives."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{what} must be a number, got {type(value).__name__}")
    number = float(value)
    if number != number or number < 0:  # NaN or negative
        raise ValueError(f"{what} must be a non-negative number, got {value!r}")
    return number


def check_count(value: Any, what: str, minimum: int) -> int:
    """Return ``value`` as an int of at least ``minimum``, rejecting booleans and non-integers."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError(f"{what} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{what} must be at least {minimum}, got {value}")
    return int(value)
