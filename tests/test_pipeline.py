"""Pipeline composition, quality gating, transforms, and every built-in pipeline end to end."""

import dataclasses
import datetime as dt
import inspect
import json
import logging
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
import pytest

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError, DataQualityError
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg, WriteResult
from local_data_platform.format.parquet import Parquet
from local_data_platform.pipeline import Pipeline, PipelineResult
from local_data_platform.pipeline.builders import bigquery_from_config, iceberg_from_config
from local_data_platform.pipeline.builtin import (
    BigQueryToCSV,
    CSVToIceberg,
    IcebergToCSV,
    IcebergToParquet,
    ParquetToIceberg,
)
from local_data_platform.pipeline.egression import Egression
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.pipeline.registry import create_pipeline, get_pipeline_class
from local_data_platform.quality import NotNull, QualityReport, Range, RowCount, Unique


class MemorySource:
    """A source that returns a fixed table and counts reads."""

    def __init__(self, table):
        self.table = table
        self.reads = 0
        self.name = "memory"

    def get(self):
        self.reads += 1
        return self.table


class MemoryTarget:
    """A target that keeps every table it is given."""

    def __init__(self):
        self.written: list[pa.Table] = []
        self.name = "sink"

    def put(self, df):
        self.written.append(df)
        return df.num_rows


def _with_bad_fare(table: pa.Table) -> pa.Table:
    fares = table["fare"].to_pylist()
    fares[0] = -1.0
    return table.set_column(table.schema.get_field_index("fare"), "fare", pa.array(fares, pa.float64()))


def _write_config(folder: Path, metadata: dict, identifier: str = "rides") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "config.json"
    path.write_text(json.dumps({"identifier": identifier, "metadata": metadata}))
    return path


# ---------------------------------------------------------------------- composition


def test_pipeline_moves_rows_from_source_to_target(tmp_path, sample_table, catalog_config):
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    pipeline = Pipeline(source=CSV("rides", tmp_path / "rides.csv"), target=Iceberg("rides", catalog_config))

    result = pipeline.run()

    assert isinstance(result, PipelineResult)
    assert result.name == "rides"
    assert result.rows_read == result.rows_written == 6
    assert isinstance(result.write_result, WriteResult)
    assert result.write_result.rows_after == 6
    assert isinstance(result.quality, QualityReport) and result.quality.passed and len(result.quality) == 0
    assert result.duration_s >= 0
    assert pipeline.target.row_count() == 6


def test_transforms_run_in_order_on_the_extracted_data(sample_table):
    target = MemoryTarget()
    seen = []

    def add_fare_with_tip(df):
        seen.append("tip")
        return df.append_column("fare_with_tip", pc.multiply(df["fare"], 1.1))

    def keep_big_tips(df):
        seen.append("filter")  # needs the column the first transform added
        return df.filter(pc.greater(df["fare_with_tip"], 15.0))

    result = Pipeline(source=MemorySource(sample_table), target=target,
                      transforms=[add_fare_with_tip, keep_big_tips]).run()

    assert seen == ["tip", "filter"]
    written = target.written[0]
    assert "fare_with_tip" in written.column_names
    assert pc.min(written["fare_with_tip"]).as_py() > 15.0
    assert result.rows_read == 6
    assert result.rows_written == written.num_rows < 6


def test_a_single_callable_is_accepted_as_transforms(sample_table):
    target = MemoryTarget()
    Pipeline(source=MemorySource(sample_table), target=target, transforms=lambda df: df.slice(0, 2)).run()
    assert target.written[0].num_rows == 2


def test_a_transform_must_return_a_table(sample_table):
    target = MemoryTarget()
    pipeline = Pipeline(source=MemorySource(sample_table), target=target, transforms=[lambda df: None])
    with pytest.raises(TypeError, match="transform #1"):
        pipeline.run()
    assert target.written == []


def test_non_callable_transforms_are_rejected_at_construction(sample_table):
    with pytest.raises(TypeError, match=r"transforms\[1\]"):
        Pipeline(source=MemorySource(sample_table), target=MemoryTarget(), transforms=[lambda df: df, "upper"])


def test_the_source_must_return_arrow_data():
    pipeline = Pipeline(source=MemorySource([1, 2, 3]), target=MemoryTarget())
    with pytest.raises(TypeError, match="expected a pyarrow.Table"):
        pipeline.run()


def test_a_record_batch_source_is_accepted(sample_table):
    target = MemoryTarget()
    Pipeline(source=MemorySource(sample_table.to_batches()[0]), target=target).run()
    assert isinstance(target.written[0], pa.Table)


def test_a_pipeline_needs_a_source_and_a_target(sample_table):
    with pytest.raises(ConfigError, match="needs a source"):
        Pipeline(target=MemoryTarget())
    with pytest.raises(ConfigError, match="needs a target"):
        Pipeline(source=MemorySource(sample_table))
    with pytest.raises(TypeError, match="get"):
        Pipeline(source=object(), target=MemoryTarget())
    with pytest.raises(TypeError, match="put"):
        Pipeline(source=MemorySource(sample_table), target=object())
    with pytest.raises(TypeError, match="Config"):
        Pipeline({"identifier": "x"})


def test_the_base_pipeline_cannot_build_parts_from_a_config(tmp_path):
    config = Config.from_dict({"identifier": "x", "metadata": {
        "source": {"name": "a", "format": "CSV", "path": "a.csv"},
        "target": {"name": "b", "format": "CSV", "path": "b.csv"},
    }}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="create_pipeline"):
        Pipeline(config)


def test_load_is_an_alias_of_run(sample_table):
    target = MemoryTarget()
    result = Pipeline(source=MemorySource(sample_table), target=target).load()
    assert isinstance(result, PipelineResult) and len(target.written) == 1


def test_result_serialises_to_json_and_reads_well(tmp_path, sample_table, catalog_config):
    result = Pipeline(source=MemorySource(sample_table), target=Iceberg("rides", catalog_config),
                      checks=[NotNull(["ride_id"])], name="demo").run()
    data = json.loads(json.dumps(result.to_dict()))
    assert data["name"] == "demo"
    assert data["write_result"]["rows_after"] == 6
    assert data["quality"]["passed"] is True
    assert str(result).startswith("demo: read 6 rows, wrote 6 rows in ")
    assert "1 of 1 checks passed" in str(result)


# ---------------------------------------------------------------------- quality gate


def test_failed_checks_with_fail_raise_and_write_nothing_to_a_new_table(sample_table, catalog_config):
    target = Iceberg("rides", catalog_config)
    pipeline = Pipeline(source=MemorySource(_with_bad_fare(sample_table)), target=target,
                        checks=[Range("fare", min=0), Unique(["ride_id"])])

    with pytest.raises(DataQualityError) as info:
        pipeline.run()

    assert [r.name for r in info.value.report.failures] == ["range(fare)"]
    assert "range(fare)" in str(info.value)
    assert not target.exists(), "the table was created although the batch failed its checks"


def test_failed_checks_leave_an_existing_table_unchanged(sample_table, catalog_config):
    target = Iceberg("rides", catalog_config)
    target.put(sample_table)
    snapshots_before = target.snapshots()

    bad = Pipeline(source=MemorySource(_with_bad_fare(sample_table)), target=target, checks=[Range("fare", min=0)])
    with pytest.raises(DataQualityError):
        bad.run(mode="append")

    assert target.row_count() == 6
    assert target.snapshots() == snapshots_before


def test_failed_checks_with_fail_do_not_create_a_file_target(tmp_path, sample_table):
    out = tmp_path / "out.csv"
    pipeline = Pipeline(source=MemorySource(sample_table), target=CSV("out", out), checks=[RowCount(min=100)])
    with pytest.raises(DataQualityError):
        pipeline.run()
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_checks_with_warn_log_and_write(sample_table, caplog):
    target = MemoryTarget()
    pipeline = Pipeline(source=MemorySource(_with_bad_fare(sample_table)), target=target,
                        checks=[Range("fare", min=0)], on_failure="warn")

    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        result = pipeline.run()

    assert len(target.written) == 1
    assert result.quality.passed is False
    assert [r.name for r in result.quality.failures] == ["range(fare)"]
    warning = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning and "range(fare)" in warning[0].getMessage()


def test_checks_see_the_transformed_data(sample_table):
    target = MemoryTarget()
    drop_negative = [lambda df: df.filter(pc.greater_equal(df["fare"], 0))]
    result = Pipeline(source=MemorySource(_with_bad_fare(sample_table)), target=target,
                      transforms=drop_negative, checks=[Range("fare", min=0)]).run()
    assert result.quality.passed
    assert target.written[0].num_rows == 5


def test_row_count_equals_source_compares_with_the_rows_read(sample_table):
    keep_all = Pipeline(source=MemorySource(sample_table), target=MemoryTarget(),
                        checks=[RowCount(equals="source")])
    assert keep_all.run().quality.passed

    drop_one = Pipeline(source=MemorySource(sample_table), target=MemoryTarget(),
                        transforms=[lambda df: df.slice(1)], checks=[RowCount(equals="source")])
    with pytest.raises(DataQualityError, match="row_count"):
        drop_one.run()


def test_validate_returns_the_report_without_writing(sample_table):
    target = MemoryTarget()
    pipeline = Pipeline(source=MemorySource(sample_table), target=target, checks=[{"check": "row_count", "min": 1}])
    report = pipeline.validate(pipeline.extract())
    assert report.passed and len(report) == 1
    assert target.written == []


def test_checks_and_on_failure_come_from_the_config(tmp_path, sample_table):
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
        "quality": {"on_failure": "warn", "checks": [{"check": "row_count", "min": 1},
                                                     {"check": "unique", "columns": ["ride_id"]}]},
    }}, base_dir=tmp_path)
    pipeline = create_pipeline(config)
    assert pipeline.on_failure == "warn"
    assert [type(c) for c in pipeline.checks] == [RowCount, Unique]


def test_an_invalid_on_failure_is_a_config_error(sample_table):
    with pytest.raises(ConfigError, match="on_failure"):
        Pipeline(source=MemorySource(sample_table), target=MemoryTarget(), on_failure="ignore")


def test_an_invalid_check_config_fails_before_the_target_is_built(tmp_path, sample_table):
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
        "quality": {"checks": [{"check": "no_such_check"}]},
    }}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="no_such_check"):
        create_pipeline(config)
    assert not (tmp_path / "wh").exists(), "the Iceberg catalog was created before the config was validated"


# ---------------------------------------------------------------------- write modes


def test_run_mode_overrides_the_iceberg_write_mode(sample_table, catalog_config):
    pipeline = Pipeline(source=MemorySource(sample_table), target=Iceberg("rides", catalog_config))
    assert [pipeline.run(mode="append").write_result.rows_after for _ in range(2)] == [6, 12]
    assert [pipeline.run(mode="overwrite").write_result.rows_after for _ in range(2)] == [6, 6]


def test_file_targets_only_take_overwrite(tmp_path, sample_table):
    pipeline = Pipeline(source=MemorySource(sample_table), target=CSV("out", tmp_path / "out.csv"))
    with pytest.raises(ConfigError, match="always overwritten"):
        pipeline.run(mode="append")
    assert not (tmp_path / "out.csv").exists()
    result = pipeline.run(mode="overwrite")
    assert result.rows_written == 6
    assert result.write_result is None


def test_an_unknown_mode_is_a_config_error(sample_table, catalog_config):
    pipeline = Pipeline(source=MemorySource(sample_table), target=Iceberg("rides", catalog_config))
    with pytest.raises(ConfigError, match="write mode"):
        pipeline.run(mode="merge")


# ---------------------------------------------------------------------- built-in pipelines


def test_csv_to_iceberg_resolves_paths_against_the_config_folder(tmp_path, sample_table, monkeypatch):
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    pa_csv.write_csv(sample_table, project / "data" / "rides.csv")
    config_path = _write_config(project, {
        "source": {"name": "rides", "format": "CSV", "path": "data/rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"},
                   "write_mode": "upsert", "join_cols": ["ride_id"]},
        "quality": {"checks": [{"check": "unique", "columns": ["ride_id"]}]},
    })
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    pipeline = create_pipeline(Config.from_json(config_path))
    first, second = pipeline.run(), pipeline.run()

    assert type(pipeline) is CSVToIceberg
    assert first.rows_read == 6 and first.write_result.rows_after == 6
    assert second.write_result.rows_after == 6, "re-running an upsert changed the row count"
    assert second.rows_written == 0
    assert (project / "wh").is_dir()
    assert list(elsewhere.iterdir()) == []


def test_parquet_to_iceberg_creates_a_partitioned_table(tmp_path, sample_table):
    pq.write_table(sample_table, tmp_path / "rides.parquet")
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "PARQUET", "path": "rides.parquet"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"},
                   "write_mode": "overwrite", "partition_by": [{"column": "city", "transform": "identity"}]},
    }}, base_dir=tmp_path)

    pipeline = create_pipeline(config)
    result = pipeline.run()

    assert type(pipeline) is ParquetToIceberg
    assert result.write_result.rows_after == 6
    table = pipeline.target.table()
    assert [field.name for field in table.spec().fields] == ["city"]
    assert table.inspect.partitions().num_rows == 3
    assert pipeline.run().write_result.rows_after == 6


def _load_iceberg(tmp_path, table):
    catalog = {"identifier": "ns", "warehouse_path": "wh"}
    Iceberg("rides", catalog, base_dir=tmp_path).put(table)
    return catalog


def test_iceberg_to_csv_exports_the_current_snapshot(tmp_path, sample_table):
    catalog = _load_iceberg(tmp_path, sample_table)
    config = Config.from_dict({"identifier": "export", "metadata": {
        "source": {"name": "rides", "format": "ICEBERG", "catalog": catalog},
        "target": {"name": "rides", "format": "CSV", "path": "exports/rides.csv"},
        "quality": {"checks": [{"check": "row_count", "equals": "source"}]},
    }}, base_dir=tmp_path)

    pipeline = create_pipeline(config)
    result = pipeline.run()

    assert type(pipeline) is IcebergToCSV
    assert result.rows_read == result.rows_written == 6
    exported = CSV("rides", tmp_path / "exports" / "rides.csv").get()
    assert exported.num_rows == 6
    assert exported["ride_id"].to_pylist() == sample_table["ride_id"].to_pylist()


def test_iceberg_to_parquet_exports_the_current_snapshot(tmp_path, sample_table):
    catalog = _load_iceberg(tmp_path, sample_table)
    config = Config.from_dict({"identifier": "export", "metadata": {
        "source": {"name": "rides", "format": "ICEBERG", "catalog": catalog},
        "target": {"name": "rides", "format": "PARQUET", "path": "exports/rides.parquet"},
    }}, base_dir=tmp_path)

    pipeline = create_pipeline(config)
    result = pipeline.run()

    assert type(pipeline) is IcebergToParquet
    assert result.rows_written == 6
    exported = Parquet("rides", tmp_path / "exports" / "rides.parquet").get()
    assert sorted(exported["fare"].to_pylist()) == sorted(sample_table["fare"].to_pylist())


def test_an_iceberg_source_ignores_target_only_settings(tmp_path, sample_table):
    catalog = _load_iceberg(tmp_path, sample_table)
    config = Config.from_dict({"identifier": "export", "metadata": {
        # write_mode upsert without join_cols would be invalid on a target.
        "source": {"name": "rides", "format": "ICEBERG", "catalog": catalog, "write_mode": "upsert"},
        "target": {"name": "rides", "format": "CSV", "path": "out.csv"},
    }}, base_dir=tmp_path)
    assert iceberg_from_config(config, "source").identifier == "ns.rides"


@pytest.fixture
def bigquery_config(tmp_path):
    """A BigQuery-to-CSV config in ``tmp_path/project`` with relative query and key paths."""
    project = tmp_path / "project"
    (project / "queries").mkdir(parents=True)
    (project / "secrets").mkdir()
    (project / "queries" / "rides.json").write_text(json.dumps({"query": "SELECT 1 AS ride_id"}))
    (project / "secrets" / "key.json").write_text(json.dumps({"type": "service_account",
                                                              "project_id": "demo-project"}))
    path = _write_config(project, {
        "source": {"name": "rides_query", "format": "JSON", "engine": "BIGQUERY", "path": "queries/rides.json",
                   "credentials": {"name": "GCP", "path": "secrets/key.json"}},
        "target": {"name": "rides", "format": "CSV", "path": "out/rides.csv"},
        "quality": {"checks": [{"check": "not_null", "columns": ["ride_id"]}]},
    })
    return Config.from_json(path)


def _fake_client(table):
    client = mock.MagicMock(name="bigquery.Client")
    client.query.return_value.result.return_value.to_arrow.return_value = table
    return client


def test_bigquery_to_csv_builds_the_client_from_the_config_and_queries_once(bigquery_config, sample_table):
    client = _fake_client(sample_table)
    bigquery_module = SimpleNamespace(Client=mock.MagicMock(return_value=client))
    service_account = SimpleNamespace(Credentials=SimpleNamespace(
        from_service_account_file=mock.MagicMock(return_value=SimpleNamespace(project_id="from-key"))))
    project = bigquery_config.base_dir

    with mock.patch("local_data_platform.store.source.gcp.bigquery._import_google",
                    return_value=(bigquery_module, service_account)):
        pipeline = create_pipeline(bigquery_config)
        assert type(pipeline) is BigQueryToCSV
        result = pipeline.run()

    service_account.Credentials.from_service_account_file.assert_called_once_with(
        str(project / "secrets" / "key.json"))
    assert bigquery_module.Client.call_args.kwargs["project"] == "demo-project"
    client.query.assert_called_once_with("SELECT 1 AS ride_id")
    assert result.rows_read == result.rows_written == 6
    assert result.quality.passed
    assert CSV("rides", project / "out" / "rides.csv").get().num_rows == 6


def test_bigquery_to_csv_accepts_a_prebuilt_client(bigquery_config, sample_table):
    client = _fake_client(sample_table)
    source = bigquery_from_config(bigquery_config, client=client)
    result = create_pipeline(bigquery_config, source=source).run()
    client.query.assert_called_once_with("SELECT 1 AS ride_id")
    assert result.rows_written == 6


def test_bigquery_to_csv_fails_early_without_the_key_file(bigquery_config):
    (bigquery_config.base_dir / "secrets" / "key.json").unlink()
    with pytest.raises(ConfigError, match="credentials file not found"):
        create_pipeline(bigquery_config)


def test_a_bigquery_source_needs_the_bigquery_engine(tmp_path):
    config = Config.from_dict({"identifier": "x", "metadata": {
        "source": {"name": "q", "format": "JSON", "engine": "DUCKDB", "path": "q.json"},
        "target": {"name": "t", "format": "CSV", "path": "t.csv"},
    }}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="BIGQUERY"):
        bigquery_from_config(config)


def test_a_builtin_rejects_a_config_for_another_route(tmp_path):
    config = Config.from_dict({"identifier": "x", "metadata": {
        "source": {"name": "rides", "format": "PARQUET", "path": "rides.parquet"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
    }}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="expected 'CSV'"):
        CSVToIceberg(config)


@pytest.mark.parametrize("missing", ["path", "name"])
def test_a_missing_source_key_is_a_config_error(tmp_path, missing):
    source = {"name": "rides", "format": "CSV", "path": "rides.csv"}
    del source[missing]
    config = Config.from_dict({"identifier": "x", "metadata": {
        "source": source,
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
    }}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match=f"metadata.source is missing '{missing}'"):
        create_pipeline(config)


def test_unused_config_keys_are_logged(tmp_path, caplog):
    config = Config.from_dict({"identifier": "x", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv", "delimiter": ";"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
    }}, base_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        create_pipeline(config)
    assert any("delimiter" in record.getMessage() for record in caplog.records)


def test_pipeline_directions():
    assert issubclass(CSVToIceberg, Ingestion) and issubclass(ParquetToIceberg, Ingestion)
    assert issubclass(BigQueryToCSV, Ingestion)
    assert not issubclass(ParquetToIceberg, Egression)
    assert issubclass(IcebergToCSV, Egression) and issubclass(IcebergToParquet, Egression)


# ---------------------------------------------------------------------- legacy names


def test_legacy_egression_csv_to_iceberg_warns_and_still_loads(tmp_path, sample_table):
    from local_data_platform.pipeline.egression.csv_to_iceberg import CSVToIceberg as LegacyCSVToIceberg

    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
    }}, base_dir=tmp_path)

    with pytest.warns(DeprecationWarning, match="create_pipeline"):
        legacy = LegacyCSVToIceberg(config=config)
    assert isinstance(legacy, CSVToIceberg)
    assert legacy.load().write_result.rows_after == 6
    assert get_pipeline_class("CSV", "ICEBERG") is CSVToIceberg


def test_importing_the_legacy_module_does_not_warn():
    import importlib

    import local_data_platform.pipeline.egression.csv_to_iceberg as legacy_module

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        importlib.reload(legacy_module)


def test_pyarrow_loader_delegates_to_create_pipeline(tmp_path, sample_table):
    from local_data_platform.pipeline.ingestion.pyarrow import PyArrowLoader

    pq.write_table(sample_table, tmp_path / "rides.parquet")
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "PARQUET", "path": "rides.parquet"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
        "quality": {"checks": [{"check": "row_count", "min": 1}]},
    }}, base_dir=tmp_path)

    loader = PyArrowLoader(config)

    assert isinstance(loader, Pipeline)
    assert type(loader.pipeline) is ParquetToIceberg
    assert loader.target is loader.pipeline.target
    assert loader._extract().num_rows == 6
    result = loader.load()
    assert result.write_result.rows_after == 6
    assert result.quality.passed and len(result.quality) == 1


# ---------------------------------------------------------------------- run events


SUCCESS = ["run.started", "run.extracted", "quality.evaluated", "run.published", "run.finished"]


def _payloads(sink) -> dict:
    return {event.type: event.payload for event in sink.events}


def test_a_run_emits_the_contract_sequence_with_one_run_id(sample_table, catalog_config):
    import uuid

    from local_data_platform.events import MemorySink

    sink = MemorySink()
    target = Iceberg("rides", catalog_config)
    result = Pipeline(source=MemorySource(sample_table), target=target, checks=[NotNull(["ride_id"])],
                      name="rides").run(sink=sink)

    assert sink.types == SUCCESS
    assert sink.flushes == 1
    assert uuid.UUID(result.run_id).version == 7
    assert {event.run_id for event in sink.events} == {result.run_id}
    assert [event.seq for event in sink.events] == [0, 1, 2, 3, 4]
    assert all(event.attempt == 1 for event in sink.events)
    assert result.status == "published" and result.idempotency_key is None
    assert result.published_snapshot_id == target.table().current_snapshot().snapshot_id

    payloads = _payloads(sink)
    assert all(p["pipeline"] == "rides" and p["table"] == "test_ns.rides" for p in payloads.values())
    assert payloads["run.started"]["publish"] == "direct" and payloads["run.started"]["mode"] == "append"
    assert payloads["run.started"]["target"]["name"] == "test_ns.rides"
    assert payloads["run.extracted"]["rows_read"] == 6
    assert [f["name"] for f in payloads["run.extracted"]["schema"]] == sample_table.column_names
    assert payloads["quality.evaluated"]["passed"] is True and payloads["quality.evaluated"]["checks_run"] == 1
    assert payloads["quality.evaluated"]["null_counts"] == {name: 0 for name in sample_table.column_names}
    published = payloads["run.published"]
    assert published["snapshot_id"] == result.published_snapshot_id and published["rows_written"] == 6
    assert published["write"]["rows_after"] == 6 and published["snapshot_summary"]["added-records"] == "6"
    assert payloads["run.finished"]["status"] == "published"
    table_uuid = str(target.table().metadata.table_uuid)
    assert sink.events[0].table_uuid is None, "the table did not exist when the run started"
    assert [event.table_uuid for event in sink.events[3:]] == [table_uuid, table_uuid]
    json.dumps([event.to_dict() for event in sink.events])


def test_a_blocked_run_emits_blocked_quality_raises_and_writes_nothing(sample_table, catalog_config):
    from local_data_platform.events import MemorySink

    sink = MemorySink()
    target = Iceberg("rides", catalog_config)
    pipeline = Pipeline(source=MemorySource(_with_bad_fare(sample_table)), target=target,
                        checks=[Range("fare", min=0)])
    with pytest.raises(DataQualityError):
        pipeline.run(sink=sink)

    assert sink.types == ["run.started", "run.extracted", "quality.evaluated", "run.blocked_quality", "run.finished"]
    payloads = _payloads(sink)
    assert payloads["run.blocked_quality"]["failures"] == ["range(fare)"]
    assert payloads["quality.evaluated"]["results"][0]["failing_rows"] == 1
    assert payloads["run.finished"]["status"] == "blocked_quality" and payloads["run.finished"]["rows_written"] == 0
    assert sink.flushes == 1
    assert not target.exists()


def test_warned_checks_still_publish(sample_table):
    from local_data_platform.events import MemorySink

    sink = MemorySink()
    Pipeline(source=MemorySource(_with_bad_fare(sample_table)), target=MemoryTarget(),
             checks=[Range("fare", min=0)], on_failure="warn").run(sink=sink)
    assert sink.types == SUCCESS
    assert _payloads(sink)["quality.evaluated"]["passed"] is False
    assert _payloads(sink)["run.finished"]["quality_passed"] is False


class FailingSource:
    name = "broken"

    def get(self):
        raise OSError("disk gone; password=hunter2")


def test_a_failed_run_emits_run_failed_with_the_stage_and_reraises(sample_table, catalog_config):
    from local_data_platform.events import MemorySink

    sink = MemorySink()
    with pytest.raises(OSError, match="disk gone"):
        Pipeline(source=FailingSource(), target=MemoryTarget()).run(sink=sink)
    assert sink.types == ["run.started", "run.failed", "run.finished"]
    failed = _payloads(sink)["run.failed"]
    assert (failed["stage"], failed["error_type"]) == ("extract", "OSError")
    assert "hunter2" not in failed["message"]
    assert _payloads(sink)["run.finished"]["status"] == "failed"

    sink = MemorySink()
    with pytest.raises(ConfigError):
        Pipeline(source=MemorySource(sample_table), target=Iceberg("rides", catalog_config)).run("merge", sink=sink)
    assert sink.types == ["run.started", "run.extracted", "quality.evaluated", "run.failed", "run.finished"]
    assert _payloads(sink)["run.failed"]["stage"] == "write"


def test_a_broken_sink_never_fails_a_run(sample_table, caplog):
    class Broken:
        def emit(self, event):
            raise RuntimeError("collector down")

        def flush(self):
            raise RuntimeError("collector down")

    target = MemoryTarget()
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        result = Pipeline(source=MemorySource(sample_table), target=target).run(sink=Broken())
    assert result.rows_written == 6 and len(target.written) == 1
    assert "collector down" in caplog.text


def test_without_a_sink_a_run_behaves_as_in_0_1_1(sample_table, catalog_config):
    from local_data_platform.events import NullSink

    pipeline = Pipeline(source=MemorySource(sample_table), target=Iceberg("rides", catalog_config))
    assert isinstance(pipeline.sink, NullSink)
    result = pipeline.run()
    assert result.run_id and result.status == "published"
    assert pipeline.target.snapshots()[-1]["summary"].get("ldp.run-id") is None
    with pytest.raises(TypeError, match="emit"):
        pipeline.run(sink=object())
    with pytest.raises(TypeError, match="emit"):
        Pipeline(source=MemorySource(sample_table), target=MemoryTarget(), sink="jsonl")


def test_the_config_observability_block_sets_the_default_sinks(tmp_path, sample_table):
    from local_data_platform.catalog.provider import create_catalog
    from local_data_platform.events import read_iceberg_events, read_jsonl

    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
        "quality": [{"check": "row_count", "min": 1}],
        "observability": {"sinks": ["iceberg", "jsonl"]},
    }}, base_dir=tmp_path)
    result = create_pipeline(config).run()

    jsonl = read_jsonl(tmp_path / ".ldp" / "events.jsonl")
    assert [event.type for event in jsonl] == SUCCESS and jsonl[0].run_id == result.run_id
    catalog = create_catalog(config.target["catalog"], base_dir=tmp_path)
    assert [event.event_id for event in read_iceberg_events(catalog, pipeline="rides")] == \
        [event.event_id for event in jsonl]
    assert catalog.load_table("_ldp.quality_results").scan().to_arrow().num_rows == 1
    assert sorted(".".join(t) for t in catalog.list_tables("_ldp")) == ["_ldp.quality_results", "_ldp.runs"]


def test_a_bad_observability_block_fails_before_anything_is_built(tmp_path):
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
        "observability": {"sinks": ["kafka"]},
    }}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="kafka"):
        create_pipeline(config)
    assert not (tmp_path / "wh").exists()


# ---------------------------------------------------------------------- staged commits


@dataclasses.dataclass(frozen=True)
class FakeWrite:
    table_identifier: str
    mode: str
    rows_written: int
    rows_before: int
    rows_after: int
    snapshot_id: int | None
    branch: str | None = None
    idempotency_key: str | None = None
    attempts: int = 1
    skipped_duplicate: bool = False


class CommitTarget:
    """An Iceberg-like target whose put takes commit= and dedupes by key."""

    name = "rides"
    identifier = "ns.rides"
    write_mode = "append"

    def __init__(self):
        self.calls = []
        self.published: dict[str, int] = {}

    def put(self, df, mode=None, *, commit=None):
        self.calls.append((mode, commit))
        key = commit.idempotency_key
        if key in self.published:
            return FakeWrite(self.identifier, mode or "append", 0, 6, 6, self.published[key], None, key, 0, True)
        self.published[key] = 1000 + len(self.published)
        return FakeWrite(self.identifier, mode or "append", df.num_rows, 0, df.num_rows, self.published[key],
                         "ldp_rabc_a1_0", key, 1)


def _commit(key="k" * 64, attempt=2):
    from local_data_platform.events import uuid7

    return SimpleNamespace(run_id=uuid7(), attempt=attempt, idempotency_key=key, spec_hash="h" * 64,
                           logical_window=(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
                                           dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)))


def test_a_staged_run_passes_the_commit_and_reports_a_skipped_duplicate(sample_table):
    from local_data_platform.events import MemorySink

    target, commit = CommitTarget(), _commit()
    pipeline = Pipeline(source=MemorySource(sample_table), target=target)
    first_sink, second_sink = MemorySink(), MemorySink()
    first = pipeline.run(commit=commit, sink=first_sink)
    second = pipeline.run("append", commit=commit, sink=second_sink)

    assert target.calls == [(None, commit), ("append", commit)]
    assert (first.status, first.run_id, first.idempotency_key) == ("published", commit.run_id, commit.idempotency_key)
    assert first.published_snapshot_id == 1000 and first.rows_written == 6
    assert second.status == "skipped_duplicate" and second.skipped_duplicate is True
    assert second.rows_written == 0 and second.published_snapshot_id == 1000
    assert "skipped: idempotency key already published in snapshot 1000" in str(second)
    assert second.to_dict()["status"] == "skipped_duplicate"

    assert first_sink.types == SUCCESS
    assert second_sink.types == ["run.started", "run.extracted", "quality.evaluated", "run.skipped_duplicate",
                                 "run.finished"]
    assert {e.attempt for e in second_sink.events} == {2}
    started = _payloads(second_sink)["run.started"]
    assert started["publish"] == "staged" and started["spec_hash"] == "h" * 64
    assert started["idempotency_key"] == commit.idempotency_key
    assert started["logical_window"] == ["2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00"]
    assert _payloads(second_sink)["run.skipped_duplicate"]["snapshot_id"] == 1000
    assert _payloads(second_sink)["run.finished"]["status"] == "skipped_duplicate"
    assert _payloads(first_sink)["run.published"]["write"]["branch"] == "ldp_rabc_a1_0"


def test_a_commit_for_a_file_target_fails_before_reading_the_source(tmp_path, sample_table):
    from local_data_platform.events import MemorySink

    source, sink = MemorySource(sample_table), MemorySink()
    with pytest.raises(ConfigError, match="staged publish protocol"):
        Pipeline(source=source, target=CSV("out", tmp_path / "out.csv")).run(commit=_commit(), sink=sink)
    assert source.reads == 0 and sink.events == []


def test_pyarrow_loader_passes_commit_and_sink_through(tmp_path, sample_table):
    from local_data_platform.events import MemorySink
    from local_data_platform.pipeline.ingestion.pyarrow import PyArrowLoader

    pq.write_table(sample_table, tmp_path / "rides.parquet")
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "PARQUET", "path": "rides.parquet"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
    }}, base_dir=tmp_path)
    target, sink = CommitTarget(), MemorySink()
    result = PyArrowLoader(config, target=target).run(commit=_commit(), sink=sink)
    assert result.status == "published" and sink.types == SUCCESS and len(target.calls) == 1


def _staged_protocol_available() -> bool:
    try:
        from local_data_platform.format.iceberg.commit import CommitContext  # noqa: F401
    except ImportError:
        return False
    return "commit" in inspect.signature(Iceberg.put).parameters


staged = pytest.mark.skipif(not _staged_protocol_available(),
                            reason="needs the staged publish protocol (format/iceberg/commit.py, Iceberg.put(commit=))")


@staged
def test_rerunning_a_window_through_run_config_is_a_no_op(tmp_path, sample_table):
    from local_data_platform.etl import run_config
    from local_data_platform.events import read_jsonl
    from local_data_platform.spec import idempotency_key, spec_hash

    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    path = _write_config(tmp_path, {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"},
                   "write_mode": "append"},
        "observability": {"sinks": ["jsonl"]},
    })
    window = "2026-09-01/2026-09-02"
    first = run_config(path, window=window)
    second = run_config(path, window=window)
    third = run_config(path, window="2026-09-02/2026-09-03")

    config = Config.from_json(path)
    assert first.status == "published" and first.idempotency_key == idempotency_key(config, window)
    assert second.status == "skipped_duplicate" and second.published_snapshot_id == first.published_snapshot_id
    assert third.status == "published" and third.write_result.rows_after == 12
    table = Iceberg("rides", {"identifier": "ns", "warehouse_path": "wh"}, base_dir=tmp_path)
    assert table.row_count() == 12, "an append re-run over the same window must not duplicate rows"
    summary = table.table().snapshot_by_id(first.published_snapshot_id).summary.additional_properties
    assert summary["ldp.idempotency-key"] == first.idempotency_key
    assert summary["ldp.run-id"] == first.run_id and summary["ldp.spec-hash"] == spec_hash(config)
    types = [event.type for event in read_jsonl(tmp_path / ".ldp" / "events.jsonl")]
    assert types.count("run.published") == 2 and types.count("run.skipped_duplicate") == 1


@staged
def test_commit_context_uses_the_idempotency_horizon(tmp_path, sample_table):
    from local_data_platform.etl import commit_context
    from local_data_platform.spec import idempotency_key, spec_hash

    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
    }}, base_dir=tmp_path)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    context = commit_context(config, window="2026-09-01/2026-09-02", now=now)
    assert context.idempotency_key == idempotency_key(config, "2026-09-01/2026-09-02")
    assert context.spec_hash == spec_hash(config) and context.attempt == 1
    assert context.search_since_ms == int(now.timestamp() * 1000) - 7 * 86_400_000
    assert context.logical_window[0] == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    assert commit_context(config, idempotency_key="from-airflow").idempotency_key == "from-airflow"


def test_commit_context_explains_a_missing_staged_protocol(tmp_path, monkeypatch):
    import sys

    from local_data_platform.etl import commit_context, run_config

    monkeypatch.setitem(sys.modules, "local_data_platform.format.iceberg.commit", None)
    config = Config.from_dict({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
    }}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="staged publishing"):
        commit_context(config, window="2026-09-01/2026-09-02")
    with pytest.raises(ConfigError, match="not both"):
        run_config(config, commit=_commit(), window="2026-09-01/2026-09-02")
