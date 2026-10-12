# Project Structure

The layout of the repository for 0.1.1. The library lives only in `src/local_data_platform/`, so
the wheel you build is the code the tests import.

```text
local-data-platform/
├── src/local_data_platform/     the package
│   ├── __init__.py              __version__, Base, Table, Flow, Worker, SupportedFormat, SupportedEngine
│   ├── config.py                Config.from_json / from_dict
│   ├── paths.py                 resolve_path: config paths relative to the config file
│   ├── spec.py                  the ldp/v1 JSON Schema, validate_spec, spec_hash, idempotency_key (platform)
│   ├── fs.py                    local, s3:// and gs:// IO with atomic writes (platform)
│   ├── exceptions.py            LDPError and its subclasses
│   ├── logger.py                get_logger; configure_cli_logging for the CLI only
│   ├── format/                  CSV, Parquet and Iceberg tables (get / put), WriteResult;
│   │                            format/iceberg/commit.py is the staged publish protocol (platform)
│   ├── catalog/                 LocalIcebergCatalog, and provider.py: create_catalog for the
│   │                            local, sql, rest and glue types (platform)
│   ├── store/                   sources (BigQuery, JSON) and older import paths kept for compatibility
│   ├── quality/                 checks, run_checks, QualityReport, and temporal.py with the time-series checks (platform)
│   ├── events.py                RunEvent and the JSONL, OpenLineage and Iceberg sinks (platform)
│   ├── engine/                  DuckDBEngine, router.py (platform) and experimental Spark (engine.spark)
│   ├── mcp_server/              the read-only MCP server for agents (platform)
│   ├── datasets.py              snapshot-pinned dataset versions (platform)
│   ├── maintenance/             snapshot expiry and orphan files (platform)
│   ├── pipeline/                Pipeline, the built-in pipelines and the registry (create_pipeline)
│   ├── etl.py                   run_config and load_config
│   ├── cli.py                   the ldp command
│   ├── demo.py                  ldp demo
│   └── github/, issue/          GitHub REST helpers used by the docs tooling
├── tests/                       pytest suite (not shipped in the wheel)
├── examples/                    runnable configs and scripts: NEAR, NYC taxi, robot episodes, an MCP agent client
├── docs/                        these docs (MkDocs), with the 0.1.1 hardening and platform contracts in docs/design
├── spark/                       the Scala Spark job, run with scala-cli (experimental)
├── tools/rest_fixture/          Apache Iceberg's REST catalog test server, run with scala-cli
├── samples/                     a standalone BigQuery tutorial script
├── scripts/                     docs and PR-history tooling
├── .github/workflows/           CI (lint, tests, wheel smoke test, docs), the opt-in JVM jobs and the PyPI publish job
├── Makefile                     install, lint, test, the demos, build, smoke, docs, clean, all
├── pyproject.toml               PEP 621 metadata, poetry-core build backend
├── requirements.txt             docs requirements for Read the Docs and CI
├── CHANGELOG.md
└── README.md
```

The modules marked (platform) come from the
[v0.1.1 platform contract](../design/v0_1_1_platform.md#implementation-status). They work from
Python, and `cli.py` registers their `ldp` subcommands through each module's `add_cli`.

## .gitignore

Local artefacts are ignored so they are never committed:

- the virtualenv and build output (`.venv/`, `dist/`, `build/`, `*.egg-info/`)
- test and Python caches (`.pytest_cache/`, `__pycache__/`, `.coverage`, `htmlcov/`)
- the MkDocs site (`site/`)
- anything a run writes locally: `warehouse/`, `ldp_demo/`, `tmp/`, SQLite files (`*.db`) and
  `.ldp/` (event and audit logs, locks, dataset manifests)
- scala-cli, Bloop and Metals build output (`.scala-build/`, `.bsp/`, `.bloop/`, `.metals/`)
- editor folders and local env files (`.idea/`, `.vscode/`, `.env`)
