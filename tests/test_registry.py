"""The pipeline registry: routes, lookups, registration and the create_pipeline factory."""

import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
import pytest

from local_data_platform import Config, SupportedEngine, SupportedFormat
from local_data_platform.exceptions import ConfigError, PipelineNotFound
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline import Pipeline
from local_data_platform.pipeline.builders import csv_from_config, parquet_from_config
from local_data_platform.pipeline.builtin import (
    BUILTIN_PIPELINES,
    BigQueryToCSV,
    CSVToIceberg,
    IcebergToCSV,
    IcebergToParquet,
    ParquetToIceberg,
)
from local_data_platform.pipeline.registry import (
    Route,
    create_pipeline,
    get_pipeline_class,
    make_route,
    register_pipeline,
    registered_pipelines,
    unregister_pipeline,
)
from local_data_platform.quality import RowCount

REPO_ROOT = Path(__file__).resolve().parents[1]

BUILTIN_ROUTES = {
    Route("CSV", "ICEBERG"): CSVToIceberg,
    Route("PARQUET", "ICEBERG"): ParquetToIceberg,
    Route("ICEBERG", "CSV"): IcebergToCSV,
    Route("ICEBERG", "PARQUET"): IcebergToParquet,
    Route("JSON", "CSV", "BIGQUERY"): BigQueryToCSV,
}


@pytest.fixture
def temporary_routes():
    """Routes appended to this list are unregistered after the test, so none leak."""
    routes: list[tuple] = []
    yield routes
    for route in routes:
        try:
            unregister_pipeline(*route)
        except PipelineNotFound:
            pass


def _csv_to_iceberg_config(tmp_path: Path, **target) -> Config:
    pa_csv.write_csv(pa.table({"ride_id": [1, 2, 3], "fare": [10.5, 11.5, 12.5]}), tmp_path / "rides.csv")
    return Config.from_dict({
        "identifier": "rides",
        "metadata": {
            "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
            "target": {"name": "rides", "format": "ICEBERG",
                       "catalog": {"identifier": "ns", "warehouse_path": "warehouse"}, **target},
        },
    }, base_dir=tmp_path)


def test_every_builtin_route_is_registered():
    routes = registered_pipelines()
    for route, cls in BUILTIN_ROUTES.items():
        assert routes[route] is cls
    assert set(BUILTIN_PIPELINES) == set(BUILTIN_ROUTES.values())


@pytest.mark.parametrize("route, cls", list(BUILTIN_ROUTES.items()), ids=[str(r) for r in BUILTIN_ROUTES])
def test_get_pipeline_class_resolves_each_builtin(route, cls):
    assert get_pipeline_class(*route) is cls


def test_lookup_is_case_insensitive_and_accepts_enum_members():
    assert get_pipeline_class("csv", " Iceberg ") is CSVToIceberg
    assert get_pipeline_class(SupportedFormat.JSON, SupportedFormat.CSV, SupportedEngine.BIGQUERY) is BigQueryToCSV
    assert get_pipeline_class("json", "csv", "bigquery") is BigQueryToCSV


def test_unknown_pair_raises_pipeline_not_found_listing_the_registered_routes():
    with pytest.raises(PipelineNotFound) as info:
        get_pipeline_class("CSV", "DELTA")
    message = str(info.value)
    assert "CSV -> DELTA" in message
    for route, cls in BUILTIN_ROUTES.items():
        assert str(route) in message
        assert cls.__name__ in message


def test_the_engine_must_match_exactly():
    # A JSON file is only a pipeline source as a BigQuery query.
    with pytest.raises(PipelineNotFound, match="JSON -> CSV"):
        get_pipeline_class("JSON", "CSV")
    with pytest.raises(PipelineNotFound, match="engine BIGQUERY"):
        get_pipeline_class("CSV", "ICEBERG", "BIGQUERY")
    # An empty engine is the same as none.
    assert get_pipeline_class("CSV", "ICEBERG", "") is CSVToIceberg


@pytest.mark.parametrize("source, target", [(None, "CSV"), ("", "CSV"), ("CSV", None), (5, "CSV")])
def test_a_missing_or_non_string_format_is_a_config_error(source, target):
    with pytest.raises(ConfigError):
        get_pipeline_class(source, target)


def test_make_route_normalises():
    assert make_route(" csv", SupportedFormat.ICEBERG, None) == Route("CSV", "ICEBERG", None)
    assert str(make_route("json", "csv", "bigquery")) == "JSON -> CSV (engine BIGQUERY)"


def test_registered_pipelines_returns_a_sorted_copy():
    routes = registered_pipelines()
    assert list(routes) == sorted(routes, key=lambda r: (r.source_format, r.target_format, r.engine or ""))
    routes.clear()
    assert registered_pipelines(), "clearing the returned dict emptied the registry"


def test_a_custom_pipeline_registers_and_is_built_by_create_pipeline(tmp_path, temporary_routes):
    @register_pipeline("parquet", "csv")
    class ParquetToCSV(Pipeline):
        def build_source(self, config):
            return parquet_from_config(config, "source")

        def build_target(self, config):
            return csv_from_config(config, "target")

    temporary_routes.append(("PARQUET", "CSV"))
    pq.write_table(pa.table({"a": [1, 2]}), tmp_path / "in.parquet")
    config = Config.from_dict({"identifier": "custom", "metadata": {
        "source": {"name": "in", "format": "PARQUET", "path": "in.parquet"},
        "target": {"name": "out", "format": "CSV", "path": "out/out.csv"},
    }}, base_dir=tmp_path)

    assert get_pipeline_class("PARQUET", "CSV") is ParquetToCSV
    pipeline = create_pipeline(config)
    assert type(pipeline) is ParquetToCSV
    assert pipeline.run().rows_written == 2
    assert CSV("out", tmp_path / "out" / "out.csv").get().column("a").to_pylist() == [1, 2]

    unregister_pipeline("PARQUET", "CSV")
    with pytest.raises(PipelineNotFound):
        get_pipeline_class("PARQUET", "CSV")


def test_a_second_class_for_a_registered_route_is_rejected():
    with pytest.raises(ValueError, match="already registered"):
        @register_pipeline("CSV", "ICEBERG")
        class Impostor(Pipeline):
            pass
    assert get_pipeline_class("CSV", "ICEBERG") is CSVToIceberg


def test_replace_true_swaps_a_route_and_it_can_be_restored():
    class Replacement(CSVToIceberg):
        pass

    try:
        register_pipeline("CSV", "ICEBERG", replace=True)(Replacement)
        assert get_pipeline_class("CSV", "ICEBERG") is Replacement
    finally:
        register_pipeline("CSV", "ICEBERG", replace=True)(CSVToIceberg)
    assert get_pipeline_class("CSV", "ICEBERG") is CSVToIceberg


def test_registering_the_same_class_again_is_a_no_op():
    assert register_pipeline("CSV", "ICEBERG")(CSVToIceberg) is CSVToIceberg
    assert get_pipeline_class("CSV", "ICEBERG") is CSVToIceberg


@pytest.mark.parametrize("thing", [object, lambda: None, "CSVToIceberg"])
def test_only_pipeline_subclasses_can_be_registered(thing):
    with pytest.raises(TypeError, match="Pipeline subclass"):
        register_pipeline("X", "Y")(thing)
    with pytest.raises(PipelineNotFound):
        get_pipeline_class("X", "Y")


def test_unregistering_an_unknown_route_raises():
    with pytest.raises(PipelineNotFound, match="A -> B"):
        unregister_pipeline("A", "B")


def test_create_pipeline_builds_the_registered_class_from_the_config(tmp_path):
    config = _csv_to_iceberg_config(tmp_path, write_mode="upsert", join_cols=["ride_id"])
    pipeline = create_pipeline(config)

    assert type(pipeline) is CSVToIceberg
    assert pipeline.config is config
    assert pipeline.name == "rides"
    assert isinstance(pipeline.source, CSV)
    assert pipeline.source.path == tmp_path / "rides.csv"
    assert isinstance(pipeline.target, Iceberg)
    assert pipeline.target.identifier == "ns.rides"
    assert pipeline.target.path == tmp_path / "warehouse"
    assert pipeline.target.write_mode == "upsert"
    assert pipeline.target.join_cols == ["ride_id"]


def test_create_pipeline_passes_overrides_to_the_constructor(tmp_path):
    config = _csv_to_iceberg_config(tmp_path)
    pipeline = create_pipeline(config, checks=[RowCount(min=100)], on_failure="warn", name="custom",
                               transforms=[lambda df: df])
    assert pipeline.name == "custom"
    assert pipeline.on_failure == "warn"
    assert [type(check) for check in pipeline.checks] == [RowCount]
    assert len(pipeline.transforms) == 1


@pytest.mark.parametrize("not_a_config", [{"identifier": "x"}, "config.json", None])
def test_create_pipeline_needs_a_config_object(not_a_config):
    with pytest.raises(TypeError, match="Config.from_json"):
        create_pipeline(not_a_config)


def test_create_pipeline_raises_for_an_unregistered_route(tmp_path):
    config = Config.from_dict({"identifier": "x", "metadata": {
        "source": {"name": "a", "format": "CSV", "path": "a.csv"},
        "target": {"name": "b", "format": "CSV", "path": "b.csv"},
    }}, base_dir=tmp_path)
    with pytest.raises(PipelineNotFound, match="CSV -> CSV"):
        create_pipeline(config)
    assert not (tmp_path / "b.csv").exists()


def test_create_pipeline_works_without_importing_any_pipeline_module():
    probe = (
        "import sys, json\n"
        "from local_data_platform.pipeline.registry import get_pipeline_class\n"
        "before = 'local_data_platform.pipeline.builtin' in sys.modules\n"
        "name = get_pipeline_class('CSV', 'ICEBERG').__name__\n"
        "print(json.dumps({'before': before, 'name': name,\n"
        "                  'after': 'local_data_platform.pipeline.builtin' in sys.modules}))\n"
    )
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO_ROOT,
                               timeout=120)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {"before": False, "name": "CSVToIceberg", "after": True}
