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
the target is written. Checks run *before* the write: with ``on_failure="fail"`` a
failing check raises :class:`~local_data_platform.exceptions.DataQualityError` and
nothing is written; with ``"warn"`` the failures are logged and the write goes ahead.

Every run has a UUIDv7 ``run_id`` and emits :class:`~local_data_platform.events.RunEvent`
records to an event sink: ``run.started``, ``run.extracted``, ``quality.evaluated``, then
one of ``run.published``, ``run.skipped_duplicate``, ``run.blocked_quality`` or
``run.failed``, and finally ``run.finished``. The sink comes from ``run(sink=...)``, the
``sink=`` constructor argument, or the config's ``metadata.observability`` block; the
default drops every event. ``run(commit=CommitContext(...))`` writes through the staged,
exactly-once publish protocol (see ``docs/exactly_once.md``).
"""

import dataclasses
import inspect
import time
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
        rows_read: Rows the source returned, before any transform.
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
        }

    def __str__(self) -> str:
        if self.skipped_duplicate:
            return (f"{self.name}: read {self.rows_read} rows, skipped: idempotency key already published in "
                    f"snapshot {self.published_snapshot_id} ({self.duration_s:.2f}s)")
        text = f"{self.name}: read {self.rows_read} rows, wrote {self.rows_written} rows in {self.duration_s:.2f}s"
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

    Raises:
        ConfigError: If there is no source or target, ``on_failure`` is invalid, or
            a check config or the observability block is invalid.
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
    ):
        if config is not None and not isinstance(config, Config):
            raise TypeError(f"config must be a local_data_platform.Config, got {type(config).__name__}; "
                            "load one with Config.from_json(path)")
        self.config = config
        quality = config.quality if config is not None else {"on_failure": "fail", "checks": []}

        # Validate the cheap, side-effect-free parts first: building an Iceberg target creates its catalog.
        self.transforms = self._check_transforms(transforms)
        self.checks = self._check_checks(checks if checks is not None else quality["checks"])
        self.on_failure = self._check_on_failure(on_failure if on_failure is not None else quality["on_failure"])
        self.sink = self._check_sink(sink) if sink is not None else sink_from_config(config)

        self.source = source if source is not None else self._build(config, self.build_source, "source")
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

    def write(self, df: pa.Table, mode: str | None = None, *, commit: Any = None) -> Any:
        """Write ``df`` to the target.

        Args:
            df: The validated data.
            mode: The write mode, for targets whose ``put`` takes one (Iceberg:
                ``append``, ``overwrite`` or ``upsert``). ``None`` uses the target's default.
            commit: A :class:`~local_data_platform.format.iceberg.commit.CommitContext`
                to write through the staged publish protocol, for targets whose ``put``
                takes ``commit=`` (Iceberg).

        Returns:
            Whatever the target's ``put`` returned.

        Raises:
            ConfigError: If ``mode`` is given for a target that always replaces its
                data (CSV, Parquet) and is not ``"overwrite"``, or ``commit`` is given
                for a target that can't take it.
        """
        if commit is not None:
            self._require_commit_support()
            if mode is None:
                return self.target.put(df, commit=commit)
            return self.target.put(df, mode=mode, commit=commit)
        if mode is None:
            return self.target.put(df)
        if self._accepts_mode(self.target):
            return self.target.put(df, mode=mode)
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

    # ------------------------------------------------------------------ run

    def run(self, mode: str | None = None, *, commit: Any = None, sink: EventSink | None = None) -> PipelineResult:
        """Extract, transform, validate, then write, emitting run events on the way.

        Args:
            mode: Override the target's write mode for this run (Iceberg targets:
                ``append``, ``overwrite`` or ``upsert``).
            commit: A :class:`~local_data_platform.format.iceberg.commit.CommitContext`.
                When given, the target is written through the staged publish protocol:
                a re-run with the same idempotency key finds the published snapshot and
                writes nothing (``PipelineResult.status == "skipped_duplicate"``). The
                run id, attempt and key come from the context.
            sink: Where this run's events go, instead of the pipeline's default sink.

        Returns:
            A :class:`PipelineResult`.

        Raises:
            DataQualityError: If a check failed and ``on_failure`` is ``"fail"``.
                Nothing is written; ``run.blocked_quality`` is emitted.
            ConfigError: If ``commit`` is given for a target that can't take it.
            TypeError: If ``sink`` has no ``emit``/``flush`` methods.
        """
        started = time.perf_counter()
        if commit is not None:
            self._require_commit_support()
        run_id = str(getattr(commit, "run_id", None) or uuid7())
        attempt = int(getattr(commit, "attempt", None) or 1)
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
            stage = "extract"
            df = self.extract()
            counts["rows_read"] = rows_read = df.num_rows
            emitter.emit("run.extracted", lambda: {"rows_read": rows_read, "schema": schema_payload(df.schema)})
            stage = "transform"
            df = self.transform(df)
            stage = "validate"
            blocked: DataQualityError | None = None
            try:
                report = self.validate(df, {"source_rows": rows_read})
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
            written = self.write(df, mode, commit=commit)
            stage = "report"
            result = self._result(df, rows_read, written, report, key, run_id, started)
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

    def _result(self, df: pa.Table, rows_read: int, written: Any, report: QualityReport, key: str | None,
                run_id: str, started: float) -> PipelineResult:
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


__all__ = ["ON_FAILURE", "Pipeline", "PipelineResult", "Transform"]
