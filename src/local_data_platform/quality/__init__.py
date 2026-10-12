"""Data quality checks that run on a ``pyarrow.Table`` before it is written.

Build checks in Python or from a config's ``quality.checks`` list, run them, then decide::

    from local_data_platform.quality import NotNull, RowCount, run_checks

    report = run_checks(table, [RowCount(min=1), NotNull(["ride_id"])])
    print(report.summary())
    report.raise_for_failures()  # DataQualityError if any check failed

See :mod:`local_data_platform.quality.checks` for how each check treats nulls, NaN and time
zones, and :mod:`local_data_platform.quality.temporal` for the time-series checks (``monotonic``,
``max_skew``, ``rate_below`` and ``max_gap``).
"""

from local_data_platform.quality.checks import (
    AcceptedValues,
    Check,
    Freshness,
    NotNull,
    Range,
    RowCount,
    SchemaHas,
    Unique,
    parse_type,
)
from local_data_platform.quality.report import CheckResult, QualityReport
from local_data_platform.quality.runner import CHECKS, checks_from_config, run_checks
from local_data_platform.quality.temporal import TEMPORAL_CHECKS, MaxGap, MaxSkew, Monotonic, RateBelow

# The temporal checks live in their own module; make their config names known to checks_from_config.
for _check in TEMPORAL_CHECKS:
    CHECKS.setdefault(_check.check_type, _check)
del _check

__all__ = [
    "CHECKS",
    "AcceptedValues",
    "Check",
    "CheckResult",
    "Freshness",
    "MaxGap",
    "MaxSkew",
    "Monotonic",
    "NotNull",
    "QualityReport",
    "Range",
    "RateBelow",
    "RowCount",
    "SchemaHas",
    "TEMPORAL_CHECKS",
    "Unique",
    "checks_from_config",
    "parse_type",
    "run_checks",
]
