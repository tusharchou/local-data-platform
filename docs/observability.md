# Observability: run events, `_ldp` tables and OpenLineage

Every pipeline run records what it did as a short stream of **run events**. You choose where the
events go: nowhere (the default), a JSONL file, two Iceberg tables in your own catalog, an
OpenLineage backend such as Marquez, or several of these at once. The envelope is the one the
[SaaS architecture](design/saas_architecture.md) proposal (§6.4–§6.5) would also use; only the
library side exists.

## The events of a run

```text
run.started ─► run.extracted ─► quality.evaluated ─┬─► run.published ───────┐
                                                   ├─► run.skipped_duplicate ├─► run.finished
                                                   ├─► run.blocked_quality ──┤
            (any step can fail) ──────────────────► run.failed ─────────────┘
```

- A run that fails before its checks goes straight from its last step to `run.failed`.
- `run.skipped_duplicate` happens only in staged mode, when the run's idempotency key is already
  on `main` (see [Exactly-once runs](#exactly-once-runs-and-idempotency-keys)).
- `run.blocked_quality` means a check failed with `on_failure: "fail"`. The pipeline still raises
  `DataQualityError`, as it does without run events, and writes nothing.
- `run.finished` always comes last and carries the final `status`: `published`,
  `skipped_duplicate`, `blocked_quality` or `failed`.

Each event is a `RunEvent`:

| Field | Meaning |
|---|---|
| `event_id` | A UUIDv7, unique per event |
| `type` | One of the types above (`table.maintained` and `run.staged` are reserved) |
| `schema_version` | The envelope version, `1` |
| `ts` | When it happened, in UTC |
| `run_id` | The run's UUIDv7. `PipelineResult.run_id` is the same value |
| `attempt` | The attempt number, from 1 |
| `seq` | 0, 1, 2… within `(run_id, attempt)` |
| `table_uuid` | The target Iceberg table's UUID, once known |
| `payload` | Facts about the step (below) |

Consumers apply each event at most once per `(run_id, attempt, seq)` and ignore fields they don't
know, so new fields can be added without breaking readers.

Every payload carries `pipeline` (the config's `identifier`) and `table` (the target). The rest
depends on the type:

| Type | Payload |
|---|---|
| `run.started` | `mode`, `publish` (`direct` or `staged`), `idempotency_key`, `spec_hash`, `logical_window`, `source` and `target` (dataset names), `ldp_version` |
| `run.extracted` | `rows_read`, `schema` (names and types) |
| `quality.evaluated` | `rows`, `schema`, `null_counts` per column, `on_failure`, `passed`, `checks_run`, `checks_failed`, and `results`: one entry per check with `name`, `column`, `passed`, `failing_rows`, `details` and numeric `metrics` |
| `run.published`, `run.skipped_duplicate` | `rows_written`, `snapshot_id`, `idempotency_key`, `write` (the `WriteResult`), `snapshot_summary` |
| `run.blocked_quality` | `checks_failed`, `failures` (check names), `on_failure` |
| `run.failed` | `stage` (`extract`, `transform`, `validate`, `write`), `error_type`, `message` |
| `run.finished` | `status`, `duration_s`, `rows_read`, `rows_written`, `published_snapshot_id`, `idempotency_key`, `quality_passed` |

### What never goes into an event

Payloads follow the SaaS boundary allowlist (§10.2): specs, schemas, counts, snapshot ids and
summaries, and check verdicts. They never carry rows or failing values:

- the `sample` of duplicated keys (`unique`) and the `invalid_values` (`accepted_values`) are
  dropped from check metrics, and so is the `e.g. …` part of their details;
- observed minimum and maximum are kept only for numeric columns;
- error messages are passed through `redact_text`, which masks passwords in URLs, `token=…`-style
  pairs, bearer tokens, AWS access key ids and PEM blocks.

A sink that fails (a full disk, a lineage server that is down) never fails the data load. The
pipeline logs a warning and carries on.

## Choosing sinks

### In a config

Add `metadata.observability` to a dataset config:

```json
{
  "identifier": "rides",
  "metadata": {
    "source": {"name": "rides", "format": "CSV", "path": "data/rides.csv"},
    "target": {"name": "rides", "format": "ICEBERG",
               "catalog": {"identifier": "nyc", "warehouse_path": "warehouse"}},
    "observability": {
      "sinks": ["iceberg", "jsonl", {"type": "openlineage", "url": "http://localhost:5000"}],
      "jsonl": {"path": ".ldp/events.jsonl"},
      "openlineage": {"namespace": "acme", "api_key_env": "MARQUEZ_TOKEN"}
    }
  }
}
```

`sinks` lists sink types, or objects with a `type` and options. Options can also sit in a block
named after the type; options in the list item win.

| Type | Options | Default |
|---|---|---|
| `iceberg` | `catalog`, `namespace`, `batch_size` | The target's Iceberg catalog (else the source's), namespace `_ldp`, 500 |
| `jsonl` | `path` | `.ldp/events.jsonl`, next to the config |
| `openlineage` | `path` or `url`, `namespace`, `api_key_env`, `timeout` | `.ldp/openlineage.jsonl`, job namespace `ldp`, 10 s |
| `null` (or `none`) | — | Drops everything |

With no `observability` block a run emits nothing. Secrets are never written into a config:
`api_key_env` names an environment variable that holds the token.

### In Python

Pass a sink to `Pipeline.run`, to the `Pipeline(..., sink=)` constructor, or to `run_config`:

```python
import pyarrow as pa

from local_data_platform.events import JsonlSink, MemorySink, MultiSink, read_jsonl
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline import Pipeline


class Rides:
    name = "rides"

    def get(self):
        return pa.table({"ride_id": [1, 2, 3], "fare": [12.5, 8.0, 30.25]})


memory = MemorySink()
pipeline = Pipeline(source=Rides(), target=Iceberg("rides", {"identifier": "nyc", "warehouse_path": "warehouse"}),
                    checks=[{"check": "not_null", "columns": ["ride_id"]}])
result = pipeline.run(sink=MultiSink([memory, JsonlSink(".ldp/events.jsonl")]))

print(result.run_id == memory.events[0].run_id)
print(memory.types)
print([event.type for event in read_jsonl(".ldp/events.jsonl")][-1])
```

`MemorySink` keeps events in a list, which is handy in tests and notebooks. Any object with
`emit(event)` and `flush()` methods is a sink.

## The `_ldp` namespace

The `iceberg` sink appends to two tables in the pipeline's own catalog, so you can query your run
history with any engine and keep it if you move:

| Table | One row per | Columns |
|---|---|---|
| `_ldp.runs` | event | `event_id`, `type`, `schema_version`, `ts`, `run_id`, `attempt`, `seq`, `table_uuid`, `pipeline`, `table_identifier`, `status` (on `run.finished`), `idempotency_key`, `snapshot_id`, `payload` (JSON) |
| `_ldp.quality_results` | check of a `quality.evaluated` event | `event_id`, `ts`, `run_id`, `attempt`, `pipeline`, `table_identifier`, `check_index`, `check_name`, `column_name`, `passed`, `failing_rows`, `on_failure`, `details`, `metrics` (JSON) |

Both are partitioned by `day(ts)` and created on first use. Events are buffered and appended in
one commit per table when the run ends, or earlier once `batch_size` events are waiting.

```python
from local_data_platform.catalog.provider import create_catalog
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.events import IcebergSink, read_iceberg_events, summarize_runs

catalog_spec = {"identifier": "nyc", "warehouse_path": "warehouse"}
pipeline.run(sink=IcebergSink(catalog_spec))

catalog = create_catalog(catalog_spec)
latest = summarize_runs(read_iceberg_events(catalog, pipeline="rides"))[0]
print(latest["status"], latest["rows_written"], latest["checks"])

with DuckDBEngine() as duck:
    duck.register_iceberg(catalog.load_table("_ldp.quality_results"), "quality")
    print(duck.query("SELECT check_name, passed FROM quality").to_pylist())
```

## OpenLineage

`OpenLineageSink` turns each run into two OpenLineage 1.x `RunEvent`s (spec `2-0-2`): `START`
when the run starts, and `COMPLETE` (published or skipped duplicate) or `FAIL` (blocked or
failed) when it finishes.

| OpenLineage | From |
|---|---|
| `run.runId` | The LDP `run_id` (a UUIDv7) |
| `job.namespace`, `job.name` | The sink's `namespace`, the pipeline name; a `jobType` facet says `BATCH`/`LDP`/`PIPELINE` |
| `inputs[0]` | The source: `file` + absolute path, `s3://bucket` + key, or the Iceberg warehouse + table; with a `schema` facet |
| `outputs[0]` | The target, with `schema`, `dataQualityMetrics` (row count and null counts of the validated batch) and `dataQualityAssertions` (one per check); plus an `outputStatistics` output facet (rows, bytes and files written) when published |
| `run.facets` | `nominalTime` for a logical window, `errorMessage` on `FAIL`, and `ldp_run` (attempt, status, idempotency key, spec hash, snapshot id) |

Give it a file path to write one JSON event per line, or an `http(s)://` URL to POST each event.
A URL without a path posts to `/api/v1/lineage`, the Marquez endpoint:

```bash
docker run -p 5000:5000 marquezproject/marquez   # then use "url": "http://localhost:5000"
```

## Reading the history and checking specs

`ldp runs`, `ldp schema` and `ldp plan` read the history and check specs from the command line:

```bash
ldp runs rides.json                    # one line per run, newest first (--limit N, default 20)
ldp runs rides.json --run RUN_ID       # every event of one run (--events shows events for all runs)
ldp runs .ldp/events.jsonl             # a JSONL events file works too
ldp schema                             # the ldp/v1 JSON Schema
ldp plan rides.json --window 2026-09-01/2026-09-02   # validate, then print the plan (--json for JSON)
```

`ldp runs` reads `_ldp.runs` in the config's catalog when the config has an `iceberg` sink or that
table already holds the pipeline's runs, else its JSONL events file. It creates nothing while
looking. `ldp plan` exits with 1 if the config (or any config in a folder) is invalid. The
same functions work from Python:

| Command | In Python |
|---|---|
| `ldp runs CONFIG` | `summarize_runs(read_jsonl(".ldp/events.jsonl"))`, or `summarize_runs(read_iceberg_events(catalog))` as above: one dict per run, newest first, with `run_id`, `attempt`, `pipeline`, `table`, `status`, `started_at`, `duration_s`, `rows_read`, `rows_written`, `checks`, `snapshot_id` and `idempotency_key` |
| `ldp runs CONFIG --run RUN_ID` | `read_iceberg_events(catalog, run_id=...)`, or `read_jsonl(path)` filtered on `event.run_id`: every event of one run |
| `ldp schema` | `spec.json_schema()`, the `ldp/v1` JSON Schema |
| `ldp plan CONFIG [--window START/END]` | `spec.validate_spec(data)`, a list of `ConfigError` that is empty when the config is valid, and `spec.plan(config, window=...)`, a dict with `valid`, `errors`, `spec_hash`, `route`, `pipeline`, `target`, `sinks` and the window's `idempotency_key` |

`read_iceberg_events` returns no events, and creates nothing, when the `_ldp` tables don't exist
yet. `spec.plan` reads nothing but the config.

## Exactly-once runs and idempotency keys

`spec.py` gives every spec two stable fingerprints:

- `spec_hash(config)` is the sha256 of the canonical JSON of the pipeline-relevant fields
  (`apiVersion`, `identifier`, `when`, `how` and `metadata` except `observability`). Key order,
  whitespace, format case, the list or object form of `quality`, and where the project is checked
  out don't change it. It is recorded as the `ldp.spec-hash` snapshot property.
- `idempotency_key(config, window)` hashes the pipeline, the target table and the logical window,
  and deliberately **not** the spec hash: redeploying a spec and re-running a window must find the
  earlier commit rather than append again (SaaS §7.3).

Pass a `window` to `run_config` to write through the staged publish protocol. A second run over the
same window finds its key on `main` and returns `status == "skipped_duplicate"` without writing:

```python
from local_data_platform.etl import run_config

first = run_config("rides.json", window="2026-09-01/2026-09-02")
again = run_config("rides.json", window="2026-09-01/2026-09-02")
assert again.status == "skipped_duplicate" and again.published_snapshot_id == first.published_snapshot_id
```

A local run has no run ledger, so it looks for its key across the whole idempotency horizon
(7 days) of `main`'s history. `run_config(..., idempotency_key="…")` uses a key from an orchestrator
instead of deriving one.
