"""Monitoring: what a table should look like between runs, and the monitors that check it.

The SLA settings of a config are in :mod:`local_data_platform.monitoring.sla`. :func:`evaluate` runs the monitors that
the SLA of a config asks for and returns a :class:`MonitorResult` for each. The results go to a history, which
:mod:`local_data_platform.monitoring.history` writes and reads, and ``ldp monitor`` does both. Importing the package
imports nothing outside the standard library.
"""

from local_data_platform.monitoring.evaluation import evaluate
from local_data_platform.monitoring.results import STATUSES, MonitorResult

__all__ = [
    "MonitorResult",
    "STATUSES",
    "evaluate",
]
