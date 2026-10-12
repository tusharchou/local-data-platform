"""Build checks from a config and run them: :func:`checks_from_config` and :func:`run_checks`."""

import inspect
from collections.abc import Iterable, Mapping
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger
from local_data_platform.quality.checks import (
    AcceptedValues,
    Check,
    Freshness,
    NotNull,
    Range,
    RowCount,
    SchemaHas,
    Unique,
    as_table,
)
from local_data_platform.quality.report import QualityReport

logger = get_logger(__name__)

CHECKS: dict[str, type[Check]] = {
    cls.check_type: cls for cls in (RowCount, NotNull, Unique, AcceptedValues, Range, Freshness, SchemaHas)
}
"""Config ``check`` names mapped to check classes."""


def checks_from_config(items: Iterable[Mapping[str, Any] | Check] | None) -> list[Check]:
    """Build checks from the ``quality.checks`` list of a config.

    Each item is an object with a ``check`` key naming the check (see :data:`CHECKS`) and the
    check's constructor parameters, for example ``{"check": "range", "column": "fare", "min": 0}``.
    Every check also accepts an optional ``name``. Items that are already :class:`Check`
    instances are passed through unchanged.

    Args:
        items: The list of check configs, or ``None`` for no checks.

    Returns:
        The checks, in config order.

    Raises:
        ConfigError: If an item is not an object, names an unknown check, has an unknown or
            missing parameter, or has a parameter with an invalid value.
    """
    if items is None:
        return []
    if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, Iterable):
        raise ConfigError(
            "quality checks must be a list of objects such as "
            f"{{'check': 'not_null', 'columns': ['id']}}, got {type(items).__name__}"
        )
    return [item if isinstance(item, Check) else _check_from_item(index, item) for index, item in enumerate(items)]


def _check_from_item(index: int, item: Any) -> Check:
    where = f"quality check #{index + 1}"
    known = ", ".join(sorted(CHECKS))
    if not isinstance(item, Mapping):
        raise ConfigError(f"{where} must be an object with a 'check' key, got {item!r}")
    params = dict(item)
    kind = params.pop("check", None)
    if kind is None:
        raise ConfigError(f"{where} is missing 'check'; expected one of: {known}")
    cls = CHECKS.get(kind.strip().lower()) if isinstance(kind, str) else None
    if cls is None:
        raise ConfigError(f"{where} has unknown check {kind!r}; expected one of: {known}")
    parameters = inspect.signature(cls).parameters
    unknown = sorted(set(params) - set(parameters))
    if unknown:
        raise ConfigError(
            f"{where} ({cls.check_type}) has unknown parameter(s) {unknown}; allowed: {', '.join(parameters)}"
        )
    missing = [name for name, parameter in parameters.items()
               if parameter.default is inspect.Parameter.empty and name not in params]
    if missing:
        raise ConfigError(f"{where} ({cls.check_type}) is missing required parameter(s) {missing}")
    try:
        return cls(**params)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} ({cls.check_type}) is invalid: {exc}") from exc


def run_checks(
    df: Any,
    checks: Iterable[Check | Mapping[str, Any]] | Check | None,
    context: Mapping[str, Any] | None = None,
) -> QualityReport:
    """Run checks on a table and collect the results.

    Args:
        df: The data, as a ``pyarrow.Table`` (a ``RecordBatch`` or anything ``pyarrow.table()``
            accepts also works).
        checks: :class:`Check` instances, config dicts (see :func:`checks_from_config`), or both.
        context: Facts about the load passed to every check, e.g. ``{"source_rows": 1000}`` for
            ``RowCount(equals="source")``.

    Returns:
        A report with one result per check, in order. It never raises for failed checks; call
        :meth:`QualityReport.raise_for_failures` to stop on failure.

    Raises:
        TypeError: If ``df`` can't be read as a table.
        ConfigError: If a config dict in ``checks`` is invalid.
    """
    table = as_table(df)
    resolved = checks_from_config([checks] if isinstance(checks, Check) else checks)
    results = []
    for check in resolved:
        result = check.run(table, context)
        logger.debug("%s", result)
        results.append(result)
    report = QualityReport(results)
    if results:
        logger.info("data quality: %d of %d checks passed on %d rows",
                    len(results) - len(report.failures), len(results), table.num_rows)
    return report
