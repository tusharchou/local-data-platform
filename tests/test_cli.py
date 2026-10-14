"""The ``ldp`` CLI, called through ``main([...])``: return codes, output and error handling."""

import argparse
import datetime as dt
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pa_csv
import pytest

from local_data_platform import __version__
from local_data_platform.cli import (CORE_COMMANDS, MODULE_COMMANDS, _needs_module_commands, build_parser,
                                     format_table, main)
from local_data_platform.exceptions import EngineNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import commit_log, staged_branches
from local_data_platform.logger import PACKAGE_LOGGER

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = {"identifier": "ns", "warehouse_path": "wh"}


def _config(folder: Path, checks: list | None = None, write_mode: str = "upsert", target: dict | None = None) -> Path:
    path = folder / "rides.json"
    path.write_text(json.dumps({"identifier": "rides", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": target or {"name": "rides", "format": "ICEBERG", "catalog": CATALOG,
                             "write_mode": write_mode, "join_cols": ["ride_id"]},
        "quality": {"on_failure": "fail", "checks": checks or [{"check": "unique", "columns": ["ride_id"]}]},
    }}))
    return path


@pytest.fixture
def config_path(tmp_path, sample_table) -> Path:
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    return _config(tmp_path)


def _table(folder: Path) -> Iceberg:
    return Iceberg("rides", CATALOG, base_dir=folder)


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"ldp {__version__}"


def test_help_exits_zero(capsys):
    assert main(["--help"]) == 0
    assert "pipelines" in capsys.readouterr().out


def test_no_command_prints_help_and_fails(capsys):
    assert main([]) == 1
    assert "COMMAND" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["frobnicate"], ["run"], ["run", "x.json", "--mode", "merge"],
                                  ["run", "x.json", "--run-id", "r", "--attempt", "0"],
                                  ["query", "x.json"], ["demo", "--rows", "0"]])
def test_usage_errors_exit_one(argv, capsys):
    assert main(argv) == 1
    assert "usage: ldp" in capsys.readouterr().err


def test_pipelines_lists_every_builtin(capsys):
    assert main(["pipelines"]) == 0
    out = capsys.readouterr().out
    for name in ("CSVToIceberg", "ParquetToIceberg", "IcebergToCSV", "IcebergToParquet", "BigQueryToCSV",
                 "DuckDBToIceberg", "ExcelToCSV", "ExcelToIceberg"):
        assert name in out
    assert "BIGQUERY" in out
    assert "(8 rows)" in out


def test_run_loads_the_config_and_prints_a_summary(config_path, capsys):
    assert main(["run", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "rides: read 6 rows, wrote 6 rows" in out
    assert "target ns.rides: upsert, 0 -> 6 rows" in out
    assert "Data quality: 1 of 1 checks passed" in out
    assert _table(config_path.parent).row_count() == 6


def test_run_mode_overrides_the_config(config_path, capsys):
    assert main(["run", str(config_path), "--mode", "append"]) == 0
    assert main(["run", str(config_path), "--mode", "append"]) == 0
    assert _table(config_path.parent).row_count() == 12
    assert main(["run", str(config_path), "--mode", "overwrite"]) == 0
    assert "overwrite, 12 -> 6 rows" in capsys.readouterr().out


def test_a_failing_check_exits_three_without_a_traceback_and_writes_nothing(tmp_path, sample_table, capsys):
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    path = _config(tmp_path, checks=[{"check": "row_count", "min": 100}, {"check": "not_null", "columns": ["x"]}])

    assert main(["run", str(path)]) == 3

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ldp: error: 2 of 2 data quality checks failed:")
    assert "row_count" in captured.err and "not_null(x)" in captured.err
    assert "Traceback" not in captured.err
    assert not _table(tmp_path).exists()


# --------------------------------------------------------------------------- ldp run with a window and a retry identity

WINDOW = "2026-08-01/2026-08-02"
SQL_CATALOG = {"type": "sql", "uri": "sqlite:///catalog.db", "warehouse": "wh", "namespace": "ns"}


def _staged_config(folder: Path, sample_table, catalog=None, checks=None) -> Path:
    """A config that appends, with the six sample rides in ``rides.csv`` next to it."""
    pa_csv.write_csv(sample_table, folder / "rides.csv")
    target = {"name": "rides", "format": "ICEBERG", "catalog": catalog or CATALOG, "write_mode": "append"}
    return _config(folder, target=target, checks=checks or [{"check": "row_count", "min": 1}])


def _fields(out: str) -> dict[str, str]:
    """The ``name  value`` lines that a staged run prints under its summary."""
    names = ("window", "idempotency_key", "run_id", "attempt", "status", "snapshot")
    found = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in names:
            found[parts[0]] = parts[1].strip()
    return found


def _head(folder: Path) -> int:
    return _table(folder).table().current_snapshot().snapshot_id


def test_a_run_with_a_window_publishes_and_prints_the_window_the_key_the_status_and_the_snapshot(
        tmp_path, sample_table, capsys):
    from local_data_platform import Config
    from local_data_platform.spec import idempotency_key

    path = _staged_config(tmp_path, sample_table)

    assert main(["run", str(path), "--window", WINDOW]) == 0

    out = capsys.readouterr().out
    fields = _fields(out)
    assert out.startswith("rides: read 6 rows, wrote 6 rows")
    assert fields["window"] == "2026-08-01T00:00:00Z/2026-08-02T00:00:00Z"
    assert fields["idempotency_key"] == idempotency_key(Config.from_json(path), WINDOW)
    assert (fields["status"], fields["attempt"]) == ("published", "1")
    assert fields["snapshot"] == str(_head(tmp_path)) and _table(tmp_path).row_count() == 6


def test_running_the_same_window_again_prints_skipped_duplicate_and_writes_nothing(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table)
    assert main(["run", str(path), "--window", WINDOW]) == 0
    first = _fields(capsys.readouterr().out)
    snapshots = len(_table(tmp_path).table().snapshots())

    assert main(["run", str(path), "--window", WINDOW]) == 0

    out = capsys.readouterr().out
    second = _fields(out)
    assert "skipped: idempotency key already published in snapshot" in out
    assert second["status"] == "skipped_duplicate" and second["snapshot"] == first["snapshot"]
    assert second["idempotency_key"] == first["idempotency_key"]
    assert _table(tmp_path).row_count() == 6 and len(_table(tmp_path).table().snapshots()) == snapshots


def test_a_second_run_of_a_published_window_does_not_read_its_source_and_says_so(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table)
    assert main(["run", str(path), "--window", WINDOW]) == 0
    first = _fields(capsys.readouterr().out)
    (tmp_path / "rides.csv").unlink()  # a run that read its source would fail now

    assert main(["run", str(path), "--window", WINDOW]) == 0

    out = capsys.readouterr().out
    assert out.startswith("rides: skipped: idempotency key already published in snapshot")
    assert "rows" not in out.splitlines()[0]  # it did not read any
    fields = _fields(out)
    assert (fields["status"], fields["snapshot"]) == ("skipped_duplicate", first["snapshot"])
    assert main(["run", str(path), "--window", WINDOW, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["status"], data["rows_read"], data["rows_written"]) == ("skipped_duplicate", 0, 0)
    assert _table(tmp_path).row_count() == 6


def test_a_different_window_is_a_different_run_and_publishes(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table)
    assert main(["run", str(path), "--window", WINDOW]) == 0
    assert main(["run", str(path), "--window", "2026-08-02/2026-08-03"]) == 0

    assert _fields(capsys.readouterr().out)["status"] == "published" and _table(tmp_path).row_count() == 12


def test_ldp_plan_and_ldp_run_print_the_same_idempotency_key_for_a_window(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table)

    assert main(["plan", str(path), "--window", WINDOW]) == 0
    planned = _fields(capsys.readouterr().out)["idempotency_key"]
    assert main(["run", str(path), "--window", WINDOW]) == 0

    assert _fields(capsys.readouterr().out)["idempotency_key"] == planned
    assert main(["run", str(path), "--window", WINDOW, "--idempotency-key", "auto"]) == 0
    again = _fields(capsys.readouterr().out)
    assert again["idempotency_key"] == planned and again["status"] == "skipped_duplicate"


def test_an_explicit_idempotency_key_is_used_as_given_and_it_alone_makes_the_run_staged(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table)

    assert main(["run", str(path), "--idempotency-key", "from-airflow-2026-08-01"]) == 0
    first = _fields(capsys.readouterr().out)
    assert first["idempotency_key"] == "from-airflow-2026-08-01" and first["status"] == "published"
    assert "window" not in first

    assert main(["run", str(path), "--idempotency-key", "from-airflow-2026-08-01"]) == 0
    assert _fields(capsys.readouterr().out)["status"] == "skipped_duplicate"
    assert _table(tmp_path).row_count() == 6


@pytest.mark.parametrize("flags, message", [
    (["--window", "yesterday-ish"], "window 'yesterday-ish' must be an ISO-8601 interval 'START/END'"),
    (["--window", "2026-08-02/2026-08-01"], "window end 2026-08-01T00:00:00+00:00 must be after its start"),
    (["--attempt", "2"], "--attempt needs --run-id: a retry reuses the run id of the attempt before it"),
    (["--run-id", "r1"], "--run-id needs --window or --idempotency-key, because only a staged run has attempts"),
    (["--idempotency-key", "auto"], "--idempotency-key auto needs --window, because the key is derived from "
                                    "the window"),
    (["--window", WINDOW, "--idempotency-key", "  "], "idempotency_key must be a non-empty string"),
])
def test_a_bad_window_or_retry_identity_exits_one_with_one_friendly_line_and_writes_nothing(
        tmp_path, sample_table, capsys, flags, message):
    path = _staged_config(tmp_path, sample_table)

    assert main(["run", str(path), *flags]) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count("\n") == 1
    assert captured.err.startswith(f"ldp: error: {message}"), captured.err
    assert not _table(tmp_path).exists()


def test_a_retry_reuses_the_run_id_with_the_next_attempt(tmp_path, sample_table, capsys):
    from local_data_platform.events import uuid7

    path = _staged_config(tmp_path, sample_table)
    run = uuid7()

    assert main(["run", str(path), "--window", WINDOW, "--run-id", run, "--attempt", "2"]) == 0

    fields = _fields(capsys.readouterr().out)
    assert (fields["run_id"], fields["attempt"], fields["status"]) == (run, "2", "published")
    summary = _table(tmp_path).table().current_snapshot().summary
    assert summary["ldp.run-id"] == run and summary["ldp.attempt"] == "2"


def test_a_staged_branch_that_attempt_one_left_is_fenced_and_never_reaches_main(tmp_path, sample_table, capsys):
    from local_data_platform import Config
    from local_data_platform.events import uuid7
    from local_data_platform.format.iceberg.commit import CommitContext, fence_marker, stage, staged_branches
    from local_data_platform.spec import idempotency_key

    path = _staged_config(tmp_path, sample_table)
    assert main(["run", str(path), "--window", "2026-07-31/2026-08-01"]) == 0  # the table has a main to branch from
    capsys.readouterr()
    run, window = uuid7(), "2026-08-01/2026-08-02"
    key = idempotency_key(Config.from_json(path), window)
    zombie = stage(_table(tmp_path).table(), sample_table.slice(0, 2), "append",
                   CommitContext.create(key, run_id=run, attempt=1))  # attempt 1 staged its rows and then died
    assert zombie.branch in _table(tmp_path).table().metadata.refs
    assert _table(tmp_path).row_count() == 6

    assert main(["run", str(path), "--window", window, "--run-id", run, "--attempt", "2"]) == 0

    refs = _table(tmp_path).table().metadata.refs
    assert _fields(capsys.readouterr().out)["status"] == "published"
    assert zombie.branch not in refs and fence_marker(run, 1) in refs
    assert staged_branches(_table(tmp_path).table()) == {}
    assert _table(tmp_path).row_count() == 12  # the six rows of the first window and the six of the retry, not the two


def test_json_prints_the_result_and_nothing_else_on_stdout(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table)

    assert main(["run", str(path), "--window", WINDOW, "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["run", str(path), "--window", WINDOW, "--json"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert (first["status"], second["status"]) == ("published", "skipped_duplicate")
    assert first["rows_written"] == 6 and first["name"] == "rides" and first["quality"]["passed"] is True
    assert first["idempotency_key"] == second["idempotency_key"] and len(first["idempotency_key"]) == 64
    assert first["published_snapshot_id"] == second["published_snapshot_id"] == _head(tmp_path)
    assert first["run_id"] != second["run_id"]


def test_json_of_a_run_without_a_window_has_no_key_and_the_text_output_is_unchanged_without_the_flags(
        tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table)

    assert main(["run", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["status"], result["idempotency_key"]) == ("published", None)

    assert main(["run", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("rides: read 6 rows, wrote 6 rows")
    assert _fields(out) == {}  # no window, key, run id, status or snapshot block: the direct run prints what it did


def test_a_failing_check_in_a_staged_run_exits_three_too_and_publishes_nothing(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table, checks=[{"check": "row_count", "min": 100}])

    assert main(["run", str(path), "--window", WINDOW]) == 3

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("ldp: error: 1 of 1 data quality checks failed:")
    assert not _table(tmp_path).exists()


def test_exit_status_three_keeps_the_traceback_with_verbose(tmp_path, sample_table, capsys):
    path = _staged_config(tmp_path, sample_table, checks=[{"check": "row_count", "min": 100}])

    assert main(["-v", "run", str(path)]) == 3

    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err and "data quality checks failed" in err


def test_the_exit_code_table_is_in_the_module_docstring_and_the_readme():
    from local_data_platform import cli

    readme = (REPO_ROOT / "README.md").read_text()
    assert (cli.EXIT_OK, cli.EXIT_ERROR, cli.EXIT_SLA, cli.EXIT_GATE) == (0, 1, 2, 3)
    for text in (cli.__doc__, readme):
        for words in ("success, including a run that published and a run that skipped a duplicate",
                      "reserved for an SLA breach that `ldp monitor` reports",
                      "a data gate held the run back, such as a quality check that blocked a write",
                      "never retries one that exits with 3"):
            assert words.replace("`", "") in text.replace("`", ""), words


# --------------------------------------------------------------------------- two processes, one key

_CHILD = """
import pathlib, sys, time
from local_data_platform.cli import main
import local_data_platform.etl, local_data_platform.pipeline.registry, local_data_platform.format.iceberg.commit
ready, go = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
ready.write_text("ready")
while not go.exists():
    time.sleep(0.002)
sys.exit(main(sys.argv[3:]))
"""


def _race(config: Path, *argv: str, processes: int = 2) -> list[dict]:
    """Start ``processes`` runs of ``ldp run`` at the same moment and return what each printed with ``--json``."""
    go = config.parent / "go"
    children = [subprocess.Popen([sys.executable, "-c", _CHILD, str(config.parent / f"ready{n}"), str(go),
                                  "run", str(config), *argv, "--json"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=config.parent)
                for n in range(processes)]
    deadline = time.monotonic() + 120
    while not all((config.parent / f"ready{n}").exists() for n in range(processes)):
        assert time.monotonic() < deadline, "the processes did not get ready"
        time.sleep(0.01)
    go.write_text("go")
    outputs = [child.communicate(timeout=300) for child in children]
    assert [child.returncode for child in children] == [0] * processes, outputs
    return [json.loads(out) for out, _ in outputs]


@pytest.mark.parametrize("catalog", [CATALOG, SQL_CATALOG], ids=["local catalog", "sql catalog on SQLite"])
def test_two_processes_with_the_same_key_write_the_batch_once(tmp_path, sample_table, catalog):
    path = _staged_config(tmp_path, sample_table, catalog=catalog)

    results = _race(path, "--window", WINDOW)

    assert sorted(result["status"] for result in results) == ["published", "skipped_duplicate"], results
    assert len({result["idempotency_key"] for result in results}) == 1
    assert len({result["published_snapshot_id"] for result in results}) == 1
    table = Iceberg("rides", catalog, base_dir=tmp_path)
    assert table.row_count() == 6
    # One publish on main carries the key. The metadata may also hold the staged snapshot of the writer that lost, which
    # is on no branch of main, so only the snapshots in main's ancestry count.
    assert [row["idempotency_key"] for row in commit_log(table.table())] == [results[0]["idempotency_key"]]
    assert staged_branches(table.table()) == {}


@pytest.mark.parametrize("argv", [["-v", "run"], ["run", "-v"]])
def test_verbose_prints_the_traceback_before_or_after_the_command(tmp_path, capsys, argv):
    missing = str(tmp_path / "missing.json")
    assert main([*argv, missing]) == 1
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert err.rstrip().endswith(f"ldp: error: config file not found: {tmp_path / 'missing.json'}")


def test_a_missing_config_is_one_friendly_line(tmp_path, capsys):
    assert main(["run", str(tmp_path / "missing.json")]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("ldp: error: config file not found")


def test_unexpected_errors_name_their_type_and_suggest_verbose(tmp_path, capsys):
    (tmp_path / "rides.csv").write_text("ride_id,city\n1,NYC\n")
    path = _config(tmp_path, target={"name": "rides", "format": "ICEBERG", "catalog": CATALOG,
                                     "write_mode": "upsert", "join_cols": ["missing_col"]}, checks=[])
    assert main(["run", str(path)]) == 1
    err = capsys.readouterr().err
    assert "join_cols" in err
    assert "Traceback" not in err


def test_snapshots_lists_the_target_snapshots(config_path, capsys):
    main(["run", str(config_path), "--mode", "append"])
    main(["run", str(config_path), "--mode", "overwrite"])
    capsys.readouterr()

    assert main(["snapshots", str(config_path)]) == 0

    out = capsys.readouterr().out
    assert "Snapshots of Iceberg table ns.rides" in out
    snapshots = _table(config_path.parent).snapshots()
    assert len(snapshots) >= 3  # append, then an overwrite (pyiceberg commits it as delete + append)
    for snapshot in snapshots:
        row = next(line for line in out.splitlines() if line.lstrip().startswith(str(snapshot["snapshot_id"])))
        assert snapshot["operation"] in row.split()
    assert f"({len(snapshots)} rows)" in out


def test_snapshots_of_a_table_that_does_not_exist_fails(config_path, capsys):
    assert main(["snapshots", str(config_path)]) == 1
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["snapshots"], ["query", "SELECT 1"]])
def test_reads_on_a_missing_warehouse_fail_without_creating_a_catalog(config_path, capsys, command):
    argv = [command[0], str(config_path), *command[1:]]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("ldp: error: Iceberg table ns.rides does not exist: there is no catalog at")
    assert err.count("\n") == 1
    assert not (config_path.parent / "wh").exists(), "a read-only command created the warehouse"


def test_snapshots_uses_an_iceberg_source_when_the_target_is_a_file(config_path, capsys):
    main(["run", str(config_path)])
    export = config_path.parent / "export.json"
    export.write_text(json.dumps({"identifier": "export", "metadata": {
        "source": {"name": "rides", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "rides", "format": "CSV", "path": "out.csv"},
    }}))
    capsys.readouterr()
    assert main(["snapshots", str(export)]) == 0
    assert "ns.rides" in capsys.readouterr().out


def test_snapshots_needs_an_iceberg_table(tmp_path, capsys):
    path = _config(tmp_path, target={"name": "out", "format": "PARQUET", "path": "out.parquet"})
    assert main(["snapshots", str(path)]) == 1
    assert "has no Iceberg table" in capsys.readouterr().err


def test_query_registers_the_table_under_the_target_name(config_path, capsys):
    main(["run", str(config_path)])
    capsys.readouterr()

    sql = "SELECT city, count(*) AS rides FROM rides GROUP BY city ORDER BY city"
    assert main(["query", str(config_path), sql]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["city", "rides"]
    assert [line.split() for line in out[2:5]] == [["BKK", "2"], ["LDN", "2"], ["NYC", "2"]]
    assert out[-1] == "(3 rows)"


def test_query_max_rows_truncates_the_output(config_path, capsys):
    main(["run", str(config_path)])
    capsys.readouterr()
    assert main(["query", str(config_path), "SELECT * FROM rides ORDER BY ride_id", "--max-rows", "2"]) == 0
    out = capsys.readouterr().out
    assert "... 4 more rows" in out and "(6 rows)" in out


FALLBACK_WARNING = "WARNING local_data_platform.engine.duckdb: Registering Iceberg tables in memory, because "


def _assert_one_friendly_error_line(err: str, expected_start: str) -> None:
    """The error starts on one friendly line; before it, at most DuckDB's in-memory fallback warning.

    Without DuckDB's iceberg extension (the offline default, and CI), ``ldp query`` first warns once
    that it registers the table in memory (contract C5); with the extension it says nothing. The
    engine's own message may continue on further lines (DuckDB points at the bad SQL).
    """
    lines = err.splitlines()
    [start] = [i for i, line in enumerate(lines) if line.startswith("ldp: error:")]
    assert lines[start].startswith(expected_start)
    assert "(run with -v for the traceback)" in lines[start]
    assert start <= 1 and all(line.startswith(FALLBACK_WARNING) for line in lines[:start]), lines[:start]
    assert "Traceback" not in err


def test_bad_sql_exits_one_with_the_engine_message(config_path, capsys):
    main(["run", str(config_path)])
    capsys.readouterr()
    assert main(["query", str(config_path), "SELECT nope FROM rides"]) == 1
    _assert_one_friendly_error_line(capsys.readouterr().err, "ldp: error: BinderException")


def test_bad_sql_without_the_iceberg_extension_warns_once_then_errors(config_path, capsys, monkeypatch):
    from local_data_platform.engine.duckdb import DuckDBEngine

    main(["run", str(config_path)])
    capsys.readouterr()
    monkeypatch.setattr(DuckDBEngine, "_load_iceberg_extension", lambda self: "the extension is not installed")
    assert main(["query", str(config_path), "SELECT nope FROM rides"]) == 1
    err = capsys.readouterr().err
    _assert_one_friendly_error_line(err, "ldp: error: BinderException")
    assert err.startswith(FALLBACK_WARNING + "the extension is not installed")


def test_query_without_duckdb_prints_the_install_hint(config_path, capsys, monkeypatch):
    import local_data_platform.engine.duckdb as duckdb_engine

    def missing():
        raise EngineNotFound(f"The DuckDB engine needs duckdb. Install it with: {duckdb_engine.INSTALL_HINT}")

    main(["run", str(config_path)])
    capsys.readouterr()
    monkeypatch.setattr(duckdb_engine, "_import_duckdb", missing)
    assert main(["query", str(config_path), "SELECT 1"]) == 1
    assert 'pip install "local-data-platform[duckdb]"' in capsys.readouterr().err


def test_demo_command_runs_into_the_workdir(tmp_path, capsys):
    workdir = tmp_path / "demo"
    assert main(["demo", "--workdir", str(workdir), "--rows", "40", "--seed", "7"]) == 0
    out = capsys.readouterr().out
    assert "[8/8] Export back to CSV" in out and "Summary" in out
    assert (workdir / "rides.json").is_file()


def test_main_leaves_logging_as_it_found_it(config_path):
    package_logger = logging.getLogger(PACKAGE_LOGGER)
    before = (package_logger.level, list(package_logger.handlers))
    main(["-v", "run", str(config_path)])
    main(["run", str(config_path / "missing")])
    assert (package_logger.level, list(package_logger.handlers)) == before


def test_verbose_enables_debug_logging(config_path, capsys):
    assert main(["-v", "run", str(config_path)]) == 0
    assert "DEBUG local_data_platform" in capsys.readouterr().err


def test_python_dash_m_runs_the_cli():
    completed = subprocess.run([sys.executable, "-m", "local_data_platform.cli", "--version"],
                               capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"ldp {__version__}"


# ---------------------------------------------------------------------- module commands (platform contract C9)

MODULE_COMMAND_NAMES = ("catalog", "schema", "plan", "runs", "commits", "datasets", "maintain", "spark", "mcp", "dbt",
                        "workflow", "backfill")


def _command_names(parser) -> list[str]:
    [commands] = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    return list(commands.choices)


def test_every_module_command_is_registered():
    assert _command_names(build_parser()) == [*CORE_COMMANDS, *MODULE_COMMAND_NAMES]
    assert [name for _, names in MODULE_COMMANDS for name in names] == list(MODULE_COMMAND_NAMES)
    assert _command_names(build_parser(module_commands=False)) == list(CORE_COMMANDS)


def test_help_lists_every_command_with_the_shared_verbose_flag(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in (*CORE_COMMANDS, *MODULE_COMMAND_NAMES):
        assert f"    {name} " in out, name
    # A sub-command must not take over the shared -v (a conflict "resolve" would strip it here).
    assert "-v, --verbose" in out


@pytest.mark.parametrize("argv", [["catalog", "test", "--help"], ["schema", "--help"], ["plan", "--help"],
                                  ["runs", "--help"], ["commits", "--help"], ["datasets", "pin", "--help"],
                                  ["maintain", "--help"], ["spark", "--help"], ["mcp", "--help"],
                                  ["dbt", "--help"], ["dbt", "build", "--help"]])
def test_module_command_help_exits_zero_and_offers_verbose(argv, capsys):
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: ldp {' '.join(argv[:-1])}")
    assert "-v, --verbose" in out


def test_an_unknown_command_lists_the_module_commands_too(capsys):
    assert main(["catalg", "test"]) == 1
    err = capsys.readouterr().err
    assert "invalid choice: 'catalg'" in err
    # Whether argparse quotes the choices depends on the Python version (3.12.15 doesn't, 3.13 does).
    choices = {choice.strip(" '") for choice in err.split("choose from ", 1)[1].rstrip(")\n").split(",")}
    assert {"maintain", "mcp"} <= choices


@pytest.mark.parametrize("argv, needed", [
    (["--version"], False), (["-v", "--version"], False), (["run", "x.json"], False),
    (["-v", "query", "x", "y"], False), (["demo"], False), (["pipelines"], False), (["schema"], True),
    (["-v", "maintain", "x.json"], True), (["frobnicate"], True), (["--help"], True),
    (["--version", "--help"], True), ([], True), (["-v"], True),
])
def test_module_commands_are_imported_only_when_needed(argv, needed):
    assert _needs_module_commands(argv) is needed


def test_version_and_core_commands_do_not_import_pyiceberg_or_the_module_commands():
    probe = (
        "import contextlib, io, json, sys\n"
        "from local_data_platform.cli import main\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    code = main(['--version'])\n"
        "print(json.dumps({'code': code, 'pyiceberg': 'pyiceberg' in sys.modules,\n"
        "                  'maintenance': 'local_data_platform.maintenance' in sys.modules}))\n"
    )
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO_ROOT,
                               timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "code": 0, "pyiceberg": False, "maintenance": False}


def test_module_commands_run_end_to_end_through_main(config_path, capsys):
    assert main(["run", str(config_path)]) == 0
    capsys.readouterr()

    assert main(["catalog", "test", str(config_path)]) == 0
    out = capsys.readouterr()
    assert "tables in ns (1): ns.rides" in out.out and out.out.rstrip().endswith("ok")
    assert "DEBUG" not in out.err, "-v must stay off unless given"

    assert main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"].startswith("local-data-platform dataset spec")

    assert main(["plan", str(config_path), "--json"]) == 0
    assert '"identifier": "rides"' in capsys.readouterr().out

    assert main(["commits", str(config_path)]) == 0
    assert "LDP publishes on main of ns.rides" in capsys.readouterr().out

    assert main(["maintain", str(config_path)]) == 0
    assert "dry run" in capsys.readouterr().out

    assert main(["runs", str(config_path)]) == 0
    assert "No runs recorded" in capsys.readouterr().out

    assert main(["datasets", "pin", str(config_path), "train"]) == 0
    assert "Pinned train@v1" in capsys.readouterr().out
    assert main(["datasets", "list", "--config", str(config_path)]) == 0
    assert "train" in capsys.readouterr().out


def test_verbose_works_before_or_after_a_module_command(config_path, capsys):
    main(["run", str(config_path)])
    capsys.readouterr()
    for argv in (["-v", "commits", str(config_path)], ["commits", "-v", str(config_path)]):
        assert main(argv) == 0
        assert "DEBUG local_data_platform" in capsys.readouterr().err


SQL_CATALOG = {"type": "sql", "uri": "sqlite:///lake/catalog.db", "warehouse": "lake/wh", "namespace": "demo"}


@pytest.mark.parametrize("command", [["snapshots"], ["query", "SELECT 1"], ["commits"], ["maintain"],
                                     ["datasets", "pin", "@", "v"]])
def test_reads_on_a_missing_sql_catalog_fail_without_creating_it(tmp_path, capsys, command):
    path = _config(tmp_path, target={"name": "rides", "format": "ICEBERG", "catalog": SQL_CATALOG,
                                     "write_mode": "append"}, checks=[])
    argv = [str(path) if part == "@" else part for part in command]
    if "@" not in command:
        argv.insert(1, str(path))
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("ldp: error: Iceberg table demo.rides does not exist: there is no catalog at")
    assert not (tmp_path / "lake").exists(), "a read-only command created the SQLite catalog"


def test_catalog_test_on_a_missing_sql_catalog_fails_without_creating_it(tmp_path, capsys):
    (tmp_path / "catalog.json").write_text(json.dumps(SQL_CATALOG))
    assert main(["catalog", "test", str(tmp_path / "catalog.json")]) == 1
    assert "there is no sql catalog at" in capsys.readouterr().err
    assert not (tmp_path / "lake").exists()


def test_reads_work_on_an_existing_sql_catalog(tmp_path, sample_table, capsys):
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    path = _config(tmp_path, target={"name": "rides", "format": "ICEBERG", "catalog": SQL_CATALOG,
                                     "write_mode": "append"}, checks=[])
    assert main(["run", str(path)]) == 0
    capsys.readouterr()
    assert main(["snapshots", str(path)]) == 0
    assert "demo.rides" in capsys.readouterr().out
    assert main(["catalog", "test", str(path)]) == 0
    assert "tables in demo (1): demo.rides" in capsys.readouterr().out


# ---------------------------------------------------------------------- format_table


def test_format_table_aligns_columns_and_shows_nulls():
    table = pa.table({"name": ["a", "bbb", None], "n": [1, 22, None], "x": [1.5, 2.25, 3.0],
                      "at": [dt.datetime(2024, 1, 1, 8), None, None]})
    lines = format_table(table).splitlines()
    # Numbers are right-aligned; NULL counts towards a column's width.
    assert lines[0] == "name     n     x  at"
    assert lines[1] == "----  ----  ----  -------------------"
    assert lines[2] == "a        1   1.5  2024-01-01 08:00:00"
    assert lines[3] == "bbb     22  2.25  NULL"
    assert lines[4] == "NULL  NULL     3  NULL"
    assert lines[-1] == "(3 rows)"


def test_format_table_truncates_and_formats_floats():
    rows = [{"value": 1.0 * i} for i in range(5)]
    text = format_table(rows, max_rows=2, float_digits=2)
    assert text.splitlines()[2:] == [" 0.00", " 1.00", "... 3 more rows", "(5 rows)"]


def test_format_table_handles_empty_input():
    assert format_table([]) == "(no columns)"
    assert format_table(pa.table({"a": pa.array([], pa.int64())})).splitlines()[-1] == "(0 rows)"
