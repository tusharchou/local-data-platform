"""Results of data quality checks: :class:`CheckResult` and :class:`QualityReport`."""

import datetime as dt
import decimal
import math
import numbers
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from local_data_platform.exceptions import DataQualityError


def to_jsonable(value: Any) -> Any:
    """Convert ``value`` to plain JSON types so reports can be serialised with ``json.dumps``.

    Datetimes and dates become ISO strings, decimals become strings, non-finite floats become
    ``"nan"``/``"inf"``/``"-inf"``, pyarrow scalars are unwrapped and anything else falls back to
    ``str(value)``.

    Args:
        value: Any value found in check metrics.

    Returns:
        The value using only ``None``, ``bool``, ``int``, ``float``, ``str``, ``list`` and ``dict``.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else str(number)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="backslashreplace")
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [to_jsonable(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "as_py"):
        try:
            return to_jsonable(value.as_py())
        except (ValueError, TypeError):
            return str(value)
    return str(value)


@dataclass
class CheckResult:
    """The outcome of one check on one table.

    Attributes:
        name: The check's name, e.g. ``"not_null(ride_id)"``.
        passed: Whether the data satisfied the check.
        details: A one-line, human-readable explanation.
        failing_rows: Rows that violate a row-level check. Table-level checks (``row_count``,
            ``freshness``, ``schema``) and checks that could not run (missing column) report 0.
        metrics: Measurements behind the verdict, such as null counts or the observed maximum.
    """

    name: str
    passed: bool
    details: str
    failing_rows: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the result as JSON-serialisable primitives."""
        return {
            "name": self.name,
            "passed": bool(self.passed),
            "details": self.details,
            "failing_rows": int(self.failing_rows),
            "metrics": to_jsonable(self.metrics),
        }

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        suffix = f" [failing rows: {self.failing_rows}]" if self.failing_rows else ""
        return f"[{status}] {self.name}: {self.details}{suffix}"


@dataclass
class QualityReport:
    """The results of running a list of checks on one table.

    A report with no results has passed: there was nothing to fail.

    Attributes:
        results: One :class:`CheckResult` per check, in the order the checks ran.
    """

    results: list[CheckResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.results = list(self.results)

    @property
    def passed(self) -> bool:
        """``True`` when every check passed."""
        return all(result.passed for result in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        """The results of the checks that failed, in run order."""
        return [result for result in self.results if not result.passed]

    def raise_for_failures(self) -> None:
        """Raise if any check failed.

        Raises:
            DataQualityError: With a message naming each failed check; the report itself is
                available as ``error.report``.
        """
        failures = self.failures
        if not failures:
            return
        lines = [f"{len(failures)} of {len(self.results)} data quality checks failed:"]
        lines += [f"  - {result.name}: {result.details}" for result in failures]
        raise DataQualityError("\n".join(lines), report=self)

    def summary(self) -> str:
        """Return a multi-line, human-readable summary with one line per check."""
        if not self.results:
            return "Data quality: no checks run"
        failed = len(self.failures)
        header = f"Data quality: {len(self.results) - failed} of {len(self.results)} checks passed"
        if failed:
            header += f", {failed} failed"
        return "\n".join([header] + [f"  {result}" for result in self.results])

    def to_dict(self) -> dict[str, Any]:
        """Return the report as JSON-serialisable primitives."""
        return {
            "passed": self.passed,
            "checks_run": len(self.results),
            "checks_failed": len(self.failures),
            "results": [result.to_dict() for result in self.results],
        }

    def __iter__(self) -> Iterator[CheckResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)
