"""Pipelines: read a source, transform, check quality, write a target.

A :class:`Pipeline` is built by composition. It *has* a source, a target, a list of
transforms and a list of quality checks::

    from local_data_platform.format.csv import CSV
    from local_data_platform.format.iceberg import Iceberg
    from local_data_platform.pipeline import Pipeline
    from local_data_platform.quality import NotNull

    pipeline = Pipeline(
        source=CSV("rides", "data/rides.csv"),
        target=Iceberg("rides", {"identifier": "nyc", "warehouse_path": "warehouse"}),
        transforms=[lambda df: df.drop_null()],
        checks=[NotNull(["ride_id"])],
    )
    result = pipeline.run()

To build a pipeline from a config, use
:func:`local_data_platform.pipeline.registry.create_pipeline`, which picks the
built-in pipeline class for the config's source and target formats.

``run()`` goes through four steps in order: ``extract`` reads the source,
``transform`` applies the callables in order, ``validate`` runs the checks, and
the target is written. A pipeline built for a ``window`` (see ``docs/design/v0_1_1.md``, "Windows and schedules")
reads the source path of that window, keeps only the rows of the window right after the extract when the config
names a ``metadata.window.column``, replaces only those rows when it overwrites an Iceberg table, and checks
freshness at the end of the window. Checks run *before* the write: with ``on_failure="fail"`` a
failing check raises :class:`~local_data_platform.exceptions.DataQualityError` and
nothing is written; with ``"warn"`` the failures are logged and the write goes ahead.

Every run has a UUIDv7 ``run_id`` and emits :class:`~local_data_platform.events.RunEvent`
records to an event sink: ``run.started``, ``run.extracted``, ``quality.evaluated``, then
one of ``run.published``, ``run.skipped_duplicate``, ``run.blocked_quality`` or
``run.failed``, and finally ``run.finished``. The sink comes from ``run(sink=...)``, the
``sink=`` constructor argument, or the config's ``metadata.observability`` block; the
default drops every event. ``run(commit=CommitContext(...))`` writes through the staged,
exactly-once publish protocol (see ``docs/exactly_once.md``). A staged run first looks for its idempotency key in the
whole retained history of the target, and when it is there the run reads nothing: it emits ``run.started``,
``run.skipped_duplicate`` and ``run.finished`` only.
"""

import dataclasses
import datetime as dt
import inspect
import time
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from local_data_platform import Config, Flow
from local_data_platform.events import (
    EventSink,
    RunEmitter,
    dataset_ref,
    quality_payload,
    redact_text,
    schema_payload,
    sink_from_config,
    uuid7,
)
from local_data_platform.exceptions import ConfigError, DataQualityError
from local_data_platform.logger import get_logger
from local_data_platform.orchestration import windows
from local_data_platform.quality import Check, QualityReport, checks_from_config, run_checks

logger = get_logger(__name__)

ON_FAILURE = ("fail", "warn")
"""The values ``on_failure`` accepts."""

Transform = Callable[[pa.Table], pa.Table]
"""A transform takes the extracted ``pyarrow.Table`` and returns a new one."""


@dataclass
class PipelineResult:
    """What a :meth:`Pipeline.run` call did.

    Attributes:
        name: The pipeline's name (the config's ``identifier`` when built from a config).
        rows_read: Rows the source returned, before any transform. 0 for a run that found its key already
            published and so did not read the source.
        rows_written: Rows the target write added or changed. For an Iceberg upsert,
            input rows identical to the stored ones are not rewritten and not counted.
        write_result: What the target's ``put`` returned: a
            :class:`~local_data_platform.format.iceberg.WriteResult` for Iceberg
            targets, ``None`` for file targets.
        quality: The quality report for the batch. It is empty (and passed) when the
            pipeline has no checks.
        duration_s: Wall-clock seconds the run took.
        run_id: The run's UUIDv7 string; every event of the run carries it. With a
            ``commit`` context it is the context's ``run_id``.
        idempotency_key: The run's idempotency key when it wrote through the staged
            protocol (``run(commit=...)``), else ``None``.
        published_snapshot_id: The Iceberg snapshot holding this run's effect: the one
            it published, or for a skipped duplicate the earlier one that already
            carries the key. ``None`` for file targets.
        status: ``"published"``, or ``"skipped_duplicate"`` when the staged protocol
            found the key already on ``main`` and wrote nothing.
        rows_kept: The rows of ``rows_read`` that lie in the window, when the config names a
            ``metadata.window.column`` and the run had a window. ``None`` otherwise.
    """

    name: str
    rows_read: int
    rows_written: int
    write_result: Any = None
    quality: QualityReport = field(default_factory=QualityReport)
    duration_s: float = 0.0
    run_id: str | None = None
    idempotency_key: str | None = None
    published_snapshot_id: int | None = None
    status: str = "published"
    rows_kept: int | None = None

    @property
    def skipped_duplicate(self) -> bool:
        """``True`` when the run found its idempotency key already published and wrote nothing."""
        return self.status == "skipped_duplicate"

    def to_dict(self) -> dict[str, Any]:
        """Return the result as JSON-serialisable primitives."""
        write_result = self.write_result
        if dataclasses.is_dataclass(write_result) and not isinstance(write_result, type):
            write_result = dataclasses.asdict(write_result)
        return {
            "name": self.name,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "write_result": write_result,
            "quality": self.quality.to_dict(),
            "duration_s": round(self.duration_s, 3),
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "published_snapshot_id": self.published_snapshot_id,
            "status": self.status,
            "rows_kept": self.rows_kept,
        }

    def __str__(self) -> str:
        kept = f", kept {self.rows_kept} in the window" if self.rows_kept is not None else ""
        if self.skipped_duplicate:
            read = "" if self.rows_read == 0 else f" read {self.rows_read} rows{kept},"
            return (f"{self.name}:{read} skipped: idempotency key already published in "
                    f"snapshot {self.published_snapshot_id} ({self.duration_s:.2f}s)")
        text = (f"{self.name}: read {self.rows_read} rows{kept}, wrote {self.rows_written} rows in "
                f"{self.duration_s:.2f}s")
        if len(self.quality):
            text += f" (quality: {len(self.quality) - len(self.quality.failures)} of {len(self.quality)} checks passed)"
        return text


class Pipeline(Flow):
    """Extract from a source, transform, check quality, then load into a target.

    Args:
        config: A dataset config. Subclasses build the source and target from it
            (see :meth:`build_source` and :meth:`build_target`), and its ``quality``
            block supplies the checks and ``on_failure``.
        source: Anything with ``get() -> pyarrow.Table``. Overrides the config.
        target: Anything with ``put(df)``, such as ``CSV``, ``Parquet`` or ``Iceberg``.
            Overrides the config.
        transforms: A callable or a list of callables, each ``pyarrow.Table ->
            pyarrow.Table``, applied in order after the extract.
        checks: Quality checks (:class:`~local_data_platform.quality.Check` instances
            or config dicts). Overrides the config's ``quality.checks``.
        on_failure: ``"fail"`` (the default) raises
            :class:`~local_data_platform.exceptions.DataQualityError` and writes
            nothing when a check fails; ``"warn"`` logs the failures and writes anyway.
            Overrides the config's ``quality.on_failure``.
        name: Name used in logs and in :class:`PipelineResult`. Defaults to the
            config's ``identifier``, else the target's name.
        sink: The default :class:`~local_data_platform.events.EventSink` for
            :meth:`run`. Defaults to the one the config's ``metadata.observability``
            block describes, else a :class:`~local_data_platform.events.NullSink`.
        window: The logical window the pipeline is built for: ``(start, end)``, ``"START/END"`` or a shortcut such
            as ``"yesterday"``, as :func:`~local_data_platform.spec.parse_window` reads it. The source path of the
            config is read for it (``{window_start:FORMAT}`` placeholders), and :meth:`run` keeps its rows. The
            window is :attr:`window`, as UTC datetimes.

    Raises:
        ConfigError: If there is no source or target, ``on_failure`` is invalid, a check config or the
            observability block is invalid, the window is malformed, or the source path has window placeholders
            and there is no window.
        TypeError: If the source, target, a transform or the sink has the wrong shape.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        source: Any = None,
        target: Any = None,
        transforms: Transform | Iterable[Transform] | None = None,
        checks: Iterable[Check | Mapping[str, Any]] | Check | None = None,
        on_failure: str | None = None,
        name: str | None = None,
        sink: EventSink | None = None,
        window: Any = None,
    ):
        if config is not None and not isinstance(config, Config):
            raise TypeError(f"config must be a local_data_platform.Config, got {type(config).__name__}; "
                            "load one with Config.from_json(path)")
        self.config = config
        self.window = self._check_window(window, config)
        quality = config.quality if config is not None else {"on_failure": "fail", "checks": []}

        # Validate the cheap, side-effect-free parts first: building an Iceberg target creates its catalog.
        self.transforms = self._check_transforms(transforms)
        self.checks = self._check_checks(checks if checks is not None else quality["checks"])
        self.on_failure = self._check_on_failure(on_failure if on_failure is not None else quality["on_failure"])
        self.sink = self._check_sink(sink) if sink is not None else sink_from_config(config)

        self.source = source if source is not None else self._build(
            windows.bind_config(config, self.window), self.build_source, "source")
        self.target = target if target is not None else self._build(config, self.build_target, "target")
        if not callable(getattr(self.source, "get", None)):
            raise TypeError(f"source must have a get() method, got {type(self.source).__name__}")
        if not callable(getattr(self.target, "put", None)):
            raise TypeError(f"target must have a put() method, got {type(self.target).__name__}")

        self.name = (name or (config.identifier if config is not None else None)
                     or getattr(self.target, "name", None) or type(self).__name__)
        logger.debug("%s %r: source %r, target %r, %d transforms, %d checks (on_failure=%s)",
                     type(self).__name__, self.name, self.source, self.target, len(self.transforms),
                     len(self.checks), self.on_failure)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, source={self.source!r}, target={self.target!r})"

    # ------------------------------------------------------------------ building

    def build_source(self, config: Config) -> Any:
        """Build the source from ``config``. Built-in pipelines override this.

        Raises:
            ConfigError: Always, for the base class: pass ``source=`` instead, or use
                :func:`~local_data_platform.pipeline.registry.create_pipeline`.
        """
        raise ConfigError(f"{type(self).__name__} cannot build a source from a config; pass source=, or use "
                          "create_pipeline(config) to pick a built-in pipeline")

    def build_target(self, config: Config) -> Any:
        """Build the target from ``config``. Built-in pipelines override this.

        Raises:
            ConfigError: Always, for the base class: pass ``target=`` instead, or use
                :func:`~local_data_platform.pipeline.registry.create_pipeline`.
        """
        raise ConfigError(f"{type(self).__name__} cannot build a target from a config; pass target=, or use "
                          "create_pipeline(config) to pick a built-in pipeline")

    def _build(self, config: Config | None, builder: Callable[[Config], Any], role: str) -> Any:
        if config is None:
            raise ConfigError(f"{type(self).__name__} needs a {role}: pass {role}= or a config")
        return builder(config)

    @staticmethod
    def _check_window(window: Any, config: Config | None) -> tuple[Any, Any] | None:
        if window is None:
            return None
        from local_data_platform.spec import window_bounds

        return window_bounds(config, window)

    @staticmethod
    def _check_transforms(transforms: Transform | Iterable[Transform] | None) -> list[Transform]:
        if transforms is None:
            return []
        items = [transforms] if callable(transforms) else list(transforms)
        for position, item in enumerate(items):
            if not callable(item):
                raise TypeError(f"transforms[{position}] must be a callable taking and returning a pyarrow.Table, "
                                f"got {type(item).__name__}")
        return items

    @staticmethod
    def _check_checks(checks: Iterable[Check | Mapping[str, Any]] | Check | None) -> list[Check]:
        return checks_from_config([checks] if isinstance(checks, Check) else checks)

    @staticmethod
    def _check_sink(sink: Any) -> EventSink:
        if not (callable(getattr(sink, "emit", None)) and callable(getattr(sink, "flush", None))):
            raise TypeError(f"sink must have emit(event) and flush() methods, got {type(sink).__name__}")
        return sink

    @staticmethod
    def _check_on_failure(on_failure: str) -> str:
        normalised = on_failure.strip().lower() if isinstance(on_failure, str) else on_failure
        if normalised not in ON_FAILURE:
            raise ConfigError(f"on_failure must be 'fail' or 'warn', got {on_failure!r}")
        return normalised

    # ------------------------------------------------------------------ steps

    def extract(self) -> pa.Table:
        """Read the source.

        Returns:
            The source data as a ``pyarrow.Table``.

        Raises:
            TypeError: If the source returns something other than Arrow data.
        """
        df = self.source.get()
        if isinstance(df, pa.RecordBatch):
            df = pa.Table.from_batches([df])
        if not isinstance(df, pa.Table):
            raise TypeError(f"source {self.source!r} returned {type(df).__name__}, expected a pyarrow.Table")
        logger.info("Pipeline %s: extracted %d rows from %r", self.name, df.num_rows, self.source)
        return df

    def transform(self, df: pa.Table) -> pa.Table:
        """Apply the transforms to ``df`` in order.

        Args:
            df: The extracted data.

        Returns:
            The transformed data. With no transforms this is ``df`` itself.

        Raises:
            TypeError: If a transform returns something other than a ``pyarrow.Table``.
        """
        for position, step in enumerate(self.transforms):
            df = step(df)
            if isinstance(df, pa.RecordBatch):
                df = pa.Table.from_batches([df])
            if not isinstance(df, pa.Table):
                label = getattr(step, "__name__", repr(step))
                raise TypeError(f"transform #{position + 1} ({label}) returned {type(df).__name__}, "
                                "expected a pyarrow.Table")
        if self.transforms:
            logger.info("Pipeline %s: applied %d transforms, %d rows", self.name, len(self.transforms), df.num_rows)
        return df

    def validate(self, df: pa.Table, context: Mapping[str, Any] | None = None) -> QualityReport:
        """Run the quality checks on ``df`` and apply ``on_failure``.

        Args:
            df: The transformed data, about to be written.
            context: Facts about the load passed to every check, such as
                ``{"source_rows": 1000}`` for ``RowCount(equals="source")``.

        Returns:
            The quality report. It is empty when the pipeline has no checks.

        Raises:
            DataQualityError: If a check failed and ``on_failure`` is ``"fail"``.
        """
        report = run_checks(df, self.checks, context)
        if report.passed:
            if len(report):
                logger.info("Pipeline %s: %d of %d quality checks passed", self.name, len(report), len(report))
            return report
        if self.on_failure == "warn":
            logger.warning("Pipeline %s: writing despite failed quality checks (on_failure=warn)\n%s",
                           self.name, report.summary())
            return report
        logger.info("Pipeline %s: %d quality checks failed; nothing was written to %r",
                    self.name, len(report.failures), self.target)
        report.raise_for_failures()
        return report  # pragma: no cover - raise_for_failures() raised

    def write(self, df: pa.Table, mode: str | None = None, *, commit: Any = None, policy: Any = None) -> Any:
        """Write ``df`` to the target.

        In a windowed run of a config that names a ``metadata.window.column``, an ``overwrite`` of an Iceberg table
        replaces only the rows of the window (the ``overwrite_filter`` of ``Iceberg.put``), not the whole table.

        Args:
            df: The validated data.
            mode: The write mode, for targets whose ``put`` takes one (Iceberg:
                ``append``, ``overwrite`` or ``upsert``). ``None`` uses the target's default.
            commit: A :class:`~local_data_platform.format.iceberg.commit.CommitContext`
                to write through the staged publish protocol, for targets whose ``put``
                takes ``commit=`` (Iceberg).
            policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` that bounds the write: its
                publish attempts, deadline and key search depth. The target's own defaults when ``None``, and a
                target whose ``put`` takes no ``policy`` (a CSV or Parquet file) has no use for one.

        Returns:
            Whatever the target's ``put`` returned.

        Raises:
            ConfigError: If ``mode`` is given for a target that always replaces its
                data (CSV, Parquet) and is not ``"overwrite"``, or ``commit`` is given
                for a target that can't take it, or a window overwrite needs a ``metadata.window.column`` that
                ``df`` does not have.
        """
        replace = self._overwrite_options(df, mode, commit)
        takes_policy = policy is not None and self._accepts(self.target, "policy")
        options = {**replace, **({"policy": policy} if takes_policy else {})}
        if not replace:
            return self._put(df, mode, commit, options)
        with warnings.catch_warnings():
            # The first load of a window has no rows to replace, which pyiceberg reports as a warning.
            warnings.filterwarnings("ignore", message="Delete operation did not match any records")
            return self._put(df, mode, commit, options)

    def _put(self, df: pa.Table, mode: str | None, commit: Any, options: Mapping[str, Any]) -> Any:
        if commit is not None:
            self._require_commit_support()
            if mode is None:
                return self.target.put(df, commit=commit, **options)
            return self.target.put(df, mode=mode, commit=commit, **options)
        if mode is None:
            return self.target.put(df, **options)
        if self._accepts_mode(self.target):
            return self.target.put(df, mode=mode, **options)
        if isinstance(mode, str) and mode.strip().lower() == "overwrite":
            return self.target.put(df)
        raise ConfigError(f"target {self.target!r} is always overwritten; write mode {mode!r} only applies to "
                          "Iceberg targets")

    @staticmethod
    def _accepts(target: Any, parameter: str) -> bool:
        try:
            parameters = inspect.signature(target.put).parameters
        except (TypeError, ValueError):
            return False
        return parameter in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())

    @staticmethod
    def _accepts_mode(target: Any) -> bool:
        try:
            return "mode" in inspect.signature(target.put).parameters
        except (TypeError, ValueError):
            return False

    def _require_commit_support(self) -> None:
        if not self._accepts(self.target, "commit"):
            raise ConfigError(f"target {self.target!r} can't write through the staged publish protocol: its put() "
                              "takes no commit=; idempotency keys need an Iceberg target")

    # ------------------------------------------------------------------ windows

    def _run_window(self, commit: Any) -> tuple[Any, Any] | None:
        """The window of a run: the pipeline's own, else the one a commit context carries, else none."""
        carried = getattr(commit, "logical_window", None)
        carried = tuple(_utc(bound) for bound in carried) if carried else None  # a window without a zone is UTC
        if self.window is None:
            return carried
        if carried is not None and carried != self.window:
            raise ConfigError(f"the pipeline was built for the window {_iso(self.window[0])}/{_iso(self.window[1])}, "
                              f"but the commit context is for {_iso(carried[0])}/{_iso(carried[1])}")
        return self.window

    def _window_column(self) -> windows.WindowSpec | None:
        """The ``metadata.window`` of the config when it names a column, else ``None``."""
        declared = self.config.window if self.config is not None else None
        return declared if declared is not None and declared.column is not None else None

    def _select_window(self, df: pa.Table, window: tuple[Any, Any] | None) -> tuple[pa.Table, int | None]:
        """Keep the rows in the window: ``(table, rows_kept)``, or ``(table, None)`` when nothing selects rows."""
        declared = self._window_column()
        if window is None or declared is None:
            return df, None
        kept = windows.filter_rows(df, declared.column, window, declared.tz)
        logger.info("Pipeline %s: kept %d of %d rows in the window %s/%s", self.name, kept.num_rows, df.num_rows,
                    _iso(window[0]), _iso(window[1]))
        return kept, kept.num_rows

    def _overwrite_options(self, df: pa.Table, mode: str | None, commit: Any) -> dict[str, Any]:
        """``{"overwrite_filter": ...}`` when a windowed run overwrites an Iceberg table, else ``{}``."""
        window, declared = self._run_window(commit), self._window_column()
        if window is None or declared is None:
            return {}
        effective = mode if mode is not None else getattr(self.target, "write_mode", None)
        if str(effective or "").strip().lower() != "overwrite" or not self._accepts(self.target, "overwrite_filter"):
            return {}
        if declared.column not in df.column_names:
            raise ConfigError(f"metadata.window.column {declared.column!r} is not a column of the data to write "
                              f"(columns: {', '.join(df.column_names)}); a transform may have dropped or renamed it")
        column_type = df.schema.field(declared.column).type
        return {"overwrite_filter": windows.overwrite_filter(declared.column, window, declared.tz, column_type)}

    def _published_before(self, commit: Any, mode: str | None) -> Any:
        """Look for the key of a staged run in the whole retained history of the target, before the source is read.

        Returns:
            The result of the earlier publish, as a skipped duplicate, or ``None`` when the key is not there. A
            target that cannot look (it has no ``published`` method) gives ``None``, and the publish loop of the
            write finds a duplicate on its own, as it always did.
        """
        lookup = getattr(self.target, "published", None)
        if not callable(lookup):
            return None
        earlier = lookup(commit, mode)
        if earlier is None:
            self._warn_if_the_key_may_have_expired(commit)
        return earlier

    def _warn_if_the_key_may_have_expired(self, commit: Any) -> None:
        """Warn when the window is older than every window that the target still holds a key for.

        A key that is not there is no proof that the window never ran: an expiry may have removed the snapshot
        that carried it, and then loading the window again would duplicate its rows.
        """
        window = getattr(commit, "logical_window", None)
        oldest_of = getattr(self.target, "oldest_key_window", None)
        if window is None or not callable(oldest_of):
            return
        try:
            oldest = oldest_of()
            if oldest is None or _utc(window[0]) >= oldest[0]:
                return
        except Exception as exc:  # noqa: BLE001 - the warning is advice, and it must never stop a run
            logger.debug("Pipeline %s: could not tell whether the key may have expired: %s", self.name, exc)
            return
        logger.warning(
            "Pipeline %s: the window %s/%s is older than every window that %r still holds a key for, the oldest "
            "starting %s, and its key is not there. If this window ran before, its key may have expired and this "
            "run loads it again. ldp backfill --replace replaces a window instead of adding to it.",
            self.name, _iso(window[0]), _iso(window[1]), self._target_name(), _iso(oldest[0]))

    # ------------------------------------------------------------------ run

    def run(self, mode: str | None = None, *, commit: Any = None, sink: EventSink | None = None,
            policy: Any = None, run_id: str | None = None, attempt: int = 1) -> PipelineResult:
        """Extract, transform, validate, then write, emitting run events on the way.

        Args:
            mode: Override the target's write mode for this run (Iceberg targets:
                ``append``, ``overwrite`` or ``upsert``).
            commit: A :class:`~local_data_platform.format.iceberg.commit.CommitContext`.
                When given, the target is written through the staged publish protocol:
                a re-run with the same idempotency key finds the published snapshot and
                writes nothing (``PipelineResult.status == "skipped_duplicate"``). The
                run id, attempt and key come from the context. The key is looked for before the source is read, in
                the whole retained history of the target, so a duplicate reads nothing.
            sink: Where this run's events go, instead of the pipeline's default sink.
            policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` of the write. A batch of
                runs raises its ``max_search_depth``, see :func:`~local_data_platform.etl.batch_search`.
            run_id: The id of the run, for a run that has no ``commit``: the attempts of a retry share it, so their
                events belong together. A new UUIDv7 by default. A ``commit`` brings its own.
            attempt: The attempt number, from 1, which every event of the run carries. A ``commit`` brings its own.

        Returns:
            A :class:`PipelineResult`.

        Raises:
            DataQualityError: If a check failed and ``on_failure`` is ``"fail"``.
                Nothing is written; ``run.blocked_quality`` is emitted.
            ConfigError: If ``commit`` is given for a target that can't take it, or together with ``run_id`` or
                ``attempt``, or ``attempt`` is not a whole number from 1.
            TypeError: If ``sink`` has no ``emit``/``flush`` methods.
        """
        started = time.perf_counter()
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ConfigError(f"attempt must be a whole number from 1, got {attempt!r}")
        if commit is not None and (run_id is not None or attempt != 1):
            raise ConfigError("a commit context brings its own run id and attempt, so pass either commit= or "
                              "run_id=/attempt=, not both")
        if commit is not None:
            self._require_commit_support()
        window = self._run_window(commit)
        run_id = str(getattr(commit, "run_id", None) or run_id or uuid7())
        attempt = int(getattr(commit, "attempt", None) or attempt)
        key = getattr(commit, "idempotency_key", None) if commit is not None else None
        emitter = RunEmitter(self._check_sink(sink) if sink is not None else self.sink, run_id, attempt=attempt,
                             context={"pipeline": self.name, "table": self._target_name()})
        counts = {"rows_read": None, "rows_written": None}
        stage = "start"
        finished = False

        def finish(status: str, **extra: Any) -> None:
            nonlocal finished
            emitter.emit("run.finished", lambda: {
                "status": status, "duration_s": round(time.perf_counter() - started, 3), **counts,
                "idempotency_key": key, **extra})
            finished = True

        try:
            if emitter.enabled:
                emitter.emit("run.started", lambda: self._started_payload(mode, commit, key),
                             table_uuid=self._table_uuid())
            if commit is not None:
                stage = "search"
                earlier = self._published_before(commit, mode)
                if earlier is not None:
                    counts["rows_written"] = 0
                    result = self._result(None, 0, earlier, QualityReport(), key, run_id, started)
                    table_uuid, summary = (self._snapshot_facts(result.published_snapshot_id) if emitter.enabled
                                           else (None, {}))
                    emitter.emit("run." + result.status, lambda: self._written_payload(result, earlier, summary),
                                 table_uuid=table_uuid)
                    finish(result.status, published_snapshot_id=result.published_snapshot_id, quality_passed=None)
                    logger.info("Pipeline %s", result)
                    return result
            stage = "extract"
            df = self.extract()
            counts["rows_read"] = rows_read = df.num_rows
            df, rows_kept = self._select_window(df, window)
            emitter.emit("run.extracted", lambda: self._extracted_payload(df, rows_read, rows_kept))
            stage = "transform"
            df = self.transform(df)
            stage = "validate"
            blocked: DataQualityError | None = None
            try:
                report = self.validate(df, self._check_context(rows_read if rows_kept is None else rows_kept, window))
            except DataQualityError as error:
                blocked = error
                report = error.report if isinstance(error.report, QualityReport) else QualityReport()
            emitter.emit("quality.evaluated", lambda: self._quality_payload(df, report))
            if blocked is not None:
                emitter.emit("run.blocked_quality", lambda: {
                    "checks_failed": len(report.failures), "failures": [r.name for r in report.failures],
                    "on_failure": self.on_failure})
                counts["rows_written"] = 0
                finish("blocked_quality", quality_passed=False)
                raise blocked

            stage = "write"
            written = self.write(df, mode, commit=commit, policy=policy)
            stage = "report"
            result = self._result(df, rows_read, written, report, key, run_id, started, rows_kept)
            counts["rows_written"] = result.rows_written
            key = result.idempotency_key
            table_uuid, summary = self._snapshot_facts(result.published_snapshot_id) if emitter.enabled else (None, {})
            emitter.emit("run." + result.status, lambda: self._written_payload(result, written, summary),
                         table_uuid=table_uuid)
            finish(result.status, published_snapshot_id=result.published_snapshot_id,
                   quality_passed=report.passed)
            logger.info("Pipeline %s", result)
            return result
        except BaseException as error:
            if not finished:
                failure = {"stage": stage, "error_type": type(error).__name__}
                if emitter.enabled:
                    failure["message"] = redact_text(str(error))
                emitter.emit("run.failed", failure)
                finish("failed")
            raise
        finally:
            emitter.flush()

    def _result(self, df: pa.Table | None, rows_read: int, written: Any, report: QualityReport, key: str | None,
                run_id: str, started: float, rows_kept: int | None = None) -> PipelineResult:
        skipped = getattr(written, "skipped_duplicate", False) is True
        if isinstance(getattr(written, "rows_written", None), int):
            rows_written = written.rows_written
        elif isinstance(written, int) and not isinstance(written, bool):
            rows_written = written
        else:
            rows_written = 0 if skipped else df.num_rows
        snapshot_id = getattr(written, "snapshot_id", None)
        return PipelineResult(
            name=self.name,
            rows_read=rows_read,
            rows_written=rows_written,
            write_result=written if dataclasses.is_dataclass(written) else None,
            quality=report,
            duration_s=time.perf_counter() - started,
            run_id=run_id,
            idempotency_key=getattr(written, "idempotency_key", None) or key,
            published_snapshot_id=snapshot_id if isinstance(snapshot_id, int) else None,
            status="skipped_duplicate" if skipped else "published",
            rows_kept=rows_kept,
        )

    def load(self, mode: str | None = None, **kwargs: Any) -> PipelineResult:
        """Alias of :meth:`run`, kept for pre-0.1.1 callers."""
        return self.run(mode, **kwargs)

    # ------------------------------------------------------------------ event payloads

    def _target_name(self) -> str | None:
        identifier = getattr(self.target, "identifier", None)
        if isinstance(identifier, str):
            return identifier
        path = getattr(self.target, "path", None)
        return str(path) if path else getattr(self.target, "name", None)

    def _started_payload(self, mode: str | None, commit: Any, key: str | None) -> dict[str, Any]:
        window = getattr(commit, "logical_window", None)
        spec_hash = getattr(commit, "spec_hash", None)
        if spec_hash is None and self.config is not None:
            from local_data_platform.spec import spec_hash as hash_spec

            spec_hash = hash_spec(self.config)
        from local_data_platform import __version__

        return {
            "mode": mode or getattr(self.target, "write_mode", None),
            "publish": "staged" if commit is not None else "direct",
            "idempotency_key": key,
            "spec_hash": spec_hash,
            "logical_window": [_iso(bound) for bound in window] if window else None,
            "source": dataset_ref(self.source),
            "target": dataset_ref(self.target),
            "ldp_version": __version__,
        }

    @staticmethod
    def _extracted_payload(df: pa.Table, rows_read: int, rows_kept: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"rows_read": rows_read}
        if rows_kept is not None:
            payload["rows_kept"] = rows_kept
        payload["schema"] = schema_payload(df.schema)
        return payload

    @staticmethod
    def _check_context(source_rows: int, window: tuple[Any, Any] | None) -> dict[str, Any]:
        """What every check is told about the load: the source rows, and ``now`` as the window end in a window run."""
        context: dict[str, Any] = {"source_rows": source_rows}
        if window is not None:
            context["now"] = window[1]
        return context

    def _quality_payload(self, df: pa.Table, report: QualityReport) -> dict[str, Any]:
        return {
            "rows": df.num_rows,
            "schema": schema_payload(df.schema),
            "null_counts": {name: df.column(name).null_count for name in df.column_names},
            "on_failure": self.on_failure,
            **quality_payload(report),
        }

    @staticmethod
    def _written_payload(result: PipelineResult, written: Any, summary: Mapping[str, Any]) -> dict[str, Any]:
        write = dataclasses.asdict(written) if dataclasses.is_dataclass(written) else None
        return {
            "rows_written": result.rows_written,
            "snapshot_id": result.published_snapshot_id,
            "idempotency_key": result.idempotency_key,
            "write": write,
            "snapshot_summary": dict(summary),
        }

    def _table_uuid(self) -> str | None:
        return self._snapshot_facts(None)[0]

    def _snapshot_facts(self, snapshot_id: int | None) -> tuple[str | None, dict[str, Any]]:
        """Return the target table's UUID and a snapshot's summary, or ``(None, {})`` for non-Iceberg targets."""
        exists, load = getattr(self.target, "exists", None), getattr(self.target, "table", None)
        if not (callable(exists) and callable(load)):
            return None, {}
        try:
            if not exists():
                return None, {}
            table = load()
            table_uuid = str(table.metadata.table_uuid)
            snapshot = table.snapshot_by_id(snapshot_id) if snapshot_id is not None else None
        except Exception as exc:  # noqa: BLE001 - event metadata is best effort
            logger.debug("Could not read metadata of %r for run events: %s", self.target, exc)
            return None, {}
        summary: dict[str, Any] = {}
        if snapshot is not None and snapshot.summary is not None:
            summary = {"operation": snapshot.summary.operation.value, **snapshot.summary.additional_properties}
        return table_uuid, summary


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _utc(moment: Any) -> Any:
    """``moment`` as an aware datetime, reading one without a time zone as UTC."""
    return moment.replace(tzinfo=dt.timezone.utc) if getattr(moment, "tzinfo", True) is None else moment


__all__ = ["ON_FAILURE", "Pipeline", "PipelineResult", "Transform"]
