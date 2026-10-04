# MCP server for agents

`local_data_platform.mcp_server` lets an AI agent (Claude, or any other
[Model Context Protocol](https://modelcontextprotocol.io) client) explore and query your Iceberg
tables without being able to change them. It is a
read-only MCP server that talks over stdio. It runs every SQL statement inside a locked-down
DuckDB, serves only the tables you allow, caps and times out every result, and writes every call
to an audit log.

It is contract C6 of the 0.2.0 design (`docs/design/v0_2_0.md`). The package has that name so it
never shadows the `mcp` SDK. Run it as `ldp mcp`; `python -m local_data_platform.mcp_server` runs
the same server with the same options, for clients that would rather start Python.

## Install

```bash
pip install "local-data-platform[mcp,duckdb]"
```

For streaming scans, the server uses DuckDB's `iceberg` extension. It never downloads anything
unless you ask it to, so install the extension once while you are online:

```bash
python -c "import duckdb; duckdb.connect().install_extension('iceberg')"
# or start the server once with:  ldp mcp --install-extensions ...
```

Without the extension, the server still works: it scans each table with pyiceberg and serves the
rows from memory. `list_tables` tells you which path each table uses (`"access": "native"` or
`"arrow"`).

## Try it

```bash
ldp demo                                  # builds ldp_demo/ with the demo.rides table
ldp mcp --config ldp_demo/rides.json      # serves it over stdio (Ctrl-C to stop)
make demo-agent                           # runs ldp demo, then a scripted agent against the server
```

`make demo-agent` runs `examples/agent_client.py`. The script starts the server as a subprocess
(`ldp mcp` when the installed `ldp` has it, else `python -m local_data_platform.mcp_server`) and
connects to it with the SDK's stdio client. It calls every tool, then tries the things an agent
must not be able to do: `COPY` to a file, reading `/etc/passwd`, sending two statements, `SET`,
`ATTACH`, and opening an `_ldp` system table. It checks that each one is refused, and prints the
tail of the audit log. Run it on its own with `python examples/agent_client.py`, which builds a
small lake in a temporary folder, or pass `--config` to point it at your own configs.

## Connect an agent

Claude Code:

```bash
claude mcp add ldp -- /abs/path/to/.venv/bin/ldp mcp \
  --config /abs/path/to/configs --allow "sales.*"
```

Claude Desktop, and other clients that take a JSON server list:

```json
{
  "mcpServers": {
    "ldp": {
      "command": "/abs/path/to/.venv/bin/ldp",
      "args": ["mcp", "--config", "/abs/path/to/configs", "--allow", "sales.*", "--max-rows", "200"]
    }
  }
}
```

Use absolute paths, because the client decides the working directory, and the `ldp` of the
virtualenv where local-data-platform is installed with the `mcp` extra.

## Options

```text
ldp mcp --config DIR_OR_FILE [--config ...] [--catalog SPEC.json ...]
        [--allow TABLES] [--max-rows N] [--timeout SECONDS] [--audit PATH] [--no-iceberg-audit]
        [--no-native] [--install-extensions] [-v]
```

| Option | Meaning |
|---|---|
| `--config` | A dataset config, or a folder of `*.json` configs. The folder is not searched recursively, and JSON files that are not configs are skipped. Every `ICEBERG` source or target becomes a table. Repeatable. |
| `--catalog` | A catalog spec file, either a `target.catalog`-style object or `{"catalog": {...}}`. Every table in the catalog's top-level namespaces is served. Repeatable. |
| `--allow` | A comma-separated allowlist of identifiers or `fnmatch` patterns, such as `sales.orders,sales.*`. A pattern without a dot also matches the bare table name. By default every table found is served, except the `_ldp` system tables, which need a pattern that starts with `_ldp`. |
| `--max-rows` | The row cap for every result. The default is 500. A tool's own `max_rows` or `n` is clamped to it. |
| `--timeout` | Interrupt a query after this many seconds. The default is 30. |
| `--audit` | The JSONL audit file. The default is `<first local warehouse>/.ldp/audit/mcp_audit.jsonl`. |
| `--no-iceberg-audit` | Audit to the JSONL file only, without the `_ldp.audit` Iceberg table. |
| `--no-native` | Always serve the Arrow copy, never `iceberg_scan`. |
| `--install-extensions` | Run `INSTALL iceberg` before loading it. This needs network access the first time. |
| `-v` | Debug logging on stderr. stdout carries the MCP protocol only. |

Discovery only reads. A catalog whose SQLite file doesn't exist yet (`local`, or `sql` on
SQLite), or a table the pipeline hasn't created yet, is reported under `unavailable` by
`list_tables`, and nothing is created for it. The only things the server writes are its audit
records. If there are no tables to serve at all, the command exits with an error.

## Tools

Every tool is annotated `readOnlyHint: true`, `destructiveHint: false`. Results come back as
`structuredContent`, with the same JSON repeated as a text block. A refused or failed call comes
back with `isError: true` and `{"error": {"type", "message"}}`.

| Tool | Arguments | Returns |
|---|---|---|
| `list_tables` | none | Each table's `table` identifier, `sql_name` (for example `"demo"."rides"`), `aliases` (its bare name when that is unique), `access` (`native` or `arrow`), `row_count`, `last_updated` and `source`. Also returns the `unavailable` tables and the `limits`. |
| `describe_table` | `table` | The schema, partition spec, sort order, `row_count` (from snapshot metadata), the 10 most recent snapshots, table properties (keys that look secret are redacted), `location` and `metadata_location`. Also `freshness` (the last commit time and its age in seconds, plus the last run's time and status), `latest_run` and `quality` from `_ldp`. |
| `query` | `sql`, optional `max_rows` | `columns` (name and Arrow type), `rows` (lists in column order), `row_count`, `truncated`, `max_rows` and `duration_ms`. The default is 100 rows. |
| `sample_rows` | `table`, optional `n` (default 10) | The first `n` rows in scan order, in the same shape as `query`. |
| `table_history` | `table`, optional `limit` (default 50) | Snapshots newest first, each with `committed_at`, `operation`, the added, deleted and total records, `is_current`, `on_main` (on the current snapshot's lineage) and the `ldp.*` snapshot properties such as `ldp.run-id` and `ldp.idempotency-key`. Also the branch and tag `refs`. |
| `get_dataset` | `name`, or `name@vN` | The latest (or Nth) pinned `DatasetVersion`: its table, snapshot, row filter, selected fields, row count and schema fingerprint. When the table is served natively, it also returns `sql` that re-reads exactly the pinned rows (see below). It returns `{"available": false, "reason"}` when the install has no `local_data_platform.datasets`. |

### Latest run and quality in `describe_table`

When the pipeline writes run events to Iceberg (`metadata.observability: {"sinks": ["iceberg"]}`,
see [the observability guide](observability.md)), `describe_table` reads the table's rows from
`_ldp.runs` and `_ldp.quality_results` in the same catalog. It matches rows by `table_uuid` or
`table_identifier`.

- `latest_run` holds the newest run's `run_id`, `attempt`, `pipeline`, `status` (`published`,
  `skipped_duplicate`, `blocked_quality`, `failed` or `running`), `started_at`, `finished_at`,
  the published `snapshot_id`, the `idempotency_key` and the list of event types.
- `quality` comes from the newest `quality.evaluated` event, with a per-check list from
  `_ldp.quality_results`: `passed`, `checks_run`, `checks_failed`, `on_failure` and `checks`.
  Without `_ldp` rows it falls back to the current snapshot's `ldp.quality` property. If there is
  neither, it is `null`.

The `_ldp` tables are read with pyiceberg, never through the DuckDB sandbox, so agents can't query
them unless you allowlist them.

### Time travel and pinned datasets

For a table whose `access` is `native`, SQL can read any snapshot that is still in the metadata:

```sql
SELECT count(*) FROM iceberg_scan('<metadata_location from describe_table>', snapshot_from_id => <snapshot_id>)
```

`get_dataset` returns this SQL ready-made for a pinned version, with the pinned columns and
`row_filter` filled in. The filter is written in pyiceberg syntax, which is almost always valid
DuckDB SQL.

## Guardrails

The protection comes in layers, and each layer was checked against DuckDB 1.5.6 and the mcp SDK
2.2.0.

**1. The statement guard** (`mcp_server/guard.py`). Before anything runs, the SQL must pass two
checks:

- DuckDB's tokenizer runs first. It drops comments and keeps string literals whole. The first
  token, after any `(`, must be `SELECT` or `WITH`. This check rejects `PRAGMA`, `DESCRIBE`,
  `SHOW`, `SUMMARIZE`, `FROM`-first queries and `VALUES`, all of which DuckDB rewrites into
  `SELECT` statements. It also stops `IMPORT DATABASE` before it reaches the parser, and that
  matters: DuckDB's parser reads `<dir>/schema.sql` while it parses the statement.
- `extract_statements`, run on the sandboxed connection, must then return exactly one statement
  of type `SELECT`. This check rejects multiple statements, however they are hidden in comments
  or strings, and CTE-wrapped writes such as `WITH x AS (...) INSERT ...`, which parse as
  `INSERT`.

**2. The DuckDB sandbox** (`mcp_server/sandbox.py`). The connection is set up in this order:

1. It connects in memory. `autoinstall_known_extensions`, `autoload_known_extensions`,
   `allow_community_extensions` and `allow_persistent_secrets` are all off. `temp_directory` is
   empty, which means no spill files, and also stops DuckDB from adding a temp folder to the
   allowed paths. Python replacement scans are off, so SQL can't read the server's own
   variables.
2. It loads `iceberg`, and registers each table as a view in a schema named after its namespace.
   The view uses `iceberg_scan('<metadata file>')` when every file the snapshot reads is local and
   under the table's location, and an Arrow copy scanned by pyiceberg otherwise. Tables on `s3://`
   or `gs://` are always served as Arrow, so DuckDB never needs cloud credentials.
3. `SET allowed_directories` is set to the served tables' folders only, not the whole warehouse.
   That keeps the SQLite catalog file, the other tables and `.ldp/` out of reach. Then
   `SET enable_external_access = false`.
4. `SET lock_configuration = true`. The server then reads the settings back from
   `duckdb_settings()`, and refuses to start if any of them didn't take.

**3. The allowlist.** Tables outside it are never registered and their folders are never allowed.
A tool that names one gets the same `TableNotAllowed` error as a table that doesn't exist, so an
agent can't probe for them.

**4. Limits.** Rows are streamed from DuckDB and the reader stops at `max_rows + 1`, so
`truncated` is exact and a huge result never reaches Python. A timer calls
`connection.interrupt()` when the timeout passes. The tool calls themselves run one at a time.

**5. The audit.** Every call is written down, as described in the next section.

What the tests check (`tests/test_mcp_server.py`):

| Attempt | Stopped by |
|---|---|
| `COPY ... TO`, `EXPORT DATABASE`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `SET`, `RESET`, `PRAGMA`, `CALL`, `CHECKPOINT`, `CREATE` (including `SECRET` and `MACRO`), `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `IMPORT`, transactions, `PREPARE`/`EXECUTE` | the guard (`QueryRejected`) |
| Two statements, including behind `--` or `/* */` comments or inside string tricks; `/* SELECT */ COPY ...`; CTE-wrapped `INSERT` or `COPY` | the guard |
| `read_csv('/etc/passwd')`, `read_text`, `read_blob('file:///...')`, `read_parquet` outside the warehouse, `glob('/*')`, `glob('~/*')` | DuckDB (`AccessDenied`) |
| Reading the catalog `.db`, another table's data files or its `iceberg_scan`, `..` traversal out of an allowed folder, a sibling folder with the same prefix | DuckDB (`AccessDenied`) |
| `SELECT * FROM query('COPY ...')` and `json_execute_serialized_sql(...)` | DuckDB: only a `SELECT` runs inside them |
| `SET`, `RESET`, `INSTALL`, `LOAD` and `ATTACH` executed directly on the connection, bypassing the guard | DuckDB (`lock_configuration`, `enable_external_access`) |
| Referencing a Python variable by name | replacement scans are off |

### Known limits

- `allowed_directories` grants write access as well as read access: once external access is off,
  `COPY ... TO '<table folder>/x.csv'` succeeds in raw DuckDB. The statement guard is what keeps
  writes out, and the tests check both layers.
- Inside an allowed table's folder, SQL can read the raw files. That includes old snapshots and
  staged `ldp_r...` branches that aren't on `main` yet. If a table must not show unpublished
  rows, don't allowlist it.
- The timeout interrupts DuckDB. It doesn't stop a pyiceberg scan that is building the Arrow copy
  of a table. For memory, pass `memory_limit=` to `LakeTools`/`DuckDBSandbox`. Arrow-served tables
  are held in memory.
- Views are refreshed before every `query` and `sample_rows` call, so new commits show up at
  once. The refresh loads each table's metadata from the catalog, one call per table.

## Audit log

Each call appends one JSON line. Result data is never logged, only its size:

```json
{"tool": "query", "status": "ok", "duration_ms": 3.1, "sql": "SELECT city, count(*) FROM demo.rides GROUP BY 1",
 "table": null, "arguments": {"max_rows": 20}, "rows": 3, "truncated": false, "error": null,
 "event_id": "5f0c...", "ts": "2026-09-30T18:19:53.497+00:00", "schema_version": 1}
```

`status` is one of these values:

| Status | Meaning |
|---|---|
| `ok` | The call succeeded |
| `rejected` | The guard, argument validation or the allowlist refused the call |
| `denied` | DuckDB's sandbox refused the call |
| `timeout` | The query was interrupted |
| `error` | Anything else went wrong |

The file is flushed after every record.

Records also go to the `_ldp.audit` Iceberg table (SaaS design §6.4) in the first served catalog,
through `IcebergSink.emit_audit`. It holds the same fields, with `table` as `table_identifier` and
`arguments` as JSON, is partitioned by `day(ts)`, and like `_ldp.runs` it is an ordinary table you
can query. The records are buffered and appended in one commit when the server stops (or every 500
records), so a server that is killed leaves them only in the JSONL file. If the catalog can't take
the write, the server logs a warning and the JSONL file stays the audit trail. `--no-iceberg-audit`
(or `LakeTools.from_sources(..., iceberg_audit=False)`) turns the table off.

## From Python

```python
from local_data_platform.mcp_server import LakeTools, build_server

with LakeTools.from_sources(["configs/"], allow="demo.*", max_rows=100, timeout_s=10) as tools:
    print(tools.call("query", {"sql": "SELECT count(*) FROM demo.rides"}).data)
    server = build_server(tools)   # a low-level mcp Server; serve_stdio(tools) runs it on stdio
```

`LakeTools.call()` validates the arguments, runs the tool, audits the call and never raises. The
MCP layer only translates its `ToolOutcome` into MCP results. The server is built on the SDK's
low-level `mcp.server.lowlevel.Server`, not on `MCPServer` (the renamed FastMCP), because
`MCPServer` calls `logging.basicConfig` when it is constructed.
