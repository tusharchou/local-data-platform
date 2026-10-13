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
"""

import datetime as dt
import os
import time
from typing import Any

from local_data_platform.config import Config
from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

IDEMPOTENCY_HORIZON_DAYS = 7
"""How far back a local staged run looks for an earlier commit of its key (``ldp.idempotency.horizon-days``)."""


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
                   now: dt.datetime | None = None) -> Any:
    """Build the :class:`~local_data_platform.format.iceberg.commit.CommitContext` for a local staged run.

    Args:
        config: The dataset config.
        window: The run's logical window, in any form
            :func:`~local_data_platform.spec.parse_window` accepts.
        idempotency_key: Use this key instead of deriving one from the config and
            window with :func:`~local_data_platform.spec.idempotency_key`.
        run_id: The run id; a new UUIDv7 by default.
        attempt: The attempt number.
        horizon_days: How far back ``find_commit`` searches ``main`` for the key. A
            local run has no run ledger, so it searches the whole idempotency horizon
            (7 days, the ``ldp.idempotency.horizon-days`` default) rather than since
            its first attempt: re-running a window within the horizon is a no-op.
        now: The current time, for tests.

    Returns:
        The commit context.

    Raises:
        ConfigError: If the window or key is invalid, or this build has no staged
            publish protocol.
    """
    from local_data_platform.events import uuid7
    from local_data_platform.spec import idempotency_key as derive_key
    from local_data_platform.spec import parse_window, spec_hash

    try:
        from local_data_platform.format.iceberg.commit import CommitContext
    except ImportError as exc:
        raise ConfigError("staged publishing (window / idempotency key) needs "
                          "local_data_platform.format.iceberg.commit, which this build doesn't have") from exc
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
        raise ConfigError(f"idempotency_key must be a non-empty string, got {idempotency_key!r}")
    bounds = parse_window(window)
    key = idempotency_key.strip() if idempotency_key else derive_key(config, bounds)
    now_ms = int((now.timestamp() if now is not None else time.time()) * 1000)
    return CommitContext(
        run_id=run_id or uuid7(),
        attempt=int(attempt),
        idempotency_key=key,
        spec_hash=spec_hash(config),
        search_since_ms=max(0, now_ms - int(horizon_days * 86_400_000)),
        logical_window=bounds,
    )


def run_config(path_or_config: Config | str | os.PathLike | dict[str, Any], mode: str | None = None, *,
               commit: Any = None, sink: Any = None, window: Any = None, idempotency_key: str | None = None,
               run_id: str | None = None, attempt: int = 1):
    """Build and run the pipeline for a config.

    Args:
        path_or_config: A ``Config``, a path to a JSON config file, or a config dict.
        mode: Override the target's write mode for this run (Iceberg targets:
            ``append``, ``overwrite`` or ``upsert``).
        commit: A ready :class:`~local_data_platform.format.iceberg.commit.CommitContext`;
            writes through the staged publish protocol.
        sink: Where the run's events go, instead of the config's
            ``metadata.observability`` sinks.
        window: A logical window. With ``window`` or ``idempotency_key`` (and no
            ``commit``), the run writes through the staged protocol with a context from
            :func:`commit_context`, so re-running it is a no-op.
        idempotency_key: An explicit idempotency key, e.g. from an orchestrator.
        run_id: The id of the run, for a retry: the next attempt reuses the id of the one before it. A new
            UUIDv7 by default. It belongs to a staged run, so it needs ``window`` or ``idempotency_key``.
        attempt: The attempt number, from 1. An attempt above 1 needs ``run_id``, and it fences every older
            attempt of the run, so a staged branch that one of them left never reaches ``main``.

    Returns:
        The :class:`~local_data_platform.pipeline.PipelineResult`.

    Raises:
        ConfigError: If the config is missing or invalid, ``attempt`` is not a whole number from 1, ``attempt``
            is above 1 without ``run_id``, ``run_id`` or ``attempt`` come without a window or key, or ``commit``
            is given together with ``window``, ``idempotency_key``, ``run_id`` or ``attempt``.
        PipelineNotFound: If no pipeline is registered for the config's route.
        DataQualityError: If a quality check fails and ``on_failure`` is ``"fail"``;
            nothing is written.
    """
    from local_data_platform.pipeline.registry import create_pipeline

    config = load_config(path_or_config)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ConfigError(f"attempt must be a whole number from 1, got {attempt!r}")
    if attempt > 1 and run_id is None:
        raise ConfigError("attempt needs a run_id: a retry reuses the run id of the attempt before it")
    staged = window is not None or idempotency_key is not None
    retry = run_id is not None or attempt > 1
    if commit is not None and (staged or retry):
        raise ConfigError("pass either commit= or window=/idempotency_key=/run_id=/attempt=, not both")
    if commit is None and staged:
        commit = commit_context(config, window=window, idempotency_key=idempotency_key, run_id=run_id,
                                attempt=attempt)
    elif retry and commit is None:
        raise ConfigError("run_id and attempt belong to a staged run, so give a window or an idempotency_key too")
    logger.info("Running config %s", config.identifier)
    return create_pipeline(config).run(mode=mode, commit=commit, sink=sink)


__all__ = ["IDEMPOTENCY_HORIZON_DAYS", "commit_context", "load_config", "run_config"]
