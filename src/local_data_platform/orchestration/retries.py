"""Retries: how often a failed task runs again, how long it waits, and which errors are worth another try.

A workflow, and each task of it, may say::

    "retries": {"max_attempts": 3, "delay_s": 30, "backoff": 2.0}

``max_attempts`` is the number of attempts in all, so 1 means no retry, and 1 is the default. ``delay_s`` is the longest
wait before the first retry, 30 seconds by default, and ``backoff`` multiplies it for each retry after that, 2.0 by
default. The wait is a full jitter: a random time between 0 and ``delay_s * backoff ** (n - 1)`` seconds after the n-th
attempt, so tasks that failed together do not try again together. No wait is longer than a day. A setting of a task
overrides the same setting of the workflow, key by key, so a task that sets only ``max_attempts`` keeps the delay of the
workflow.

A retry is the next attempt of the same run. The runner keeps the run id of the task and counts the attempt up, and a
pipeline task runs with the same idempotency key, so the new attempt fences every older one, as ``docs/exactly_once.md``
describes under "Retries and zombies". A retry of a load that failed after its data was written finds the key and writes
nothing.

Only errors that another attempt can fix are retried: :func:`is_retriable` says which. A quality check that blocked a
write, a config that is wrong and a fenced attempt are never retried, because trying again cannot change them.

This module needs only the standard library when it is imported. pyiceberg is imported, when it is installed, the first
time an error is classified.
"""

import functools
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from local_data_platform.exceptions import ConfigError, DataQualityError, PipelineNotFound

RETRY_KEYS = ("max_attempts", "delay_s", "backoff")
"""The keys of a ``retries`` object."""

DEFAULT_DELAY_S = 30.0
"""The ``delay_s`` of a ``retries`` object that does not set one."""

DEFAULT_BACKOFF = 2.0
"""The ``backoff`` of a ``retries`` object that does not set one."""

MAX_DELAY_S = 86400.0
"""The longest wait between two attempts, whatever the delay and the backoff come to."""


@dataclass(frozen=True)
class RetryPolicy:
    """How a task is retried.

    Attributes:
        max_attempts: The attempts in all, the first one included. 1 means the task is not retried.
        delay_s: The longest wait before the first retry, in seconds.
        backoff: The factor that the longest wait grows by for each retry after the first.
    """

    max_attempts: int = 1
    delay_s: float = DEFAULT_DELAY_S
    backoff: float = DEFAULT_BACKOFF

    @classmethod
    def from_settings(cls, *settings: Any) -> "RetryPolicy":
        """The policy that settings give, from the most general to the most specific.

        Each setting is a ``retries`` object or ``None``. A key that a later setting has replaces the key of an earlier
        one, so a task overrides the workflow one key at a time. The objects are taken as they are: check them with
        :func:`retries_problems` first.
        """
        merged: dict[str, Any] = {}
        for setting in settings:
            if isinstance(setting, Mapping):
                merged.update({key: value for key, value in setting.items() if key in RETRY_KEYS})
        return cls(max_attempts=int(merged.get("max_attempts", 1)),
                   delay_s=float(merged.get("delay_s", DEFAULT_DELAY_S)),
                   backoff=float(merged.get("backoff", DEFAULT_BACKOFF)))

    @property
    def enabled(self) -> bool:
        """Whether a failed attempt is followed by another one."""
        return self.max_attempts > 1

    def delay_cap(self, attempt: int) -> float:
        """The longest wait after attempt number ``attempt`` failed, in seconds."""
        try:
            return min(self.delay_s * self.backoff ** (attempt - 1), MAX_DELAY_S)
        except OverflowError:
            return MAX_DELAY_S

    def delay(self, attempt: int, rng: Any = None) -> float:
        """A full-jitter wait after attempt number ``attempt`` failed: a random time from 0 to :meth:`delay_cap`.

        Args:
            attempt: The attempt that failed, from 1.
            rng: Anything with ``uniform(a, b)``, such as a seeded :class:`random.Random`. A new one by default.
        """
        return (rng if rng is not None else random.Random()).uniform(0.0, self.delay_cap(attempt))

    def to_dict(self) -> dict[str, Any]:
        """The policy as the plain object that ``ldp workflow plan --json`` shows."""
        return {"max_attempts": self.max_attempts, "delay_s": self.delay_s, "backoff": self.backoff}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def retries_problems(value: Any) -> list[tuple[str, str]]:
    """The problems of a ``retries`` object, as ``(path, message)``.

    ``path`` is ``""`` for the object itself and ``".max_attempts"``, ``".delay_s"`` or ``".backoff"`` for a key, so a
    caller puts its own path in front of it.
    """
    if not isinstance(value, Mapping):
        return [("", f"must be an object such as {{'max_attempts': 3, 'delay_s': 30}}, got {value!r}")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(set(map(str, value)) - set(RETRY_KEYS))
    if unknown:
        problems.append(("", f"has unknown keys {unknown}; allowed: {list(RETRY_KEYS)}"))
    attempts = value.get("max_attempts")
    if "max_attempts" in value and not (isinstance(attempts, int) and not isinstance(attempts, bool)
                                        and attempts >= 1):
        problems.append((".max_attempts", f"must be a whole number from 1, got {attempts!r}"))
    for key, least in (("delay_s", 0), ("backoff", 1)):
        item = value.get(key)
        if key in value and not (_number(item) and item >= least):
            problems.append((f".{key}", f"must be a number from {least}, got {item!r}"))
    return problems


def retries_schema() -> dict[str, Any]:
    """The JSON Schema of a ``retries`` object."""
    return {
        "type": "object", "additionalProperties": False,
        "description": "How a failed task is tried again. A task's keys override the workflow's.",
        "properties": {
            "max_attempts": {"type": "integer", "minimum": 1, "default": 1,
                             "description": "The attempts in all. 1 means no retry."},
            "delay_s": {"type": "number", "minimum": 0, "default": DEFAULT_DELAY_S,
                        "description": "The longest wait before the first retry, in seconds. The wait is a random "
                                       "time from 0 to this (full jitter)."},
            "backoff": {"type": "number", "minimum": 1, "default": DEFAULT_BACKOFF,
                        "description": "What the longest wait is multiplied by for each retry after the first."},
        },
    }


@functools.cache
def _transient_types() -> tuple[type[BaseException], ...]:
    """The error types that another attempt may fix: ``OSError``, and the commit errors of pyiceberg."""
    types: list[type[BaseException]] = [OSError]
    try:
        from pyiceberg.exceptions import CommitFailedException, CommitStateUnknownException
    except ImportError:  # pyiceberg is not installed, and nothing can raise its errors
        pass
    else:
        types += [CommitFailedException, CommitStateUnknownException]
    return tuple(types)


def is_retriable(error: BaseException) -> bool:
    """Whether another attempt of a task may succeed where this one failed with ``error``.

    These are retried: a ``CommitConflict`` with ``retriable`` true (the publish attempts ran out under contention),
    pyiceberg's ``CommitFailedException`` and ``CommitStateUnknownException``, and any ``OSError``, which covers a
    dropped connection, a timeout, a file that has not landed yet and the connection errors of ``requests``.

    These never are: a ``DataQualityError`` (the data has to change), a ``ConfigError``, a ``PipelineNotFound``, and a
    ``CommitConflict`` with ``retriable`` false (the attempt was fenced by a newer one and must stop). Any other error
    is not retried either, because nothing says another attempt would end differently.
    """
    if isinstance(error, (DataQualityError, ConfigError, PipelineNotFound)):
        return False
    retriable = getattr(error, "retriable", None)
    if isinstance(retriable, bool):  # CommitConflict says it itself
        return retriable
    return isinstance(error, _transient_types())


__all__ = [
    "DEFAULT_BACKOFF", "DEFAULT_DELAY_S", "MAX_DELAY_S", "RETRY_KEYS", "RetryPolicy", "is_retriable",
    "retries_problems", "retries_schema",
]
