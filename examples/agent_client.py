"""A scripted AI-agent client for the read-only MCP server (``ldp mcp``).

It starts ``ldp mcp`` as a subprocess, talks to it over stdio with the ``mcp`` SDK's
client, and calls every tool the way an agent would: ``list_tables``, ``describe_table``,
``sample_rows``, ``table_history``, ``query``, ``get_dataset``, ``list_metrics`` and
``query_metric``. The metric tools compute a number that a metrics file defines, from the
current release of a gold table, and bind what the agent asks for as values. Then it tries
what an agent must not be able to do (``COPY``, reading ``/etc/passwd``, several statements,
``SET``, ``ATTACH``, a metric that does not exist) and checks that each one is refused, and
that SQL in a filter is only a value. Finally it reads the audit log.

Usage::

    python examples/agent_client.py                       # builds a small lake in a temp folder
    python examples/agent_client.py --config ldp_demo/rides.json   # serves an existing config
    python examples/agent_client.py --config ldp_demo/restaurant/config   # serves a folder with metrics
    make demo-agent                                       # runs 'ldp demo', then this script on it

It exits with 0 when every call behaved as expected and 1 otherwise. It needs the
``mcp`` and ``duckdb`` extras: ``pip install "local-data-platform[mcp,duckdb]"``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

NAMESPACE = "agent_demo"
DATASET = "rides_long_trips"
GOLD_NAMESPACE = "gold"
INJECTION = "NYC' OR '1'='1"
REFUSED_SQL = {
    "COPY to a file": "COPY (SELECT 1 AS x) TO 'stolen.csv'",
    "read /etc/passwd": "SELECT * FROM read_csv('/etc/passwd')",
    "two statements": "SELECT 1; SELECT 2",
    "change a setting": "SET enable_external_access = true",
    "attach a database": "ATTACH '/tmp/other.duckdb' AS other",
}


# ---------------------------------------------------------------------- the lake


def _write_rides(path: Path, day: dt.date, rows: int, start_id: int, seed: int) -> None:
    rng = random.Random(seed)
    cities = ["NYC", "BKK", "LDN"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ride_id", "city", "fare", "distance_km", "pickup_ts"])
        for offset in range(rows):
            pickup = dt.datetime.combine(day, dt.time(6)) + dt.timedelta(minutes=7 * offset)
            distance = round(rng.uniform(0.5, 25.0), 2)
            writer.writerow([start_id + offset, cities[offset % 3], round(3.0 + distance * 1.9, 2), distance,
                             pickup.isoformat(sep=" ")])


def build_lake(workdir: Path) -> Path:
    """Load two days of synthetic rides into ``agent_demo.rides`` and return the config path."""
    from local_data_platform.etl import run_config

    config_path = workdir / "rides.json"
    config = {
        "identifier": "agent_demo_rides",
        "metadata": {
            "source": {"name": "rides", "format": "CSV", "path": "data/rides.csv"},
            "target": {"name": "rides", "format": "ICEBERG",
                       "catalog": {"identifier": NAMESPACE, "warehouse_path": "warehouse"},
                       "write_mode": "upsert", "join_cols": ["ride_id"]},
            "quality": {"on_failure": "fail", "checks": [
                {"check": "row_count", "min": 1},
                {"check": "not_null", "columns": ["ride_id", "fare"]},
                {"check": "unique", "columns": ["ride_id"]},
                {"check": "range", "column": "fare", "min": 0},
            ]},
        },
    }
    try:
        import local_data_platform.events  # noqa: F401 - run events land in _ldp when available
        config["metadata"]["observability"] = {"sinks": ["iceberg"]}
    except ImportError:
        pass
    workdir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2))
    for day, start_id in ((dt.date(2026, 9, 1), 1), (dt.date(2026, 9, 2), 81)):
        _write_rides(workdir / "data" / "rides.csv", day, rows=80, start_id=start_id, seed=start_id)
        result = run_config(config_path)
        print(f"  loaded {result.rows_written} rides for {day} into {NAMESPACE}.rides")
    try:
        from local_data_platform import datasets
        from local_data_platform.format.iceberg import Iceberg

        table = Iceberg("rides", {"identifier": NAMESPACE, "warehouse_path": str(workdir / "warehouse")})
        version = datasets.pin(table, DATASET, row_filter="distance_km > 15", properties={"split": "train"})
        print(f"  pinned dataset {DATASET} v{version.version}: {version.row_count} rides at snapshot "
              f"{version.snapshot_id}")
    except ImportError:
        print("  (local_data_platform.datasets is not installed; get_dataset will say so)")
    build_gold_layer(workdir)
    return config_path


def build_gold_layer(workdir: Path) -> None:
    """Build a gold table of rides by day and city, define three metrics over it, and cut a release."""
    from local_data_platform.gold.build import build_gold

    catalog = {"identifier": GOLD_NAMESPACE, "warehouse_path": "warehouse"}
    (workdir / "rides_daily.json").write_text(json.dumps({
        "identifier": "rides_daily", "who": "ops@example.com",
        "metadata": {
            "source": {"name": "rides_daily", "format": "DUCKDB", "inputs": ["rides.json"],
                       "sql": "SELECT CAST(pickup_ts AS DATE) AS ride_date, city, count(*) AS rides, "
                              "CAST(sum(fare) AS DECIMAL(18, 2)) AS fares FROM rides GROUP BY 1, 2"},
            "target": {"name": "rides_daily", "format": "ICEBERG", "write_mode": "overwrite", "catalog": catalog},
            "docs": {"layer": "gold", "description": "Rides and fares for each day and city.",
                     "grain": "one row per day and city",
                     "columns": {"ride_date": "The day of the pickup.", "city": "The city of the ride.",
                                 "rides": "How many rides.", "fares": "What the riders paid."}}}}, indent=2))
    (workdir / "metrics.json").write_text(json.dumps({
        "apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "ride_metrics", "who": "ops@example.com",
        "semantic_models": [{
            "name": "rides_daily", "table": f"{GOLD_NAMESPACE}.rides_daily",
            "time_dimension": {"column": "ride_date", "grains": ["day", "week", "month", "quarter", "year"]},
            "dimensions": ["city"],
            "measures": [{"name": "fares_sum", "agg": "sum", "expr": "fares"},
                         {"name": "rides_sum", "agg": "sum", "expr": "rides"}]}],
        "metrics": [
            {"name": "fares", "type": "simple", "measure": "fares_sum", "format": "currency",
             "description": "What the riders paid."},
            {"name": "rides", "type": "simple", "measure": "rides_sum", "description": "How many rides were taken."},
            {"name": "avg_fare", "type": "ratio", "numerator": "fares_sum", "denominator": "rides_sum",
             "format": "currency", "description": "Fares divided by rides."}]}, indent=2))
    result = build_gold(workdir, release=True)
    release = result.release.release.name if result.release is not None and result.release.release else "none"
    print(f"  built gold.rides_daily with {result.tables[0].rows} rows, release {release}, "
          f"{len(result.metrics.definitions.metrics) if result.metrics else 0} metrics fit it")


# ---------------------------------------------------------------------- the server


def server_command(override: str | None) -> list[str]:
    """``ldp mcp`` when the installed ``ldp`` has it, else ``python -m local_data_platform.mcp_server``."""
    if override:
        return shlex.split(override)
    ldp = shutil.which("ldp", path=str(Path(sys.executable).parent)) or shutil.which("ldp")
    if ldp:
        probe = subprocess.run([ldp, "mcp", "--help"], capture_output=True, text=True, timeout=120)
        if probe.returncode == 0:
            return [ldp, "mcp"]
    return [sys.executable, "-m", "local_data_platform.mcp_server"]


def _show(label: str, data: Any, width: int = 110) -> None:
    text = json.dumps(data, default=str)
    print(f"  {label}: {text if len(text) <= width else text[:width - 3] + '...'}")


async def drive(command: list[str], configs: list[str], audit: Path, dataset: str) -> list[str]:
    """Connect to the server, call every tool and return the list of unexpected outcomes."""
    from mcp import Client, StdioServerParameters

    args = [*command[1:]]
    for config in configs:
        args += ["--config", config]
    args += ["--max-rows", "25", "--timeout", "20", "--audit", str(audit)]
    env = {key: os.environ[key] for key in ("PYTHONPATH", "LDP_WAREHOUSE") if key in os.environ}
    params = StdioServerParameters(command=command[0], args=args, env=env or None)
    problems: list[str] = []

    async with Client(params) as client:
        async def call(name: str, arguments: dict[str, Any], *, expect_error: bool = False) -> dict[str, Any]:
            result = await client.call_tool(name, arguments)
            data = result.structured_content or json.loads(result.content[0].text)
            if bool(result.is_error) != expect_error:
                problems.append(f"{name}({arguments}) -> is_error={result.is_error}: {data}")
            return data

        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        print(f"\nConnected to {client.server_info.name if client.server_info else 'server'}; tools: {names}")
        expected = sorted(["describe_table", "get_dataset", "list_metrics", "list_tables", "query", "query_metric",
                           "sample_rows", "table_history"])
        if names != expected:
            problems.append(f"tools are {names}, expected {expected}")

        print("\n1. list_tables")
        tables = await call("list_tables", {})
        for item in tables.get("tables", []):
            print(f"  {item['table']:<28} sql_name={item['sql_name']:<24} rows={item.get('row_count')} "
                  f"access={item['access']} last_updated={item.get('last_updated')}")
        if not tables.get("tables"):
            problems.append("list_tables returned no tables")
            return problems
        table = tables["tables"][0]
        identifier, sql_name = table["table"], table["sql_name"]

        print(f"\n2. describe_table {identifier}")
        described = await call("describe_table", {"table": identifier})
        fields = described.get("schema", {}).get("fields", [])
        print("  columns: " + ", ".join(f"{field['name']} {field['type']}" for field in fields))
        _show("partition spec", described.get("partition_spec", {}).get("fields"))
        _show("freshness", described.get("freshness"))
        _show("latest run", described.get("latest_run"))
        quality = described.get("quality") or {}
        _show("quality", {key: quality.get(key) for key in ("source", "passed", "checks_run", "checks_failed")})

        print(f"\n3. sample_rows {identifier} n=3")
        sample = await call("sample_rows", {"table": identifier, "n": 3})
        print("  " + " | ".join(column["name"] for column in sample.get("columns", [])))
        for row in sample.get("rows", []):
            print("  " + " | ".join(str(value) for value in row))

        print(f"\n4. table_history {identifier}")
        history = await call("table_history", {"table": identifier, "limit": 5})
        for entry in history.get("history", []):
            print(f"  {entry['committed_at']}  {entry['operation']:<9} +{entry['added_records'] or 0} "
                  f"-{entry['deleted_records'] or 0} total={entry['total_records']} main={entry['on_main']}")

        text = next((f["name"] for f in fields if f["type"] == "string"), None)
        number = next((f["name"] for f in fields if f["type"] in ("double", "float")), None) or next(
            (f["name"] for f in fields if f["type"] in ("long", "int") and not f["name"].endswith("id")), None)
        if text and number:
            sql = (f'SELECT "{text}", count(*) AS n, round(avg("{number}"), 2) AS avg_{number} FROM {sql_name} '
                   f"GROUP BY 1 ORDER BY n DESC")
        else:
            sql = f"SELECT count(*) AS n FROM {sql_name}"
        print(f"\n5. query: {sql}")
        answer = await call("query", {"sql": sql, "max_rows": 10})
        for row in answer.get("rows", []):
            print(f"  {row}")
        print(f"  ({answer.get('row_count')} rows, truncated={answer.get('truncated')}, "
              f"{answer.get('duration_ms')} ms)")

        print("\n6. query with a row cap of 2")
        capped = await call("query", {"sql": f"SELECT * FROM {sql_name}", "max_rows": 2})
        print(f"  {capped.get('row_count')} rows, truncated={capped.get('truncated')}")
        if capped.get("row_count") != 2 or capped.get("truncated") is not True:
            problems.append(f"row cap not applied: {capped}")

        print(f"\n7. get_dataset {dataset}")
        pinned = await client.call_tool("get_dataset", {"name": dataset})
        data = pinned.structured_content or {}
        if pinned.is_error:
            print(f"  not found: {data.get('error', {}).get('message')}")
        elif not data.get("available"):
            print(f"  not available: {data.get('reason')}")
        else:
            version = data["version"]
            print(f"  {data['name']} v{version.get('version')}: table {version.get('table_identifier')} snapshot "
                  f"{version.get('snapshot_id')} filter {version.get('row_filter')!r} rows {version.get('row_count')}")
            if data.get("sql"):
                rows = await call("query", {"sql": f"SELECT count(*) AS n FROM ({data['sql']})"})
                print(f"  re-read through the pinned snapshot: {rows.get('rows')}")

        metrics = await metric_steps(call)
        print("\n10. things an agent must not be able to do")
        for label, bad_sql in REFUSED_SQL.items():
            refused = await call("query", {"sql": bad_sql}, expect_error=True)
            error = refused.get("error", {})
            print(f"  {label:<18} -> {error.get('type')}: {str(error.get('message'))[:80]}")
        refused = await call("describe_table", {"table": "_ldp.runs"}, expect_error=True)
        print(f"  {'hidden table':<18} -> {refused.get('error', {}).get('type')}")
        if metrics:
            await metric_refusals(call, metrics, problems)
    return problems


async def metric_steps(call: Any) -> list[dict[str, Any]]:
    """Steps 8 and 9: list the metrics, then compute the ones of the first table. Returns the metrics."""
    print("\n8. list_metrics")
    listed = await call("list_metrics", {})
    metrics = listed.get("metrics", [])
    for item in metrics:
        print(f"  {item['name']:<12} {item['format']:<9} {item['description']} (table {item['table']}, by "
              f"{', '.join(item['dimensions']) or 'nothing'}, grains {', '.join(item['grains']) or 'none'})")
    if not metrics:
        print(f"  none. {listed.get('note')}")
        print("\n9. query_metric\n  skipped, because there is no metric to compute")
        return []
    first = metrics[0]
    asked = [item["name"] for item in metrics if item["table"] == first["table"]][:3]
    arguments: dict[str, Any] = {"metrics": asked}
    if first["dimensions"]:
        arguments["by"] = first["dimensions"][:1]
    if first["grains"]:
        arguments["grain"] = "month" if "month" in first["grains"] else first["grains"][0]
    print(f"\n9. query_metric {arguments}")
    answer = await call("query_metric", arguments)
    print("  " + " | ".join(column["name"] for column in answer.get("columns", [])))
    for row in answer.get("rows", []):
        print("  " + " | ".join(f"{value:.2f}" if isinstance(value, float) else str(value) for value in row))
    print(f"  ({answer.get('row_count')} rows, read {answer.get('source')})")
    print("  the SQL the server compiled, with $1 for each value it binds:")
    print("    " + str(answer.get("sql")).replace("\n", "\n    "))
    return metrics


async def metric_refusals(call: Any, metrics: list[dict[str, Any]], problems: list[str]) -> None:
    """What an agent must not be able to do with the metric tools, and what a hostile filter turns into."""
    first = metrics[0]
    refused = await call("query_metric", {"metrics": ["no_such_metric"]}, expect_error=True)
    error = refused.get("error", {})
    print(f"  {'unknown metric':<18} -> {error.get('type')}: {str(error.get('message'))[:80]}")
    if first["dimensions"]:
        dimension = first["dimensions"][0]
        hostile = await call("query_metric", {"metrics": [first["name"]], "by": [dimension],
                                              "filters": [f"{dimension}={INJECTION}"]})
        print(f"  {'SQL in a filter':<18} -> {hostile.get('row_count')} rows, bound as {hostile.get('parameters')}")
        if hostile.get("row_count") != 0 or hostile.get("parameters") != [INJECTION] or INJECTION in str(
                hostile.get("sql")):
            problems.append(f"a filter was not kept as a value: {hostile}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", action="append", default=[],
                        help="serve this dataset config (repeatable); by default a small lake is built")
    parser.add_argument("--workdir", help="folder for the built lake (default: a temporary folder)")
    parser.add_argument("--dataset", default=DATASET, help=f"dataset name for get_dataset (default {DATASET})")
    parser.add_argument("--server-cmd", help="command that starts the server (default: 'ldp mcp')")
    parser.add_argument("--audit", help="audit JSONL path (default: <workdir or config folder>/mcp_audit.jsonl)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    import anyio

    temp = None
    configs = [str(Path(path).resolve()) for path in args.config]
    if not configs:
        if args.workdir:
            workdir = Path(args.workdir).resolve()
        else:
            temp = tempfile.mkdtemp(prefix="ldp_agent_demo_")
            workdir = Path(temp)
        print(f"Building a small lake in {workdir}")
        build_lake(workdir)
        configs = [str(workdir)]
        audit = Path(args.audit) if args.audit else workdir / "mcp_audit.jsonl"
    else:
        audit = Path(args.audit) if args.audit else Path(configs[0]).parent / "mcp_audit.jsonl"
    try:
        command = server_command(args.server_cmd)
        print(f"Starting the server: {' '.join(command)} --config {' --config '.join(configs)}")
        problems = anyio.run(drive, command, configs, audit, args.dataset)
        records = [json.loads(line) for line in audit.read_text().splitlines()] if audit.is_file() else []
        print(f"\n11. audit log {audit}: {len(records)} records")
        for record in records[-3:]:
            print(f"  {record['ts']} {record['tool']:<14} {record['status']:<8} rows={record['rows']} "
                  f"sql={(record.get('sql') or '')[:50]!r}")
        if not records:
            problems.append("the audit log is empty")
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
    if problems:
        print("\nUNEXPECTED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nEvery tool answered, and every forbidden request was refused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
