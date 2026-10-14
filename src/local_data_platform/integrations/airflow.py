"""Run a dataset config as an Apache Airflow 3 task, with Airflow's retries and backfills mapped onto windows and keys.

::

    from airflow.sdk import DAG
    from airflow.timetables.interval import CronDataIntervalTimetable
    from local_data_platform.integrations.airflow import LdpRunOperator

    with DAG("restaurant_orders", schedule=CronDataIntervalTimetable("@daily", timezone="UTC"),
             start_date=datetime(2026, 8, 1)):
        LdpRunOperator(task_id="orders", config="/data/restaurant/config/orders.json", retries=2)

A cron string as ``schedule``, such as ``"@daily"``, makes Airflow 3 use a trigger timetable, whose runs have a data
interval that starts and ends at the same instant, and there is no window to load in that. So give the schedule as a
``CronDataIntervalTimetable``, as above, or set ``create_cron_data_intervals`` to ``True`` in the ``[scheduler]``
section of the Airflow configuration.

The task runs the config for the data interval of its DAG run, as ``ldp run CONFIG --window START/END`` would, and the
three things that Airflow knows become the three things that LDP's exactly-once protocol needs:

* **The window** is ``[data_interval_start, data_interval_end)``. So the idempotency key is the one that
  ``ldp plan CONFIG --window START/END`` prints for the same interval, and a second DAG run over the same interval, such
  as a backfill that Airflow starts, finds the key published and writes nothing.
* **The run id** is a string that is the same for every try of a task instance: ``airflow:`` and the DAG id, the run id
  of the DAG run and the task id, and the map index of a mapped task, joined with colons.
* **The attempt** is ``try_number`` of the task instance, which counts up with every try. So the first commit of try N
  fences every older try of the task instance, also one that Airflow gave up on and that is still running.

A run that a data gate held back, ``DataQualityError``, fails the task with ``AirflowFailException``, so Airflow does
not retry it, because the same source would hold it back again. A config that cannot run, ``ConfigError``, fails it the
same way, as ``ldp workflow run`` does not retry it either, and so does a DAG run that has no data interval to take a
window from. A window that was already published, ``skipped_duplicate``, succeeds. Any other error is raised as it is,
so Airflow retries it as the ``retries`` of the task say, with the next try as the next attempt. The value that
``execute`` returns, and so the XCom of the task, is
:meth:`~local_data_platform.pipeline.PipelineResult.to_dict` of the run.

The mapping is plain Python and needs no Airflow: :func:`window_of`, :func:`run_id_of`, :func:`attempt_of`,
:func:`run_options`, :func:`run` and :func:`key_for` take the ``context`` that Airflow passes to ``execute``, anything
that has the same keys and attributes. Airflow is imported when :class:`LdpRunOperator` is first used, with
``from airflow.sdk import BaseOperator``, so importing this module costs nothing and works without Airflow installed.
There is no pip extra for it. Install Airflow 3 with its constraints file, as ``docs/airflow.md`` shows.
"""

import functools
import os
from collections.abc import Mapping
from typing import Any

from local_data_platform.exceptions import ConfigError, DataQualityError

_INSTALL_HINT = ("LdpRunOperator needs Apache Airflow 3, which is not installed here. Install it with the constraints "
                 "file of its release, see https://airflow.apache.org/docs/apache-airflow/stable/installation/"
                 "installing-from-pypi.html, and then install local-data-platform in the same environment")


class NoDataInterval(ConfigError):
    """The DAG run has no data interval, or an empty one, so there is no window to run."""


def _get(context: Any, name: str) -> Any:
    """A key of the context, which is a mapping in Airflow and may be anything with the key in a test."""
    try:
        return context[name]
    except (KeyError, TypeError):
        return getattr(context, name, None)


def _task_instance(context: Any) -> Any:
    ti = _get(context, "ti") or _get(context, "task_instance")
    if ti is None:
        raise ConfigError("the context has no task instance (ti), so there is no run id and no attempt")
    return ti


def window_of(context: Any) -> tuple[Any, Any]:
    """The window of a task instance: ``(data_interval_start, data_interval_end)`` of its DAG run.

    Args:
        context: The ``context`` that Airflow passes to ``execute``.

    Returns:
        The two ends, as the aware datetimes that Airflow gives. ``run_config`` and ``ldp plan`` read them in UTC.

    Raises:
        NoDataInterval: If the DAG run has no data interval, such as a run that an asset event started, or an empty
            one, which Airflow 3 gives to the runs of a cron string as ``schedule``. Such a run has no window for the
            config to load.
    """
    start, end = _get(context, "data_interval_start"), _get(context, "data_interval_end")
    run = _get(context, "run_id") or getattr(_get(context, "ti"), "run_id", "?")
    if start is None or end is None:
        raise NoDataInterval(f"the DAG run {run} has no data interval, so there is no window for the config to load. "
                             "A run from a schedule has one, and so has a backfill")
    if start == end:
        raise NoDataInterval(f"the data interval of the DAG run {run} is empty, it starts and ends at {start}, so "
                             "there is no window for the config to load. Airflow 3 does this for a cron string as the "
                             "schedule. Give the schedule as CronDataIntervalTimetable(\"@daily\", timezone=\"UTC\"), "
                             "from airflow.timetables.interval, or set create_cron_data_intervals to True in the "
                             "[scheduler] section of the Airflow configuration")
    return start, end


def run_id_of(context: Any) -> str:
    """The run id of a task instance: the same string for every try of it, and a different one for every other.

    It is ``airflow:DAG_ID:RUN_ID:TASK_ID``, with ``:MAP_INDEX`` after the task id for a mapped task. Two mapped
    instances of one task get two run ids, because they stage their writes under it.
    """
    ti = _task_instance(context)
    run_id = f"airflow:{ti.dag_id}:{ti.run_id}:{ti.task_id}"
    index = getattr(ti, "map_index", -1)
    return run_id if index is None or index < 0 else f"{run_id}:{index}"


def attempt_of(context: Any) -> int:
    """The attempt of a task instance: its ``try_number``, which is 1 for the first try and counts up.

    Raises:
        ConfigError: If the task instance has no ``try_number`` of 1 or more, which would give every try the same
            attempt and leave nothing to fence a try that is still running.
    """
    attempt = getattr(_task_instance(context), "try_number", None)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ConfigError(f"the task instance has try_number {attempt!r}, and a try needs a whole number from 1")
    return attempt


def run_options(context: Any, mode: str | None = None) -> dict[str, Any]:
    """The keyword arguments of :func:`~local_data_platform.etl.run_config` for a task instance.

    Args:
        context: The ``context`` that Airflow passes to ``execute``.
        mode: A write mode that overrides the one of the target, ``append``, ``overwrite`` or ``upsert``.

    Returns:
        ``{"mode", "window", "run_id", "attempt"}``.
    """
    return {"mode": mode, "window": window_of(context), "run_id": run_id_of(context), "attempt": attempt_of(context)}


def run(config: "str | os.PathLike | Mapping[str, Any]", context: Any, mode: str | None = None) -> Any:
    """Run a config for a task instance: its window, its run id and its attempt.

    Args:
        config: The path of a dataset config, or a config dict.
        context: The ``context`` that Airflow passes to ``execute``.
        mode: A write mode that overrides the one of the target.

    Returns:
        The :class:`~local_data_platform.pipeline.PipelineResult`.

    Raises:
        NoDataInterval: If the DAG run has no data interval.
        DataQualityError: If a data gate held the run back. ``LdpRunOperator`` fails the task without a retry for it.
    """
    from local_data_platform.etl import run_config

    return run_config(config, **run_options(context, mode))


def key_for(config: "str | os.PathLike | Mapping[str, Any]", context: Any) -> str:
    """The idempotency key that a task instance publishes under, without running anything.

    It is the key that ``ldp plan CONFIG --window START/END`` prints for the data interval of the DAG run.
    """
    from local_data_platform.etl import load_config
    from local_data_platform.spec import idempotency_key

    loaded = load_config(config)
    return idempotency_key(loaded, window_of(context))


def _fail_exception() -> type[BaseException]:
    """``AirflowFailException``, which is in ``airflow.sdk.exceptions`` from Airflow 3.2 and in ``airflow.exceptions``
    before that. The old place is deprecated where the new one exists, so the new one is tried first."""
    try:
        from airflow.sdk.exceptions import AirflowFailException
    except ImportError:
        from airflow.exceptions import AirflowFailException
    return AirflowFailException


@functools.lru_cache(maxsize=None)
def _operator_class(base: type) -> type:
    """The operator class over an Airflow ``BaseOperator``. There is one for each base that is ever given."""

    class LdpRunOperator(base):  # type: ignore[misc, valid-type]
        """Run a dataset config for the data interval of a DAG run, as ``ldp run --window`` would.

        The window is ``(data_interval_start, data_interval_end)``, the attempt is ``ti.try_number``, and the run id is
        a stable string of the DAG id, the run id of the DAG run and the task id, so a retry of the task fences the try
        before it and a second DAG run over the same interval writes nothing. A data gate that holds the run back, and
        a config that cannot run, fail the task with ``AirflowFailException``, which Airflow does not retry. The
        return value, which is the XCom of the task, is the ``PipelineResult`` of the run as a dict.

        Args:
            config: The path of a dataset config. A relative path is read from the folder the worker runs in, so give
                an absolute one. It is templated.
            mode: A write mode that overrides the one of the target, ``append``, ``overwrite`` or ``upsert``. It is
                templated.
            **kwargs: The arguments of Airflow's ``BaseOperator``, such as ``task_id`` and ``retries``.
        """

        template_fields = ("config", "mode")

        def __init__(self, *, config: "str | os.PathLike", mode: str | None = None, **kwargs: Any):
            super().__init__(**kwargs)
            self.config = os.fspath(config)
            self.mode = mode

        def execute(self, context: Any) -> dict[str, Any]:
            try:
                options = run_options(context, self.mode)
                start, end = options["window"]
                self.log.info("Running %s for the data interval %s/%s as run %s, attempt %d", self.config,
                              start.isoformat(), end.isoformat(), options["run_id"], options["attempt"])
                result = run(self.config, context, self.mode)
            except DataQualityError as error:
                raise _fail_exception()(f"A data gate held back {self.config}, so the task fails and is not "
                                        f"retried: {error}") from error
            except ConfigError as error:
                raise _fail_exception()(f"{self.config} cannot run for this task, so the task fails and is not "
                                        f"retried: {error}") from error
            self.log.info("%s: %s, key %s, snapshot %s", self.config, result.status, result.idempotency_key,
                          result.published_snapshot_id)
            return result.to_dict()

    LdpRunOperator.__module__ = __name__
    LdpRunOperator.__qualname__ = "LdpRunOperator"
    return LdpRunOperator


def __getattr__(name: str) -> Any:
    """``LdpRunOperator``, built over Airflow's ``BaseOperator`` the first time it is used."""
    if name == "LdpRunOperator":
        try:
            from airflow.sdk import BaseOperator
        except ImportError as error:
            raise ImportError(_INSTALL_HINT) from error
        return _operator_class(BaseOperator)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), "LdpRunOperator"])


__all__ = ["LdpRunOperator", "NoDataInterval", "attempt_of", "key_for", "run", "run_id_of", "run_options",  # noqa: F822
           "window_of"]
