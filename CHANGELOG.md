# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [0.2.0] - Unreleased

"Multi-writer, any catalog, object storage, agent-ready": Phase 1 of
`docs/design/saas_architecture.md`. The build contract is `docs/design/v0_2_0.md`. This is the
first release on PyPI since 0.1.0, and it also ships the 0.1.1 hardening work listed in the next
section, which was never released on its own. The features below work from Python and from the new
`ldp` subcommands; see [Not done yet](#not-done-yet) for what is still missing.

The project is now MIT licensed throughout. The `LICENSE` file was already MIT, but it named the
wrong copyright holder, and the package metadata said Apache-2.0.

### Added

- Pluggable catalogs: `catalog.provider.create_catalog(spec)` with the `local` (the default, as in
  0.1.1), `sql`, `rest` and `glue` types, `register_catalog_type` for your own, and
  `catalog_namespace`. Configs name environment variables for tokens, credentials and passwords
  (`token_env`, `credential_env`, `password_env`, `properties_env`); literal secrets in a spec are
  rejected, and reprs and logs redact secret-looking keys. `Iceberg(..., catalog_obj=)` takes an
  existing catalog object.
- Object storage: `local_data_platform.fs` (`filesystem_for`, `open_input`, `open_output_atomic`,
  `exists`) over `pyarrow.fs`. CSV, Parquet and JSON accept `s3://` and `gs://` paths, and `s3://`
  honours `AWS_ENDPOINT_URL_S3` and `AWS_ENDPOINT_URL`.
- The staged publish protocol in `format/iceberg/commit.py`: `CommitContext` (with
  `CommitContext.create`), `CommitPolicy`, `StagedWrite`, `branch_name`, `ensure_base`,
  `find_commit`, `fence`, `stage`, `publish`, `write_once`, `CommitConflict` and
  `CommitSearchExhausted`. `Iceberg.put(df, mode=None, *, commit=None, overwrite_filter=None,
  policy=None)` uses it when given a `CommitContext`: a re-run with the same idempotency key is a
  no-op (`skipped_duplicate=True`), a concurrent writer causes a rebase and retry, and superseded
  attempts are fenced. `WriteResult` gains `branch`, `idempotency_key`, `attempts` and
  `skipped_duplicate`. It needs pyiceberg 0.10 or newer.
- Run events: `events.RunEvent` and the `NullSink`, `MemorySink`, `JsonlSink`, `OpenLineageSink`,
  `IcebergSink` (the `_ldp.runs` and `_ldp.quality_results` tables) and `MultiSink` sinks, plus
  `read_jsonl`, `read_iceberg_events` and `summarize_runs`.
  `Pipeline.run(mode=None, *, commit=None, sink=None)` emits them, and `PipelineResult` gains
  `run_id`, `idempotency_key`, `published_snapshot_id` and `status`. Config
  `metadata.observability` sets the default sinks. `run_config(..., window=)` writes through the
  staged protocol with a key derived from the config and the window.
- `spec`: `API_VERSION = "ldp/v1"`, `json_schema()`, `validate_spec()`, `spec_hash()`,
  `idempotency_key()` and `plan()`.
- DuckDB reads Iceberg natively with the `iceberg` extension's `iceberg_scan` (streaming, with
  filter pushdown), falling back to the in-memory scan when the extension can't load. Also
  `attach_rest`, `snapshots` and `files`.
- `engine.router`: `estimate_scan_bytes` and `choose_engine`. Nothing calls them yet.
  `engine.spark` gains `spark_catalog_conf_for` for the `local`, `sql` and `rest` catalog types.
- A read-only MCP server for agents, `local_data_platform.mcp_server`, run with `ldp mcp` (or
  `python -m local_data_platform.mcp_server`). It has `list_tables`, `describe_table`, `query`,
  `sample_rows`, `table_history` and `get_dataset` tools, single-statement `SELECT` guardrails, a
  locked-down DuckDB, row and time caps, a table allowlist, and an audit log written to JSONL and to
  the `_ldp.audit` Iceberg table (`IcebergSink.emit_audit`; `--no-iceberg-audit` turns the table
  off). `examples/agent_client.py` drives it as a scripted agent.
- Reproducible datasets: `datasets.pin`, `load`, `list_versions`, `list_datasets`, `get_version` and
  `export`, with JSON manifests under `<warehouse>/.ldp/datasets/` and a tag on each pinned
  snapshot.
- Temporal quality checks in `quality.temporal`: `monotonic`, `max_skew`, `rate_below` and
  `max_gap`.
- `examples/robot_episodes/`: humanoid-robot episodes and sensor frames in bronze, silver and gold
  tables, temporal checks, a pinned training split, DuckDB queries and an optional Spark aggregate.
- Table maintenance: `maintenance.expire_snapshots` (never past the idempotency floor),
  `find_orphans` and `remove_orphans` (a dry run by default).
- `tools/rest_fixture`: Apache Iceberg's REST catalog test server, run with scala-cli, for the
  opt-in REST catalog tests (`LDP_RUN_REST=1`) and `make demo-rest`.
- Makefile targets `demo-robotics`, `demo-agent`, `demo-spark`, `demo-rest`, `demo-all`,
  `spark-test` and `rest-test`.
- The new `ldp` subcommands, registered by each module's `add_cli`: `ldp catalog test`,
  `ldp schema`, `ldp plan`, `ldp runs`, `ldp commits`, `ldp datasets pin|list|export`,
  `ldp maintain`, `ldp mcp` and `ldp spark`. Their modules are imported only when the command line
  may need them, so `ldp --version` and the 0.1.1 commands don't load them.
- `catalog.provider.catalog_database_file` and `require_catalog_database`, and
  `iceberg_from_config(..., must_exist=True)`, which every read-only command uses.
- The `mcp`, `s3` and `glue` extras. The `dev` extra adds `moto[server]` and the MCP SDK.
- The `rest` pytest marker, for REST catalog integration tests that run only with
  `LDP_RUN_REST=1`.
- An opt-in CI workflow, `.github/workflows/jvm.yml`, with `spark` and `rest` jobs on Temurin 17
  and scala-cli. It runs when started by hand, weekly, or on a pull request labelled `jvm`, and its
  jobs never block a merge.
- Docs: `docs/catalogs.md`, `docs/exactly_once.md`, `docs/observability.md`, `docs/agents.md` and
  `docs/robotics.md`, with the v0.2.0 contract (and its implementation status) and the SaaS
  architecture proposal in the site navigation.

### Changed

- Direct-mode Iceberg writes make one commit per write: the schema union and the data write run in
  one transaction.
- `rows_before` and `rows_after` come from snapshot summaries (`total-records`) instead of a scan.
- `format/iceberg` builds its catalog with `create_catalog`, so `ldp run`, `ldp snapshots` and
  `ldp query` work with the new catalog types.
- CI installs the `s3` and `glue` extras with `dev` and `bigquery`, so the moto-backed S3 and Glue
  tests and the MCP tests run on every push.
- `.gitignore` also ignores `.ldp/` run state and scala-cli, Bloop and Metals build output.
- `make lint` (and so CI) also lints `scripts/`, which now passes flake8.
- `docs/design/v0_1_1.md` records F9 (Scala Spark) as implemented, as the experimental
  `engine.spark`.

### Fixed

- A direct `overwrite` or `upsert` could lose or duplicate rows when two processes wrote the same
  local table at once. Both now take an exclusive lock on
  `<warehouse>/.ldp/locks/<namespace>.<table>.lock`.
- A write that added columns made two commits, so a reader or a failure between them could see the
  new schema without the data.
- `make generate-docs` ran an empty `docs/scripts/generate_issue_list.py`. It now runs
  `scripts/generate_issue_list.py`, and the empty copy is removed.
- Read-only commands on a `sql` config whose SQLite file didn't exist created an empty catalog
  (0.1.1 guarded only `local` catalogs). `ldp snapshots`, `query`, `commits`, `maintain`,
  `datasets pin` and `catalog test` now fail without creating anything.
- An upsert batch that lacked one of the table's columns failed deep inside pyiceberg with
  "Target schema's field names are not matching". It is now a `ConfigError` naming the missing
  columns, in direct and staged mode, and nothing is written.
- `make rest-test` (`pytest -m rest`) ran none of the REST catalog tests, because
  `tests/test_rest_catalog.py` wasn't marked `rest`.

### Removed

- `how_to_setup.md`, a Poetry-based setup guide that no longer matched the build, and the unused
  root `poetry.lock`. `make install` and the README cover setup.
- The empty `scripts/github_api.py`, which nothing imported.

### Not done yet

- No integration tests yet for Postgres `sql` catalogs, hosted REST catalogs or `gs://` reads and
  writes.
- Nothing calls the engine router (`engine.router`) yet.

## [0.1.1] - Not released separately

The hardening work: one package, correct and idempotent writes, data quality checks, local SQL,
and a green CI. The design contract is in `docs/design/v0_1_1.md`. It was never published to PyPI
on its own and ships as part of 0.2.0. (A `release-v0.1.1` git tag from 2024-10-30 points at older
code.)

### Added

- Write modes for Iceberg targets: `append`, `overwrite` (replaces the table's rows, so re-runs
  are idempotent) and `upsert` on configured `join_cols`. `Iceberg.put` returns a `WriteResult`
  with row counts before and after, and the snapshot ID.
- Partitioned Iceberg tables from `partition_by` in the config: `identity`, `year`, `month`,
  `day`, `hour`, `bucket[N]` and `truncate[W]`.
- Time travel: `Iceberg.snapshots()` and `Iceberg.get(snapshot_id=...)`, plus `row_filter`,
  `selected_fields` and `limit` on reads.
- Schema evolution: new columns in a batch are added to the table by name.
- A `quality` module with `row_count`, `not_null`, `unique`, `accepted_values`, `range`,
  `freshness` and `schema` checks. They run before the write, and with `on_failure: fail` a failed
  check raises `DataQualityError` and nothing is written.
- `DuckDBEngine` in `engine.duckdb` for SQL over Iceberg and Arrow tables (the `[duckdb]` extra).
- A pipeline registry and factory: `register_pipeline`, `create_pipeline(config)` and
  `registered_pipelines()`. An unknown route raises `PipelineNotFound` listing the registered ones.
- `Pipeline` built by composition from a source, a target, transforms and checks, returning a
  `PipelineResult`. New built-in `IcebergToParquet`.
- The `ldp` command: `run`, `pipelines`, `snapshots`, `query` and `demo`.
- `ldp demo`, an offline end-to-end walkthrough on deterministic synthetic data.
- `Config.from_json`, which resolves the config's paths against the config file's folder, and
  `resolve_path`.
- `github.get_item` and an exception hierarchy under `LDPError`.
- `CHANGELOG.md`, `docs/quickstart.md`, `examples/README.md` and
  `docs/design/factory_registry.md`.
- Tests for packaging (version, no import-time side effects, src-only layout) and for every
  example config.
- Makefile targets `install`, `lint`, `test`, `demo`, `build`, `smoke`, `docs`, `serve-docs`,
  `clean` and `all`.
- Experimental Spark support in `engine.spark`: `SparkEngine` (PySpark, the new `spark` extra, plus
  a JDK 17+) and `ScalaSparkJob`, which runs `spark/IcebergJob.scala` with scala-cli. Both open the
  same SQLite catalog as pyiceberg. Their integration tests are opt-in (`LDP_RUN_SPARK=1`) and not
  run in CI. See `docs/spark.md`.
- `LocalIcebergCatalog.close()` (also a context manager) and `LocalIcebergCatalog.database_path()`.

### Changed

- The library lives only in `src/local_data_platform`, so the wheel you build is the code you
  test. The publish workflow builds from the repo root when a GitHub release is published, checks
  that the tag matches the package version, smoke-tests the wheel and uploads it with PyPI trusted
  publishing, so the repo holds no PyPI token.
- `pyproject.toml` uses PEP 621 metadata with the poetry-core backend. Dependencies are declared:
  `pyiceberg[pyarrow,sql-sqlite,pyiceberg-core]>=0.10,<0.13`, `pyarrow` and `requests`, with the
  `duckdb`, `bigquery`, `dev`, `docs` and (experimental) `spark` extras. `pyiceberg-core` is needed for partitioned writes
  with time, bucket or truncate transforms on pyiceberg 0.10 and later.
- Config paths are relative to the config file's folder, and absolute paths work.
- `ParquetToIceberg` is an ingestion pipeline.
- The BigQuery source runs each query exactly once and returns `job.result().to_arrow()`.
  The Google libraries are imported only when needed.
- `Issue` uses the GitHub REST API instead of scraping HTML, and raises `GitHubAPIError` on errors.
- The library no longer configures logging. It uses loggers under `local_data_platform`, and only
  the CLI and the demo add a handler.
- The example report scripts use `Config.from_json` and `create_pipeline` instead of an if/else on
  formats, and their configs use relative paths.
- CI runs lint, tests on Python 3.12 and 3.13 against pyiceberg 0.10.0 and the newest supported
  release, a wheel build with a smoke test, and `mkdocs build --strict`, on pushes and pull
  requests to `main` only.
- The README describes only what exists.

### Deprecated

- Legacy config paths with a leading slash that were really relative to the cwd (such as
  `"/rides.csv"`). They still resolve, with a `DeprecationWarning`, and support ends in 0.3.0.
- `pipeline.egression.csv_to_iceberg.CSVToIceberg`, which was CSV to Iceberg filed under
  egression. Use `create_pipeline`.
- `logger.log()`. Use `logger.get_logger(name)`.

### Removed

- The stale package copies in `local_data_platform/` and `local-data-platform/` at the repo root.
- Committed SQLite catalog files, `lumache.py`, `README.rst` and the old Sphinx docs.
- The `mkdocs_link_check.yml` workflow. The CI docs job runs `mkdocs build --strict`, which fails
  on broken links between pages.
- The `beautifulsoup4` dependency.
- The placeholder test.
- `local_data_platform.hello_world`, a project-template leftover that printed a greeting.

### Fixed

- Every CSV or Parquet to Iceberg load crashed on pyiceberg 0.12, because it called the private
  `_namespace_exists`. It now uses the public `create_namespace_if_not_exists`.
- Re-running a load duplicated its data. Use `overwrite` or `upsert`.
- The GCP credentials, private key included, were written to the log. Only the key path and
  `project_id` are logged now.
- A BigQuery query ran twice, once inside a log line.
- `store.target.iceberg`, `issue` and `etl` failed to import.
- `Table.get()` raised `AttributeError` instead of `TableNotFound`.
- `CSV.put` wrote empty files and failed on `None`. It now raises `ValueError` for both, and writes
  atomically.
- `Pipeline.load()` never called `transform()`.
- The pipeline constructors disagreed, so egression `CSVToIceberg` failed with a `TypeError`.
- The timestamp downcast was stored as a fake table property and set through `os.environ` at
  import time. Nanosecond timestamps are now cast to microseconds before the write.
- The test suite failed at collection because a root-level package shadowed `src/`.
- Iceberg reads and writes left `ResourceWarning: unclosed database` behind. The catalog's pooled
  SQLite connections are now closed when it is closed or garbage collected.
- A legacy leading-slash path to an output that didn't exist yet (such as `"/out/rides.csv"`) was
  kept as an absolute path, so the write failed at the filesystem root. It now falls back to the
  relative folder when that exists, with the same `DeprecationWarning`, and the warning points at
  the caller's code rather than the library's.
- `ldp snapshots` and `ldp query` on a config whose catalog didn't exist created an empty catalog.
  They now fail without creating anything.

## [0.1.0] - 2024-10

The first release on PyPI, with a different module layout (`pipelines/python/...`,
`source/...`) from 0.1.1.
