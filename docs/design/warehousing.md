# Design: warehousing with dbt over Iceberg tables

Status: the contract for the 0.1.2 `ldp dbt` command, and it is built: source registration, the
publish step and the command. It continues #45 (dbt transformation layer) and #57 (dbt dependencies),
and it is milestone 0.1.2 Warehousing in the [roadmap](../roadmap.md).

## Problem

Tables that `ldp run` writes are Iceberg tables in a catalog. People who work with data want to
model them the way a warehouse team does: staging models, gold tables, tests, documentation and
incremental loads. dbt does all of that, but it doesn't know Iceberg, and LDP's catalogs are not
something dbt can connect to. What the modelling produces should be Iceberg again, so that it is
versioned and can be time-travelled, and so that `ldp query`, `ldp runs`, `ldp maintain` and
`ldp mcp` see it like any other table.

## Decision

dbt runs on DuckDB, in a build file inside the project (`target/ldp_build.duckdb`). `ldp dbt` puts
LDP on both sides of it, in three steps.

1. **Register sources.** Every Iceberg table that LDP knows about becomes a relation in the build
   file, and a generated `models/_ldp_sources.yml` declares them to dbt, so a model can say
   `{{ source('raw', 'orders') }}`.
2. **Run dbt.** `dbt` runs as a subprocess with `--project-dir` and `--profiles-dir` set to the
   project. LDP holds no connection to the build file while dbt runs.
3. **Publish.** After a build that succeeded, each model marked `ldp_publish: true` is written to
   Iceberg through the existing [`DUCKDB` to `ICEBERG` route](factory_registry.md), so a published
   model gets the same quality gate, run events and snapshots as ingested data.

Alternatives that were considered:

- **A dbt materialization that writes Iceberg itself.** It would tie LDP to dbt-duckdb internals
  and skip LDP's quality checks and run events.
- **SQLMesh.** It is a good tool, but nobody asked for it, and dbt is what a learner needs to know.
- **dbt's Python API, in process.** The subprocess keeps dbt's dependencies and version apart from
  LDP's, and it is what `dbt` on a laptop already is.
- **SQL models run by LDP itself.** That would rebuild dbt, without its tests, docs or community.
- **dbt v2.** Its engine downloads its own DuckDB driver on first run and does not yet support local
  docs, which breaks offline use. The subprocess design keeps it possible later.

## The project

`ldp dbt init PROJECT_DIR --sources CONFIG_DIR --warehouse WAREHOUSE_DIR` writes a project that
works as soon as the sources exist:

| File | What it is |
|---|---|
| `dbt_project.yml` | The dbt project. It turns off dbt's usage statistics under `flags`. |
| `profiles.yml` | One DuckDB output, `path: target/ldp_build.duckdb`, 4 threads. It also sets `autoinstall_known_extensions: false`, so dbt never downloads a DuckDB extension. |
| `ldp_dbt.json` | What LDP needs: the sources, where to publish, and where events go. |
| `models/staging/stg_example.sql` | A staging model on the first source table, or on a constant when there is none yet. |
| `models/gold/gold_example.sql` | A gold model on the staging one, marked `ldp_publish: true`. |
| `models/_ldp_sources.yml` | Generated on every build. Do not edit it. |

`init` writes only into an empty or missing folder and never overwrites anything. To change what an
existing project uses, edit `ldp_dbt.json`.

dbt resolves a relative `path` against the folder it runs in, so LDP always runs dbt with the
project as its working directory. The build file lives in `target/`, so `dbt clean` removes it, and
with it the state of incremental models.

### `ldp_dbt.json`

```json
{
  "database": "target/ldp_build.duckdb",
  "sources": {"configs": ["../config"], "catalogs": []},
  "publish": {
    "catalog": {"identifier": "gold", "warehouse_path": "../warehouse"},
    "config_dir": "target/ldp"
  },
  "observability": {"sinks": ["iceberg", "jsonl"]}
}
```

- `database` is the build file. It defaults to `target/ldp_build.duckdb` and must match the `path`
  in `profiles.yml`. LDP reads the profile to check, and fails with both paths when they differ.
- `sources` lists dataset configs (a file, or a folder of `*.json` files) and catalog spec files to
  take tables from. A plain list means configs. Do not list the catalog you publish to as a source
  catalog.
- `publish.catalog` is an LDP catalog block, as in a dataset config. `publish.config_dir` is where
  the generated configs go, and defaults to `target/ldp`. It can be the folder that `sources` reads,
  so the published models sit beside the ingest configs for `ldp mcp` to serve. A later build does
  not take them as sources, because their source is the build file.
- `observability` is the same block as in a dataset config. It is copied into every generated
  config, so `ldp runs` works on published models.
- Relative paths resolve against the project folder. Unknown keys are errors, reported in the style
  of `ldp plan`.

## Sources

`register_sources(database, configs, catalogs, mode="auto")` uses the same discovery as the MCP
server, so a table is a source if `ldp mcp` would serve it. It opens the build file, creates one
schema per namespace and one relation per table named `namespace.table`, writes the YAML, and closes
the file before it returns.

- **View mode.** Each relation is a persistent view over `iceberg_scan` of the table's metadata
  file, pinned to the snapshot that was current when registration started. A build therefore reads
  one consistent version of every source, even if an ingest commits while it runs.
- **Copy mode.** Each relation is a DuckDB table holding that snapshot. It works without DuckDB's
  `iceberg` extension, and it is slower to register and bigger on disk.
- **Auto, the default.** View mode when the extension loads without a download, copy mode
  otherwise, with a warning that says how to install the extension. A single table that DuckDB
  cannot scan falls back to a copy.
- **Nothing is downloaded.** The `iceberg` extension is never installed. A view only exists when
  the extension was already installed, so dbt finds it on its own. The generated profile also turns
  autoinstall off, in case dbt runs with another DuckDB.
- **Leftovers are removed.** LDP keeps a small table, `_ldp.sources`, in the build file. A relation
  it created earlier, for a table that no longer exists, is dropped on the next registration, and so
  is its schema when that is empty. Relations that dbt created are never touched.
- **Published tables are not sources.** A table that a config publishes from the build file itself
  is an output of the project, so it is skipped. Otherwise a published model would hide its own dbt
  relation, or read itself. Tables in the `_ldp` namespace are skipped too.
- **The YAML** has one dbt source per namespace, one entry per table, and each column's name and
  DuckDB type. Its first line says it is generated and rewritten on every build.

## Running dbt

- The `dbt` that runs is the one next to the Python interpreter, else the one on `PATH`, or the one
  given with `--dbt-bin`. Without any, `ldp dbt` prints `pip install "local-data-platform[dbt]"`
  and exits with 1, before it has registered anything.
- Every command is `dbt --no-version-check ARGS --project-dir P --profiles-dir P`, where `ARGS`
  start with the dbt command. The version check would contact pypi.org on every run. The project
  flags come last so that a command with a subcommand, such as `docs generate`, accepts them. When
  the output is not a terminal, `--no-use-colors` is added, so logs and pipes have no escape codes.
- Everything after `PROJECT_DIR` on the `ldp` command line is passed to dbt unchanged, and a `--`
  right after `PROJECT_DIR` is dropped, so `ldp dbt build P --select a` and
  `ldp dbt build P -- --select a` are the same. Options of `ldp dbt` itself, such as `--dbt-bin`,
  `--sources-mode` and `--no-sources`, go before `PROJECT_DIR`.
- dbt's output goes straight to the terminal. `ldp dbt build`, `test`, `docs` and `exec` register
  sources first. `--no-sources` skips that, for a command such as `clean` that does not read them,
  and `--sources-mode view|copy|auto` chooses how. Only `build` publishes.
- `ldp dbt exec PROJECT_DIR COMMAND ...` runs any other dbt command. `ldp dbt docs` runs
  `dbt docs generate`, and with `--serve` (and `--port`, 8080 by default) serves the result on
  127.0.0.1 only, until Ctrl-C.
- `ldp dbt build` reads `manifest.json` and `run_results.json` from dbt's target folder: the
  `--target-path` it was given, else `DBT_TARGET_PATH`, else `target-path` in `dbt_project.yml`,
  else `target`. It refuses files that dbt did not write during this invocation, for example after
  `--no-write-json`, because they would describe an earlier run.
- The exit status is 0 on success and 1 on any error, including a dbt run that failed. A failing
  dbt run is reported as `ldp: error: dbt build failed with exit status N, so nothing was published`,
  followed by one line for each failed node.

## The publish contract

**Which models.** A model is published when all three are true.

1. Its resolved config has meta `ldp_publish: true`. It is set per model, or for a folder with
   `+meta` in `dbt_project.yml`.
2. It ran in this invocation and has status `success` in `target/run_results.json`. A model that
   `--select` left out is not published, even if an earlier build published it.
3. dbt exited with 0.

A build with `--empty` is never published. It builds every model with no rows, and an `overwrite`
of those would erase the tables.

**All or nothing.** If dbt exits with anything else, nothing is published, the failed nodes from
`run_results.json` are listed, and `ldp` exits with 1. That includes a failing data test, because
`dbt build` fails the run for it. Publishing the models that succeeded before another node failed is
not planned: it would put tables in the lakehouse that tests have not cleared.

**How.** The default write mode follows the materialization.

| Materialization | Default | Why |
|---|---|---|
| `table` or `view` | `overwrite` | The relation holds the whole table. |
| `incremental` with a `unique_key` | `upsert` on that key | Only changed and new rows are written. |
| `incremental` without a `unique_key` | an error | Appending the whole relation on every build would duplicate rows. Set `ldp_write_mode`. |
| `ephemeral`, or anything that writes files | an error | There is no relation to read. |

These meta keys override or add to the defaults, with the same meaning as in an LDP config.

| Meta key | Meaning |
|---|---|
| `ldp_write_mode` | `append`, `overwrite` or `upsert`. |
| `ldp_join_cols` | The key columns for `upsert`, when the model has no `unique_key`. |
| `ldp_namespace` | The namespace of the Iceberg table. The default is the one of `publish.catalog`. For a `local` catalog the namespace is the catalog's `identifier`, so each namespace gets its own catalog file. |
| `ldp_partition_by` | A list of `{"column", "transform"}` items, as `partition_by` in a config. |
| `ldp_quality` | Quality checks, as `metadata.quality` in a config. A failing check blocks that model's write. |

The Iceberg table is named after the model's alias. Two models that would write the same table are
an error. An `upsert` never deletes: a row that a model stops producing stays in Iceberg until the
table is overwritten.

**What is generated.** For each published model LDP writes `<model>.json` into `publish.config_dir`
and runs it with `etl.run_config`. The file is an ordinary dataset config whose source is
`{"format": "DUCKDB", "path": <build file>, "relation": "<schema>.<alias>"}`, so it works with
`ldp snapshots`, `ldp query`, `ldp runs`, `ldp maintain` and `ldp mcp --config`, and `ldp run` on it
publishes the model again. Paths in it are relative to `config_dir`.

**Order and failure.** Every generated config is validated before the first write, so a bad meta key
fails the command with the model's name and nothing published. Models then publish in dependency
order. Each model's write is one Iceberg commit, but a command that publishes several models is not
atomic. If a write fails, for example on a blocked quality check, the command stops, says which
models were published and which were not, prints the failed model's config so it can be run by hand
with `ldp run`, and exits with 1.

**Schema changes.** The columns of a published model can change between builds, and Iceberg decides
what is allowed. A new column is added to the table by an `overwrite`. An `upsert` fails when the
model gains a column and when it loses one, because it replaces whole rows, so the model has to be
published once with `ldp run CONFIG --mode overwrite`. The build file holds the whole relation, so
that gives the table the new columns, and the next build upserts again. A column that a model
stopped producing stays in the table, and an `overwrite` writes nulls in it. A type change that
Iceberg does not allow, such as `bigint` to `double`, fails with the error Iceberg gives. With dbt's
default `on_schema_change: ignore`, an incremental model never gains a column, so none of this
applies until that setting changes.

**Re-running.** A second build with no new source data adds no snapshot to models published with
`upsert`, because rows that did not change are not rewritten. A model published with `overwrite`
adds new snapshots every time, a delete and an append.

**Summary.** One line per published model, printed as soon as it is written: the table, the write
mode, the rows before and after, and the snapshot id, for example
`ldp: published silver.fct_orders: upsert, 3 -> 5 rows, snapshot 7181667972156646358`. When no
model ran with the mark, `ldp` says so and exits with 0.

## Not planned in 0.1.2

- Publishing partially after a failed node, as said above.
- dbt snapshots (slowly changing dimensions), Python models and the `external` materialization.
- Scheduling dbt runs, which is [0.1.3](../roadmap.md). The `ldp dbt build -- ARGS` pass-through
  exists for it.
- Lineage from the dbt manifest into OpenLineage, and descriptions and owners from dbt into the
  published tables. Those are 0.1.4, the gold layer.

## Tests

Everything that does not run dbt is tested without it: source registration and the generated YAML
(`test_dbt_sources.py`), `ldp_dbt.json`, `init` and finding dbt (`test_dbt_project.py`), the publish
plan built from `manifest.json` and `run_results.json` dicts and the writes it leads to
(`test_dbt_publish.py`), and the command itself (`test_dbt_cli.py`), which runs against a stand-in
`dbt` script that records its arguments and writes the artifacts and relations a run would. Tests
that run the real dbt carry the `dbt` marker and run in the `dbt` CI job, offline, with a trap that
fails them if anything opens a connection (`test_dbt_end_to_end.py`). They run the three-model
project in `tests/fixtures/dbt_project` end to end, and cover a build that publishes, a failing data
test or model that publishes nothing, an incremental second build that upserts only the new rows, a
`--select` build that publishes only the selected models, a project with no published models, a view
over the sources, and `init`, `test`, `docs` and `exec`.
