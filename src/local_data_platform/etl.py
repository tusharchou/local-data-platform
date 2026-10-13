"""Run a dataset config end to end: ``run_config("config.json")``.

This is the Python equivalent of ``ldp run CONFIG``. The pipeline is picked by
:func:`local_data_platform.pipeline.registry.create_pipeline` from the config's
source and target formats.

Pass a logical ``window`` (or an explicit ``idempotency_key``) to write through the
staged, exactly-once publish protocol: re-running the same config over the same
window finds the published snapshot and writes nothing::

    run_config("rides.json", window="2026-09-01/2026-09-02")   # publishes
    run_config("rides.json", window="2026-09-01/2026-09-02")   # status "skipped_duplicate"

A scheduler that retries a run passes the run id of the first attempt and the next attempt
number, and the new attempt fences the older ones::

    run_config("rides.json", window="2026-09-01/2026-09-02", run_id=run_id, attempt=2)

A window is also what a config's ``metadata.source.path`` placeholders and ``metadata.window.column`` read, so a
daily run loads the file or the rows of its own day. The shortcuts ``yesterday``, ``last-week`` and ``last-month``
stand for a window that is worked out in the time zone of the config's ``metadata.window``::

    run_config("orders.json", window="yesterday")

A staged run looks for its key in the whole retained history of the target before it reads its source, so a window that
ran 8 days ago, or 600 commits ago, is skipped without being read. A batch of runs, such as a backfill of two years
of days, also bounds the search inside each publish with :func:`batch_search`::

    since_ms, policy = batch_search(len(windows))
    for window in windows:
        run_config("orders.json", window=window, policy=policy, search_since_ms=since_ms)
"""

import dataclasses
import datetime as dt
import os
import time
from typing import Any

from local_data_platform.config import Config
from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

IDEMPOTENCY_HORIZON_DAYS = 7
"""How far back the publish loop of a staged run looks for a rival's commit of its key, as the table property
``ldp.idempotency.horizon-days`` has it. A run looks for an earlier commit of its key in the whole retained history
before that, see :func:`run_config`."""

BATCH_SKEW_S = 600
"""The clock skew, in seconds, that a batch allows between the machines that write one table. The key search of a
batch starts this long before the batch does."""


def load_config(path_or_config: Config | str | os.PathLike | dict[str, Any]) -> Config:
    """Return a :class:`Config` from a config, a JSON file path, or a config dict.

    Args:
        path_or_config: A ``Config``; a path to a JSON config (its paths resolve
            against the file's folder); or a dict (its paths resolve against the cwd).

    Returns:
        The config, validated.

    Raises:
        ConfigError: If the file is missing or invalid.
        TypeError: If the argument is none of the accepted types.
    """
    if isinstance(path_or_config, Config):
        path_or_config.validate()
        return path_or_config
    if isinstance(path_or_config, dict):
        return Config.from_dict(path_or_config)
    if isinstance(path_or_config, (str, os.PathLike)):
        return Config.from_json(path_or_config)
    raise TypeError(f"expected a Config, a config file path or a dict, got {type(path_or_config).__name__}")


def commit_context(config: Config, *, window: Any = None, idempotency_key: str | None = None,
                   run_id: str | None = None, attempt: int = 1, horizon_days: float = IDEMPOTENCY_HORIZON_DAYS,
                   now: dt.datetime | None = None, search_since_ms: int | None = None) -> Any:
    """Build the :class:`~local_data_platform.format.iceberg.commit.CommitContext` for a local staged run.

    Args:
        config: The dataset config.
        window: The run's logical window, in any form
            :func:`~local_data_platform.spec.parse_window` accepts. A shortcut such as ``yesterday`` is counted in
            the time zone of the config's ``metadata.window``.
        idempotency_key: Use this key instead of deriving one from the config and
            window with :func:`~local_data_platform.spec.idempotency_key`.
        run_id: The run id; a new UUIDv7 by default.
        attempt: The attempt number.
        horizon_days: How far back the publish loop searches ``main`` for a rival's commit of the key. A
            local run has no run ledger, so it searches the whole idempotency horizon
            (7 days, the ``ldp.idempotency.horizon-days`` default) rather than since its first attempt.
            :func:`run_config` looks for an earlier commit of the key in the whole retained history before that.
        now: The current time, for tests.
        search_since_ms: Epoch milliseconds that end the search of the publish loop, instead of ``horizon_days`` back
            from now. A batch of runs passes the start of the batch minus the clock skew, see :func:`batch_search`.

    Returns:
        The commit context.

    Raises:
        ConfigError: If the window or key is invalid, ``search_since_ms`` is not a number of epoch milliseconds, or
            this build has no staged publish protocol.
    """
    from local_data_platform.events import uuid7
    from local_data_platform.spec import idempotency_key as derive_key
    from local_data_platform.spec import spec_hash, window_bounds

    try:
        from local_data_platform.format.iceberg.commit import CommitContext
    except ImportError as exc:
        raise ConfigError("staged publishing (window / idempotency key) needs "
                          "local_data_platform.format.iceberg.commit, which this build doesn't have") from exc
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
        raise ConfigError(f"idempotency_key must be a non-empty string, got {idempotency_key!r}")
    if search_since_ms is not None and (not isinstance(search_since_ms, int) or isinstance(search_since_ms, bool)
                                        or search_since_ms < 0):
        raise ConfigError(f"search_since_ms must be a number of epoch milliseconds from 0, got {search_since_ms!r}")
    bounds = window_bounds(config, window, now=now)
    key = idempotency_key.strip() if idempotency_key else derive_key(config, bounds)
    now_ms = int((now.timestamp() if now is not None else time.time()) * 1000)
    return CommitContext(
        run_id=run_id or uuid7(),
        attempt=int(attempt),
        idempotency_key=key,
        spec_hash=spec_hash(config),
        search_since_ms=search_since_ms if search_since_ms is not None
        else max(0, now_ms - int(horizon_days * 86_400_000)),
        logical_window=bounds,
    )


def batch_search(windows: int, *, started: dt.datetime | None = None, policy: Any = None,
                 snapshots_per_run: int = 2, skew_s: float = BATCH_SKEW_S) -> tuple[int, Any]:
    """The key search for a batch of runs on one table: ``(search_since_ms, policy)``.

    A batch is many windows run one after another, such as a backfill of two years of days. Each run adds snapshots to
    the table, and the publish loop of the next run walks back over them looking for its key. It stops at its time
    bound or its depth. With the defaults, 7 days and 500 snapshots, the 501st daily window of a batch fails closed:
    every snapshot of the batch is newer than 7 days, so the walk never reaches the time bound, and 500 snapshots are
    not enough to reach the start of the table. The key of a window that ran before the batch was already found,
    without a bound, before the run read its source, so the walk only has to cover what the batch itself commits. A
    batch therefore searches back to its own start, less the clock skew, and its depth holds every snapshot it can add.

    Args:
        windows: How many runs the batch has.
        started: When the batch starts. The clock by default.
        policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` to raise the depth of. Its
            defaults when ``None``. Its ``max_search_depth`` stays what it is for the writes that are not the batch's,
            and the depth that the batch needs is added to it.
        snapshots_per_run: How many snapshots one run can add to ``main``: 2 for an ``overwrite`` or an ``upsert``,
            which add a delete and an append, and 1 for an ``append``. The default is the larger of the two.
        skew_s: The clock skew to allow, in seconds.

    Returns:
        ``search_since_ms``, for :func:`commit_context` and :func:`run_config`, and the policy with the raised
        ``max_search_depth``, for ``policy=`` of :func:`run_config`.

    Raises:
        ConfigError: If ``windows`` or ``snapshots_per_run`` is not a whole number, or ``skew_s`` is negative.
    """
    from local_data_platform.format.iceberg.commit import CommitPolicy

    for name, value, least in (("windows", windows, 0), ("snapshots_per_run", snapshots_per_run, 1)):
        if not isinstance(value, int) or isinstance(value, bool) or value < least:
            raise ConfigError(f"{name} must be a whole number from {least}, got {value!r}")
    if not isinstance(skew_s, (int, float)) or isinstance(skew_s, bool) or skew_s < 0:
        raise ConfigError(f"skew_s must be a number of seconds from 0, got {skew_s!r}")
    if policy is not None and not isinstance(policy, CommitPolicy):
        raise ConfigError(f"policy must be a CommitPolicy, got {type(policy).__name__}")
    begin = started if started is not None else dt.datetime.now(dt.timezone.utc)
    begin = begin.replace(tzinfo=dt.timezone.utc) if begin.tzinfo is None else begin
    policy = policy or CommitPolicy()
    # Every run of the batch adds up to snapshots_per_run snapshots, and the first run of a new table adds one more,
    # the empty ldp.init snapshot that a staged write branches from.
    depth = policy.max_search_depth + windows * snapshots_per_run + 1
    since_ms = max(0, int(begin.timestamp() * 1000) - int(skew_s * 1000))
    return since_ms, dataclasses.replace(policy, max_search_depth=depth)


def run_config(path_or_config: Config | str | os.PathLike | dict[str, Any], mode: str | None = None, *,
               commit: Any = None, sink: Any = None, window: Any = None, idempotency_key: str | None = None,
               run_id: str | None = None, attempt: int = 1, policy: Any = None, search_since_ms: int | None = None):
    """Build and run the pipeline for a config.

    Args:
        path_or_config: A ``Config``, a path to a JSON config file, or a config dict.
        mode: Override the target's write mode for this run (Iceberg targets:
            ``append``, ``overwrite`` or ``upsert``).
        commit: A ready :class:`~local_data_platform.format.iceberg.commit.CommitContext`;
            writes through the staged publish protocol.
        sink: Where the run's events go, instead of the config's
            ``metadata.observability`` sinks.
        window: A logical window: ``"START/END"``, a ``(start, end)`` pair, or ``yesterday``, ``last-week`` or
            ``last-month`` in the time zone of the config. With ``window`` or ``idempotency_key`` (and no
            ``commit``), the run writes through the staged protocol with a context from
            :func:`commit_context`, so re-running it is a no-op. The window also picks what the run reads: the
            source path of the window, when ``metadata.source.path`` has ``{window_start:FORMAT}`` placeholders,
            and only the rows in the window, when the config names a ``metadata.window.column``. With a window
            column and the write mode ``overwrite``, the run replaces only the rows of the window.
        idempotency_key: An explicit idempotency key, e.g. from an orchestrator.
        run_id: The id of the run, for a retry: the next attempt reuses the id of the one before it. A new
            UUIDv7 by default. It belongs to a staged run, so it needs ``window`` or ``idempotency_key``.
        attempt: The attempt number, from 1. An attempt above 1 needs ``run_id``, and it fences every older
            attempt of the run, so a staged branch that one of them left never reaches ``main``.
        policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` of the write: its publish
            attempts, its deadline and the number of snapshots that the publish loop may walk back over to look for
            the key. The defaults when ``None``. A batch uses :func:`batch_search`.
        search_since_ms: For a staged run built from ``window`` or ``idempotency_key``, the epoch milliseconds that end
            the search of the publish loop, instead of 7 days back. See :func:`commit_context` and
            :func:`batch_search`.

    A staged run looks for its key in the whole retained history of the target before it reads its source. When the key
    is there, the result is ``skipped_duplicate`` with the snapshot that carries it, ``rows_read`` is 0, and the run
    emits ``run.started``, ``run.skipped_duplicate`` and ``run.finished`` only. That holds for a window that ran 8 days
    ago or 600 commits ago, for as long as the table keeps its snapshot, which ``ldp maintain --expire`` does for the
    idempotency horizon. When the key is not there and the window is older than every window that the table still holds
    a key for, the run logs a warning that the key may have expired.

    Returns:
        The :class:`~local_data_platform.pipeline.PipelineResult`.

    Raises:
        ConfigError: If the config is missing or invalid, ``attempt`` is not a whole number from 1, ``attempt``
            is above 1 without ``run_id``, ``run_id`` or ``attempt`` come without a window or key, ``commit``
            is given together with ``window``, ``idempotency_key``, ``run_id`` or ``attempt``, or the source path
            has window placeholders and the run has no window, ``search_since_ms`` comes without a window or key or
            together with ``commit``, or ``policy`` is not a ``CommitPolicy``.
        PipelineNotFound: If no pipeline is registered for the config's route.
        DataQualityError: If a quality check fails and ``on_failure`` is ``"fail"``;
            nothing is written.
    """
    from local_data_platform.orchestration.windows import lacks_window_selection
    from local_data_platform.pipeline.registry import create_pipeline
    from local_data_platform.spec import window_bounds

    config = load_config(path_or_config)
    if policy is not None:
        from local_data_platform.format.iceberg.commit import CommitPolicy

        if not isinstance(policy, CommitPolicy):
            raise ConfigError(f"policy must be a CommitPolicy, got {type(policy).__name__}")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ConfigError(f"attempt must be a whole number from 1, got {attempt!r}")
    if attempt > 1 and run_id is None:
        raise ConfigError("attempt needs a run_id: a retry reuses the run id of the attempt before it")
    staged = window is not None or idempotency_key is not None
    retry = run_id is not None or attempt > 1
    if commit is not None and (staged or retry or search_since_ms is not None):
        raise ConfigError("pass either commit= or window=/idempotency_key=/run_id=/attempt=/search_since_ms=, "
                          "not both")
    if search_since_ms is not None and not staged:
        raise ConfigError("search_since_ms belongs to a staged run, so give a window or an idempotency_key too")
    bounds = None  # the window as UTC bounds; a shortcut is worked out once, in the config's time zone
    if commit is None and staged:
        bounds = window_bounds(config, window)
        commit = commit_context(config, window=bounds, idempotency_key=idempotency_key, run_id=run_id,
                                attempt=attempt, search_since_ms=search_since_ms)
    elif retry and commit is None:
        raise ConfigError("run_id and attempt belong to a staged run, so give a window or an idempotency_key too")
    elif commit is not None:
        bounds = getattr(commit, "logical_window", None)  # a ready context brings its window along
    if bounds is not None and lacks_window_selection(config.metadata, config.when, mode):
        logger.warning("config %s has a schedule and appends, but nothing selects the rows of a window, so every "
                       "window loads the same rows again; put {window_start:FORMAT} in metadata.source.path or set "
                       "metadata.window.column", config.identifier)
    logger.info("Running config %s", config.identifier)
    return create_pipeline(config, window=bounds).run(mode=mode, commit=commit, sink=sink, policy=policy)


__all__ = ["BATCH_SKEW_S", "IDEMPOTENCY_HORIZON_DAYS", "batch_search", "commit_context", "load_config", "run_config"]
