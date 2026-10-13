# Local Data Platform

A modern, modular, and developer-friendly platform for local data engineering, analytics, and experimentation.

> Want to contribute? Check out the [**Contributing Guide**](contributing.md)!

[Explore Recipes & Examples](recipes.md){ .md-button .md-button--primary }

[View Open Issues](user_issues.md){ .md-button }

---

## Start here

- [Quickstart](quickstart.md): install, run `ldp demo`, and load your own data.
- [Recipes](recipes.md): short, runnable examples of the Python API.
- [Warehousing](warehousing.md): from Excel and CSV exports to dbt-tested Iceberg tables, with churn and time travel.
- [v0.1.1 hardening contract](design/v0_1_1.md): what each module does.
- [Roadmap](roadmap.md): the milestones from 0.1.3 to 0.2.0 and what each one adds.

## New in 0.1.2

0.1.2 adds warehousing. `ldp dbt` runs a dbt project over your Iceberg tables and publishes the models you mark
back to Iceberg, an `XLSX` source reads Excel sheets, and `ldp query` reads older snapshots and joins the tables of
a folder. Direct appends from several processes at once no longer lose batches on pyiceberg 0.11. The
[warehousing guide](warehousing.md) walks through all of it on a restaurant chain's exports, and
[Warehousing with dbt](design/warehousing.md) is the design.

## New in 0.1.1

0.1.1 is the first release since 0.1.0. On top of the hardening work it adds the platform work,
"multi-writer, any catalog, object storage, agent-ready". Its contract is the
[v0.1.1 platform contract](design/v0_1_1_platform.md), which is Phase 1 of the
[SaaS architecture](design/saas_architecture.md) proposal. The features below work from Python and
from new `ldp` subcommands (`ldp catalog test`, `ldp schema`, `ldp plan`, `ldp runs`,
`ldp commits`, `ldp datasets`, `ldp maintain`, `ldp mcp` and `ldp spark`). Each guide shows both.

| Guide | What it covers |
|---|---|
| [Catalogs and object storage](catalogs.md) | The `local`, `sql`, `rest` and `glue` catalog types, secrets through environment variables, and `s3://` / `gs://` paths |
| [Exactly-once writes](exactly_once.md) | Direct-mode fixes, and the staged publish protocol with idempotency keys and fencing |
| [Run events and the `_ldp` namespace](observability.md) | `RunEvent`, the JSONL, OpenLineage and Iceberg sinks, and reading the run history |
| [MCP server for agents](agents.md) | The read-only MCP server: its tools, guardrails and audit log |
| [Robot-episode datasets](robotics.md) | Temporal quality checks, pinned dataset versions and the robotics example |
| [Spark (experimental)](spark.md) | The Scala Spark job and the PySpark engine on the same catalog |

Most have a demo: `make demo-robotics`, `make demo-agent`, `make demo-warehouse`, `make demo-spark` and
`make demo-rest`. `make demo-all` runs them all; `demo-warehouse` needs the `dbt` and `excel` extras, and the two
JVM demos need [scala-cli](https://scala-cli.virtuslab.org).

## 🏆 Issues to contribute on

The open issues, tagged by status and theme, are listed on [User Issues](user_issues.md). `make generate-docs`
refreshes that page from the GitHub API.

---

## 📋 Top PRs to Review

For project managers and senior contributors, this section highlights key pull requests that are ready for review.

[Review Open Pull Requests](pr_reviews.md)
