"""Orchestration: the parts of a lakehouse that make it run for the right time window.

0.1.3 builds this package up item by item:

* :mod:`~local_data_platform.orchestration.windows`: schedules, time zones, the window shortcuts, the placeholders of a
  source path and the filters that select the rows of one window.
* :mod:`~local_data_platform.orchestration.workflow`: workflow files, with their validation, their JSON Schema and their
  plan.
* :mod:`~local_data_platform.orchestration.tasks`: the registry of task kinds, with the ``pipeline`` and ``maintain``
  kinds.
* :mod:`~local_data_platform.orchestration.runner`: the runner, :func:`run_workflow`.

The names below are loaded when they are first used, so importing this package, which ``local_data_platform.spec`` does
for the windows, does not import pyiceberg::

    from local_data_platform.orchestration import run_workflow

    result = run_workflow("workflows/daily_orders.json", window="2026-08-01/2026-08-02")
"""

import importlib
from typing import Any

_LAZY = {
    "run_workflow": "runner",
    "TaskResult": "runner",
    "WorkflowResult": "runner",
    "Workflow": "workflow",
    "load_workflow": "workflow",
    "plan_workflow": "workflow",
    "validate_workflow": "workflow",
    "workflow_schema": "workflow",
    "register_task_kind": "tasks",
    "task_kinds": "tasks",
    "Task": "tasks",
    "TaskKind": "tasks",
    "TaskOutcome": "tasks",
    "TaskPlan": "tasks",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


__all__ = sorted(_LAZY)
