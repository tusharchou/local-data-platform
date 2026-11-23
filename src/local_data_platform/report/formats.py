"""How a value is written in a report: numbers, money, percentages, dates and text.

The HTML page, the Markdown and, later, the Slack message of a report write a value with these rules, so a number looks
the same in a key figure, on the axis of a chart and in a table. A format is one of the strings that a report file may
name for a block, and :func:`metric_format` turns the format of a metric into one of them::

    integer         1234.5 is 1,235
    number          1234 is 1,234 and 1234.5 is 1,234.50, the same rule as ``ldp metrics query`` has for a column
    number:2        1234.5 is 1,234.50
    percent:1       0.1234 is 12.3% (a percent is a fraction), and percent alone is percent:1
    currency:USD    1234.5 is $1,234.50, with the symbol of the currency, or its code when there is no symbol

A value with no format is written as it is, with no separators, so that an id or a year is not changed. Rounding is half
up on the decimal digits of the value, so the same number is always written the same. A value that is missing is a
``-``.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from local_data_platform.report.spec import FORMAT_PATTERN

MISSING = "-"
"""What is written for a value that is missing."""

SYMBOLS = {
    "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "INR": "\u20b9", "JPY": "\u00a5", "CNY": "\u00a5",
    "KRW": "\u20a9", "RUB": "\u20bd", "TRY": "\u20ba", "ILS": "\u20aa", "BRL": "R$", "CAD": "CA$", "AUD": "A$",
    "MXN": "MX$", "PHP": "\u20b1", "VND": "\u20ab", "NGN": "\u20a6", "UAH": "\u20b4",
}
"""The symbol of the currencies that have a common one. Any other code is written as the code and a space."""

ZERO_DECIMALS = frozenset({"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND",
                           "VUV", "XAF", "XOF", "XPF"})
THREE_DECIMALS = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
"""The currencies that have no minor unit, or a thousandth, by ISO 4217. The others have two decimals."""

METRIC_FORMATS = {"number": "number", "percent": "percent:1", "currency": "number:2"}
"""The format of a metric, which says how a person reads it, as the format that writes it."""

_FORMAT = re.compile(FORMAT_PATTERN)
_PRECISION = 80


@dataclass(frozen=True)
class Format:
    """A parsed format.

    Attributes:
        kind: ``integer``, ``number``, ``percent`` or ``currency``.
        decimals: How many decimals, or ``None`` for a ``number`` that takes none when the whole column is whole and two
            otherwise.
        code: The currency code of a ``currency``.
    """

    kind: str
    decimals: int | None = None
    code: str | None = None


def parse_format(text: str) -> Format:
    """The :class:`Format` of a format string.

    Raises:
        ValueError: If the text is not ``integer``, ``number``, ``number:N``, ``percent``, ``percent:N`` or
            ``currency:CODE``.
    """
    if not isinstance(text, str) or not _FORMAT.fullmatch(text):
        raise ValueError(f"{text!r} is not a format. Use integer, number, number:N, percent, percent:N or "
                         f"currency:CODE")
    kind, _, argument = text.partition(":")
    if kind == "integer":
        return Format("integer", 0)
    if kind == "currency":
        return Format("currency", _currency_decimals(argument), argument)
    if kind == "percent":
        return Format("percent", int(argument) if argument else 1)
    return Format("number", int(argument) if argument else None)


def metric_format(name: str | None) -> str | None:
    """The format string for the ``number``, ``percent`` or ``currency`` of a metric, or ``None`` for no format."""
    return METRIC_FORMATS.get(name) if name else None


def _currency_decimals(code: str) -> int:
    return 0 if code in ZERO_DECIMALS else 3 if code in THREE_DECIMALS else 2


def is_number(value: Any) -> bool:
    """Whether a value is a number that a format can write, an ``int``, a ``float`` or a ``Decimal``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    return not (isinstance(value, float) and not math.isfinite(value)) and not (
        isinstance(value, Decimal) and not value.is_finite())


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(int(value))


def _whole(value: Any) -> bool:
    return _decimal(value) == _decimal(value).to_integral_value()


def _fixed(value: Any, decimals: int) -> tuple[str, str]:
    """``(sign, digits)`` of a number rounded half up to ``decimals``, with the thousands grouped. No sign for zero."""
    with localcontext() as context:
        context.prec = _PRECISION
        rounded = _decimal(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = format(abs(rounded), "f")
    whole, _, fraction = text.partition(".")
    digits = f"{int(whole):,}" + (f".{fraction}" if fraction else "")
    return ("-" if rounded < 0 else ""), digits


def _write(value: Any, parsed: Format, whole: bool) -> str:
    if parsed.kind == "percent":
        with localcontext() as context:
            context.prec = _PRECISION
            value = _decimal(value) * 100
        sign, digits = _fixed(value, parsed.decimals or 0)
        return f"{sign}{digits}%"
    decimals = parsed.decimals if parsed.decimals is not None else (0 if whole else 2)
    sign, digits = _fixed(value, decimals)
    if parsed.kind == "currency":
        symbol = SYMBOLS.get(parsed.code or "") or f"{parsed.code} "
        return f"{sign}{symbol}{digits}"
    return f"{sign}{digits}"


def plain(value: Any) -> str:
    """A value written as it is, with no format.

    There are no separators, a float has at most six decimals, a date is ISO text and a missing value is ``-``.
    """
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def format_values(values: Sequence[Any], fmt: str | None = None) -> list[str]:
    """The values of one column written by one format, so that the column agrees with itself.

    A ``number`` with no digits named takes none when every number of the column is whole and two otherwise, as ``ldp
    metrics query`` writes it. A value that is not a number is written as it is, whatever the format, and a missing
    value is a ``-``.

    Args:
        values: The values of the column.
        fmt: A format string, or ``None`` for each value as it is.

    Raises:
        ValueError: If ``fmt`` is not a format.
    """
    if fmt is None:
        return [plain(value) for value in values]
    parsed = parse_format(fmt)
    whole = all(_whole(value) for value in values if is_number(value))
    return [MISSING if value is None else _write(value, parsed, whole) if is_number(value) else plain(value)
            for value in values]


def format_value(value: Any, fmt: str | None = None) -> str:
    """One value written by a format, as in :func:`format_values`."""
    return format_values([value], fmt)[0]


def change(value: Any, compare: Any) -> float | None:
    """The change of ``value`` from ``compare`` as a fraction, ``0.1`` for ten percent more.

    Returns:
        ``None`` when either is not a number, or ``compare`` is zero, because a change from nothing is not a percent.
    """
    if not is_number(value) or not is_number(compare) or _decimal(compare) == 0:
        return None
    with localcontext() as context:
        context.prec = _PRECISION
        return float((_decimal(value) - _decimal(compare)) / abs(_decimal(compare)))


def change_text(fraction: float | None) -> tuple[str, str] | None:
    """A change as its words and its direction, such as ``("+12.3%", "up")``.

    The direction is ``up``, ``down`` or ``flat``, and a change that rounds to zero is ``flat``. ``None`` gives
    ``None``.
    """
    if fraction is None:
        return None
    sign, digits = _fixed(Decimal(repr(fraction)) * 100, 1)
    if digits in ("0.0", "-0.0"):
        return "0.0%", "flat"
    return f"{sign or '+'}{digits}%", "down" if sign else "up"


__all__ = [
    "Format",
    "METRIC_FORMATS",
    "MISSING",
    "SYMBOLS",
    "change",
    "change_text",
    "format_value",
    "format_values",
    "is_number",
    "metric_format",
    "parse_format",
    "plain",
]
