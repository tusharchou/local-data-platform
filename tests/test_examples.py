"""The examples and the documented code must match the library.

Every example config loads and validates, uses paths relative to its own folder, points
at inputs that are committed, and names a registered pipeline route. The report scripts
import, and the offline NEAR and NYC examples run end to end. Every ``local_data_platform``
import shown in the README and the guides resolves.
"""

import ast
import datetime as dt
import importlib
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
import pytest

from local_data_platform import Config
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.spec import DEFAULT_KIND, kind_of_file
from tests.support import load_example_module

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"
# Dataset configs only: a folder of configs may hold files of other kinds, such as workflows. A file whose kind cannot
# be read stays in the list, so the tests below report it.
CONFIGS = sorted(path for path in EXAMPLES.glob("**/config/*.json") if kind_of_file(path) in (DEFAULT_KIND, None))
SCRIPTS = sorted(EXAMPLES.glob("**/reports/*.py"))
DOCS_WITH_CODE = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "quickstart.md",
    REPO_ROOT / "docs" / "design" / "factory_registry.md",
    REPO_ROOT / "docs" / "recipes.md",
    EXAMPLES / "README.md",
]

# The pipeline each example config must reach, from the built-in routes in docs/design/v0_1_1.md.
EXPECTED_PIPELINES = {
    "near_data_lake/config/ingestion.json": "BigQueryToCSV",
    "near_data_lake/config/egression.json": "CSVToIceberg",
    "nyc_yellow_taxi_dataset/config/ingestion.json": "ParquetToIceberg",
    "nyc_yellow_taxi_dataset/config/egression.json": "IcebergToCSV",
    "restaurant/config/restaurants_to_csv.json": "ExcelToCSV",
    "restaurant/config/restaurants.json": "ExcelToIceberg",
    "restaurant/config/customers.json": "CSVToIceberg",
    "restaurant/config/menu_items.json": "CSVToIceberg",
    "restaurant/config/reviews.json": "CSVToIceberg",
    "restaurant/config/orders.json": "CSVToIceberg",
    "restaurant/config/order_lines.json": "CSVToIceberg",
}

# Inputs in these formats are committed next to the example; Parquet is a documented download.
COMMITTED_INPUT_FORMATS = {"CSV", "JSON"}
# The restaurant example's generator writes its inputs into the work folder, so none of them is committed.
GENERATED_INPUTS = ("restaurant/",)


def _example_id(path: Path) -> str:
    return path.relative_to(EXAMPLES).as_posix()


def _config_paths(config: Config):
    """Yield ``(where, raw_path)`` for every path a config holds."""
    for section in ("source", "target"):
        block = config.metadata[section]
        if "path" in block:
            yield f"{section}.path", block["path"]
        if "credentials" in block:
            yield f"{section}.credentials.path", block["credentials"]["path"]
        if "catalog" in block:
            yield f"{section}.catalog.warehouse_path", block["catalog"]["warehouse_path"]


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(f"example_{path.parent.parent.name}_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _documented_imports():
    found = set()
    for doc in DOCS_WITH_CODE:
        for block in re.findall(r"```python\n(.*?)```", doc.read_text(), flags=re.DOTALL):
            for node in ast.walk(ast.parse(textwrap.dedent(block))):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("local_data_platform"):
                    found.update((doc.relative_to(REPO_ROOT).as_posix(), node.module, a.name) for a in node.names)
                elif isinstance(node, ast.Import):
                    found.update((doc.relative_to(REPO_ROOT).as_posix(), a.name, None)
                                 for a in node.names if a.name.startswith("local_data_platform"))
    return sorted(found, key=lambda item: (item[0], item[1], item[2] or ""))


DOCUMENTED_IMPORTS = _documented_imports()


def test_every_example_config_is_covered():
    assert {_example_id(path) for path in CONFIGS} == set(EXPECTED_PIPELINES)


@pytest.mark.parametrize("path", CONFIGS, ids=_example_id)
def test_config_loads_and_validates(path):
    config = Config.from_json(path)
    config.validate()
    assert config.base_dir == path.parent
    assert config.quality["on_failure"] in ("fail", "warn")


@pytest.mark.parametrize("path", CONFIGS, ids=_example_id)
def test_config_quality_checks_are_known(path):
    from local_data_platform.quality import checks_from_config

    config = Config.from_json(path)
    checks = checks_from_config(config.quality["checks"])
    assert len(checks) == len(config.quality["checks"])


@pytest.mark.parametrize("path", CONFIGS, ids=_example_id)
def test_config_paths_stay_inside_the_example(path):
    config = Config.from_json(path)
    example_dir = path.parent.parent
    for where, raw in _config_paths(config):
        assert not raw.startswith("/"), f"{where} {raw!r} uses the legacy leading slash"
        if raw.startswith("~"):
            # Secrets such as service-account keys live outside the repo on purpose.
            assert where.endswith("credentials.path"), f"{where} {raw!r} points outside the example"
            continue
        assert config.resolve(raw).is_relative_to(example_dir), f"{where} {raw!r} escapes {example_dir}"


@pytest.mark.parametrize("path", CONFIGS, ids=_example_id)
def test_committed_inputs_exist(path):
    config = Config.from_json(path)
    source = config.source
    if _example_id(path).startswith(GENERATED_INPUTS):
        pytest.skip("the example's generator writes the input")
    if source["format"] not in COMMITTED_INPUT_FORMATS:
        pytest.skip(f"{source['format']} input is not committed")
    assert config.resolve(source["path"]).is_file()


@pytest.mark.parametrize("path", CONFIGS, ids=_example_id)
def test_config_names_a_registered_pipeline(path):
    from local_data_platform.pipeline.registry import get_pipeline_class

    config = Config.from_json(path)
    pipeline_class = get_pipeline_class(config.source["format"], config.target["format"], config.source.get("engine"))
    assert pipeline_class.__name__ == EXPECTED_PIPELINES[_example_id(path)]


@pytest.mark.parametrize("path", SCRIPTS, ids=_example_id)
def test_report_script_imports_without_running(path):
    module = _load_script(path)
    assert callable(module.main)
    assert Path(module.CONFIG_PATH).is_file()


def test_near_put_data_runs_offline_and_is_idempotent(tmp_path):
    example = tmp_path / "near_data_lake"
    shutil.copytree(EXAMPLES / "near_data_lake", example, ignore=shutil.ignore_patterns("warehouse", "__pycache__"))
    script = _load_script(example / "reports" / "put_data.py")
    config_path = example / "config" / "egression.json"

    first = script.put_near_transaction_dataset(config_path)
    second = script.put_near_transaction_dataset(config_path)

    assert first.rows_read == second.rows_read == 5
    assert first.write_result.rows_after == 5
    assert second.write_result.rows_after == 5, "re-running the upsert changed the row count"
    assert first.quality.passed and second.quality.passed
    assert (example / "warehouse").is_dir()


def test_nyc_example_runs_offline_on_a_synthetic_month(tmp_path):
    """put_data.py then get_data.py on a small Parquet file shaped like the TLC download."""
    example = tmp_path / "nyc_yellow_taxi_dataset"
    shutil.copytree(EXAMPLES / "nyc_yellow_taxi_dataset", example,
                    ignore=shutil.ignore_patterns("warehouse", "data", "__pycache__"))
    start = dt.datetime(2023, 1, 1)
    pickups = [start + dt.timedelta(hours=7 * i) for i in range(40)]  # 40 rides over 12 days
    trips = pa.table({
        "VendorID": pa.array([1 + i % 2 for i in range(40)], pa.int64()),
        "tpep_pickup_datetime": pa.array(pickups, pa.timestamp("us")),
        "tpep_dropoff_datetime": pa.array([p + dt.timedelta(minutes=20) for p in pickups], pa.timestamp("us")),
        "fare_amount": [-5.0 if i == 3 else 10.0 + i for i in range(40)],  # the real data has negative fares
    })
    (example / "data").mkdir()
    pq.write_table(trips, example / "data" / "yellow_tripdata_2023-01.parquet")
    put = _load_script(example / "reports" / "put_data.py")
    get = _load_script(example / "reports" / "get_data.py")

    first = put.put_nyc_yellow_taxi_dataset(example / "config" / "ingestion.json")
    second = put.put_nyc_yellow_taxi_dataset(example / "config" / "ingestion.json")
    exported = get.get_nyc_yellow_taxi_dataset(example / "config" / "egression.json")

    assert first.write_result.rows_after == second.write_result.rows_after == 40, "overwrite is not idempotent"
    assert not first.quality.passed, "the negative fare should fail the range check"
    assert [r.name for r in first.quality.failures] == ["range(fare_amount)"]  # on_failure=warn: written anyway
    table = Iceberg("rides", {"identifier": "nyc_yellow_taxi_dataset", "warehouse_path": str(example / "warehouse")})
    assert [field.transform.__class__.__name__ for field in table.table().spec().fields] == ["DayTransform"]
    assert table.table().inspect.partitions().num_rows == 12
    assert exported.rows_written == 40
    csv = pa_csv.read_csv(example / "data" / "exports" / "nyc_yellow_taxi_rides.csv")
    assert csv.num_rows == 40


def test_the_monitoring_layer_of_the_restaurant_example_runs_offline_and_raises_and_resolves_its_alerts(
        tmp_path, monkeypatch, capsys):
    """``run.py --layer monitoring`` without dbt: a late table, a short batch and a new column, with no connection."""
    run = load_example_module("example_restaurant_run", EXAMPLES / "restaurant" / "run.py")
    monkeypatch.setattr(run, "dbt_installed", lambda: False)
    monkeypatch.delenv("LDP_SLACK_WEBHOOK", raising=False)

    def refuse(self, address):
        raise AssertionError(f"the example opened a connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)

    status = run.main(["--workdir", str(tmp_path / "ldp_demo" / "restaurant"), "--layer", "monitoring"])

    out = capsys.readouterr().out
    assert status == 0
    alerts = tmp_path / "ldp_demo" / "restaurant" / ".ldp" / "alerts.jsonl"
    events = [(line["event"], line["monitor"]) for line in map(json.loads, alerts.read_text().splitlines())]
    assert events == [
        ("monitor.error", "freshness"),
        ("monitor.resolved", "freshness"), ("monitor.error", "volume.rows"),
        ("monitor.resolved", "volume.rows"), ("monitor.warn", "schema"),
        ("monitor.resolved", "schema")]
    assert "Skipping the revenue step: dbt is not installed" in out
    assert (tmp_path / "ldp_demo" / "restaurant" / "status.html").is_file()
    assert out.count("Alerts sent to file") == 4, "the fresh night and the second look had nothing to send"


@pytest.mark.dbt
def test_the_revenue_step_of_the_monitoring_layer_reads_the_gold_table_and_raises_and_resolves_its_alert(
        tmp_path, network_trap):
    """The same script with dbt: the revenue of a day with a revenue drop is an error, and a fresh day resolves it."""
    environment, connections = network_trap
    work = tmp_path / "ldp_demo" / "restaurant"

    done = subprocess.run([sys.executable, str(EXAMPLES / "restaurant" / "run.py"), "--workdir", str(work),
                           "--layer", "monitoring"], capture_output=True, text=True, env=environment, cwd=tmp_path)

    assert done.returncode == 0, done.stderr
    assert connections == []  # not one request to a proxy, so dbt and ldp stayed offline
    assert "revenue for day 2026-08-08 was 633, below the 1000 that min asks for" in done.stdout
    assert "revenue for day 2026-08-09 was 6330, inside the limits" in done.stdout
    events = [(line["event"], line["monitor"])
              for line in map(json.loads, (work / ".ldp" / "alerts.jsonl").read_text().splitlines())]
    assert {("monitor.error", "metric.revenue.value"), ("monitor.resolved", "metric.revenue.value")} <= set(events)
    assert len(events) == 8


@pytest.mark.parametrize(
    "doc, module, name", DOCUMENTED_IMPORTS,
    ids=[f"{doc}:{module}{'.' + name if name else ''}" for doc, module, name in DOCUMENTED_IMPORTS],
)
def test_documented_import_resolves(doc, module, name):
    imported = importlib.import_module(module)
    if name is not None and not hasattr(imported, name):
        # ``from package import submodule`` is fine too; this raises if it is neither.
        importlib.import_module(f"{module}.{name}")
