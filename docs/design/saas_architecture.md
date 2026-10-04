# Design: LDP Cloud, a SaaS control plane for Iceberg tables

Status: proposal, 2026-09-30. Nothing in this document changes the [v0.1.1 contract](v0_1_1.md); it
describes what is built on top of it, in which order, and what would make the result worth a
billion dollars or tell us early that it will not be.

This design starts from the operations-first proposal and takes ideas from the two proposals that
lost to it (see [§15](#15-alternatives-considered)). It fixes every fatal flaw the technical,
business and execution reviews raised. Market numbers come only from the research brief and link
to their source; "reported" marks a figure the brief marks as reported rather than confirmed. Numbers
without a link are planning assumptions and are labelled as such.

!!! note "Verified against the code on 2026-09-30"
    Statements about local-data-platform and pyiceberg in this document were checked against the
    working tree on `feat/v0.1.1-hardening` and the installed pyiceberg 0.12.0. Line numbers refer
    to `src/local_data_platform/format/iceberg/__init__.py` unless another file is named. The
    concurrency results in §2 and §7 come from small scripts run against pyiceberg 0.12.0's
    `SqlCatalog` on SQLite. They will be checked in as tests in Phase 1; Postgres runs are pending,
    because Postgres and Docker are not installed on the machine used.

    The line numbers and the four write-path defects in §2.1 describe the tree before the 0.2.0
    work. The [v0.2.0 contract](v0_2_0.md) fixes them (C1, C3, C5), and its
    [implementation status](v0_2_0.md#implementation-status) says what exists now. Nothing in this
    document beyond that contract has been built.

---

## 1. Summary

Apache Iceberg is now the table format every large platform reads and writes. Snowflake made Iceberg
v3 GA on May 7, 2026
([source](https://docs.snowflake.com/en/release-notes/2026/other/2026-05-07-iceberg-v3-ga)).
Databricks lets any engine read and write Unity Catalog Iceberg tables over REST
([source](https://www.databricks.com/blog/unity-catalog-and-next-era-apache-icebergtm)). Google's
BigLake REST catalog went GA on Nov 20, 2025
([source](https://cloud.google.com/blog/products/data-analytics/biglake-metastore-now-supports-iceberg-rest-catalog/)).
Storage, catalogs and compaction are turning into commodities:

- Apache Polaris is an Apache Top-Level Project
  ([source](https://www.snowflake.com/en/blog/apache-polaris-top-level-project/)).
- AWS cut S3 Tables compaction prices by 50–90% in July 2025
  ([source](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-s3-tables-reduce-compaction-costs)).
- R2 storage costs $0.015/GB-month with no egress fees
  ([source](https://blog.cloudflare.com/r2-data-catalog-public-beta/)).

One thing is still unowned. When a Python pipeline leaves a laptop and gains a schedule, a second
writer and a downstream consumer, nothing guarantees neutrally, on the catalog the team already
runs, that:

- a logical window is published at most once;
- data that failed its checks never reaches `main`;
- each snapshot can be traced to the spec, run, quality verdict and cost that produced it.

LDP already has the laptop half:

- a validated spec;
- idempotent `overwrite` and `upsert` writes;
- quality checks that block a bad load;
- DuckDB, PySpark and a Scala Spark job on one catalog.

**LDP Cloud** sells the always-on half: coordination across writers, a ledger of every commit,
table maintenance, cost per table, and governance. It runs as a hosted control plane that drives a
data plane in the customer's own cloud account.

**The decision.**

1. **Operations-first, minimal control plane.**
   - One FastAPI service, one Postgres per cell, and no message bus.
   - The queue uses Postgres `SKIP LOCKED`. The outbox is a table that a relay polls.
   - We buy everything we can: RDS, ECS/Fargate, EMR Serverless, auth, billing and compliance
     tooling.
2. **BYOC only in year 1.**
   - The data plane is `ldp agent`, a command in the same Apache-2.0 wheel as `ldp run`. It runs
     in the customer's account from one Terraform module.
   - A pooled, LDP-hosted tier arrives in Phase 4. It needs a gVisor sandbox for user Python that
     has passed a pen test.
3. **Catalog-neutral; we build no catalog.**
   - Customers keep Glue, S3 Tables, Polaris, Unity, BigLake, R2 or Postgres.
   - Hosted Polaris is an option when the pooled tier ships.
   - We never run two catalogs over one table.
4. **Correctness lives in the library and the catalog, not in the control plane.**
   - Every cloud write stages on a private branch per attempt.
   - It then publishes with one `Catalog.commit_table` call that carries two ref requirements:
     `main == base` and `branch == staged`.
   - This call bypasses pyiceberg's blind commit retry, fences zombie and superseded attempts at the
     commit point, and holds even while the control plane is down.
   - Verified on pyiceberg 0.12.0 (§7).
5. **Idempotency keys are built from the data they write.** The key is
   `sha256(pipeline_id, target table_uuid, logical window)`. The spec version is recorded next to
   it but is not part of it, so redeploying a spec cannot duplicate a window.
6. **Paid features are coordination, history, maintenance, cost, governance and an SLA.**
   Correctness primitives stay free.
7. **Pricing is per org:** a platform fee plus a fee per governed table, with no markup on customer
   compute.
8. **Three gates come before any control-plane code reaches a customer:**
   - Commit and publish 0.1.1, which takes days, not weeks.
   - A six-week paid concierge test. Stop if fewer than 3 of 10 qualified teams pay.
   - A multi-writer harness showing zero lost and zero duplicated rows.

**What this can be worth.** The plan in §11 reaches about $100M ARR at the end of 2032. At an
assumed 10× ARR multiple that is $1B. The closest comparable warns that orchestration and
control-plane companies often stall well short of that: Astronomer is valued at $775M
([reported](https://research.contrary.com/company/astronomer)) on about $39.5M of revenue
([unverified](https://research.contrary.com/company/astronomer)). So $1B also needs two things. The
ledger has to become the customer's system of record, and at least three modules have to be adopted
per customer. §11 turns both into tests at each fundraise.

---

## 2. Problem, users, wedge

### 2.1 The problem, reproduced

Three things break when a pipeline stops being one person's script.

1. **Concurrent writers corrupt keyed tables.**
   - Test: four processes, each running 15 overlapping `upsert`s over a 40-key space, against one
     pyiceberg 0.12.0 `SqlCatalog`.
   - Result: in 3 of 3 runs, 14–20 of the 35–39 ids ended up duplicated. After that, every upsert
     failed with `Target table has duplicate rows, aborting upsert`.
   - Cause: pyiceberg's own retry.
     - `Transaction.commit_transaction` retries `CommitFailedException` 4 times by default, with a
       30-minute total timeout (`commit.retry.num-retries`, `commit.retry.total-timeout-ms`).
     - Each retry rebuilds the snapshot through `_rebuild_snapshot_updates`, which rebases the
       insert half of an upsert without re-reading keys.
     - Before rebuilding, it checks only whether its *own* snapshot id already landed.
   - Keyed appends in the same test lost 0 rows and duplicated 0.
   - Issue #88 (optimistic concurrency) was closed without code.
2. **Re-runs duplicate or lose data.**
   - Appending twice duplicates, which is why the demo's second step exists.
   - A run that crashes after its commit cannot tell whether the commit landed.
   - Nothing ties a retry to the window it is retrying.
3. **Nothing links a number to what produced it.**
   - LDP's checks already block a bad in-memory batch (`quality/checks.py`, `runner.py`).
   - Nothing records what was published, by which run, from which input, with which checks, across
     Python, Spark and whatever else writes the table.

The current write path also has four defects that must be fixed before any second writer is
allowed:

| Defect | Where | Consequence |
|---|---|---|
| Schema evolution commits on its own | `_evolve_schema` calls `table.update_schema()` (line 545), called at line 447, before the data commit (lines 452, 455, 458) | A failed data write leaves an evolved schema with no data, and every evolving write makes two metadata versions |
| Racy counts | `rows_before` and `rows_after` come from separate `_row_count` reads (lines 448, 467) | `WriteResult` is wrong under concurrency |
| Catalog hard-coded | `LocalIcebergCatalog(...)` at line 284 | There is no Postgres, REST or Glue path |
| Full materialisation | `scan(...).to_arrow()` in `engine/duckdb/__init__.py` line 150 | Queries are capped by RAM |

### 2.2 Who pays first

The ideal customer profile (ICP) comes from the business-first proposal, which the business review
rated highest. It fits every row below.

| Attribute | Value |
|---|---|
| Company | Digital-native, 200–2,000 employees, Series B to pre-IPO; fintech, consumer apps, B2B SaaS |
| Team | 3–15 data engineers who write Python |
| Data | 5–100 TB on AWS or GCP, plus an existing Snowflake or Databricks bill |
| Decision already made | Move bronze and silver layers to Iceberg in the company's own bucket; the gold layer stays in the warehouse |
| Writers today | Hand-rolled pyiceberg or pandas scripts under Airflow or cron |
| Buyer / champion | Head of data or data-platform lead / a senior data engineer |
| Budget | Warehouse compute they are moving off-platform, and platform headcount they cannot hire |

**Expansion workloads.** Two follow from the same wedge:

- Consumer-scale event analytics (#89): hour-partitioned tables, compaction as the main cost lever,
  and funnel and session models.
- Regulated CDC into a lakehouse, where only BYOC is acceptable.

### 2.3 Trigger and wedge

**Trigger:** the first time a laptop pipeline needs a schedule, a second writer or a downstream
consumer. In the product, that moment is `ldp deploy`.

**Offer:** "Deploy an `ldp` spec against your own bucket and catalog in 15 minutes. You get:

- scheduled, fenced publishes that are gated on quality;
- a ledger of every snapshot on the table, including snapshots from other engines;
- Slack and webhook alerts;
- cost per table.

The free tier covers 25 tables."

### 2.4 Who we say no to (or serve for free)

**Single-analyst SMBs.** This covers the Sheets sync (#106), the restaurant data mart (#97), and the
"personal data mastery" audience in `docs/wiki/VISION.md`. They get OSS templates and Cloud Free as
top of funnel, not as a revenue base.

- The research found no billion-dollar outcome among "data stack in a box" companies: Mozart raised
  a $15M Series A ([reported](https://techcrunch.com/2022/04/27/mozart-data-raises-15m-to-help-startups-spin-up-a-data-stack/)),
  Keboola $32M ([source](https://www.keboola.com/blog/keboola-data-operations-supercharger-raises-32m-in-series-a-funding)),
  and Y42 $31M ([reported](https://techcrunch.com/2021/10/25/data-platform-y42-raises-31m/)).
- Definite already sells that bundle from $250/month
  ([source](https://www.definite.app/blog/duckdb-ducklake-business-case)).

**Other anti-ICPs:**

- Teams all-in on Databricks: Unity Catalog already covers them.
- SQL-only teams with no Python.
- Sub-second OLAP workloads.

### 2.5 Why now

- **The Python Iceberg writer is mainstream.** pyiceberg had 18.9M PyPI downloads in the 30 days to
  2026-09-29 and 905K on that day ([source](https://pypistats.org/api/packages/pyiceberg/recent)).
  Downloads include CI traffic and swing widely, so we steer by weekly-active projects, not
  downloads.
- **Teams run several engines on one table.** In the community Iceberg survey (self-selected, n not
  stated), usage is Spark 96.4%, Trino 60.7% and DuckDB 28.6%, and 78.6% use Iceberg only
  ([source](https://datalakehousehub.com/blog/2026-02-state-of-the-apache-iceberg-ecosystem/)).
- **Catalogs are fragmented.** In the same survey: Glue 39.3%, Nessie 28.6%, S3 Tables 25.0%,
  Polaris 21.4%, Lakekeeper 21.4%. A tool that needs its own catalog starts from zero; a tool that
  works on any catalog does not.
- **DuckDB writes Iceberg.** Inserts arrived in v1.4.0, and updates and deletes in v1.4.2
  ([source](https://duckdb.org/2025/11/28/iceberg-writes-in-duckdb)). Laptop tools are now real
  writers to production tables.
- **The warehouse is not shrinking.** Snowflake product revenue grew 37% year on year in Q2 FY2027,
  with 126% NRR ([source](https://www.sec.gov/Archives/edgar/data/0001640147/000164014726000033/fy2027q2earnings.htm)).
  Our bet is not that the warehouse loses. It is that more of every table is written by
  non-warehouse writers, and those writes need governing. We are complementary: Snowflake reads S3
  Tables over REST, GA Aug 10, 2026
  ([source](https://docs.snowflake.com/en/release-notes/2026/other/2026-08-10-amazon-s3-tables-iceberg-rest-catalog-integration-ga)).

### 2.6 Competitive map

| Alternative | What it owns | Where LDP is different | Our posture |
|---|---|---|---|
| Fivetran + dbt Labs: about $600M combined ARR ([source](https://www.fivetran.com/press/fivetran-and-dbt-labs-unite-to-set-the-standard-for-open-data-infrastructure-2025)), 100,000+ data teams, merger closed June 1, 2026 ([source](https://www.fivetran.com/press/fivetran-dbt-labs-complete-merger-to-create-the-data-infrastructure-for-trusted-ai-agents)) | Managed SaaS ingestion and SQL transforms | Python-authored writes, correctness with several writers, a ledger across all writers including theirs | **Complement.** No SaaS connector catalogue; `dbt-duckdb` over LDP tables; their commits show up in our ledger |
| Estuary, Confluent Tableflow, Qlik (Upsolver), Cloudflare Pipelines | CDC and streams landed in Iceberg | We govern what happens after data lands: Python transforms, quality gates, maintenance, audit | **Complement.** We do not lead with CDC or event ingestion |
| S3 Tables, BigLake, Unity Catalog, Polaris, R2 Data Catalog | Storage, catalog, compaction | Neutral across all of them; the laptop-to-prod loop; ledger and cost across catalogs | **Run on top.** Defer to native compaction where it exists |
| Airflow/Astronomer, Dagster/Prefect | Scheduling Python tasks | They see tasks, not commits; no fenced publish, no per-table ledger | **Sit underneath.** Free operators send run events to our ledger |
| MotherDuck ($400M post-money, 2023, [source](https://www.prnewswire.com/news-releases/motherduck-raises-52-5-million-series-b-funding-as-duckdb-adoption-soars-301932741.html)) | Hosted DuckDB with its own storage | Iceberg in the customer's bucket, any engine | Different buyer; DuckDB is one of our engines |
| Bauplan ($7.5M seed, [reported](https://technews180.com/funding-news/python-first-ai-platform-bauplan-scores-7-5m-in-seed-funding/)), Tower (EUR 5.5M, [reported](https://tech.eu/2026/03/13/tower-secures-eur55m-to-support-data-engineers-in-the-ai-era/)) | Serverless Python on Iceberg | BYOC by default, any catalog, Spark and DuckDB on one table | Head-to-head; win on neutrality and BYOC |
| Self-managed Glue + Spark + Airflow + a quality tool | Everything, operated by hand | We remove the operations work and add guarantees | The status quo we replace |

---

## 3. Principles

These are the three DDIA goals plus the constraints of this product. Each one has a consequence in
the design.

1. **Reliability: correctness first, and it must not depend on our uptime.**
   - Publishing is safe with any number of writers, zombies and retries, on any catalog that
     validates Iceberg requirements atomically.
   - The control plane can be down and the guarantees still hold.
   - When a guarantee cannot be proven, we fail closed. For example, if the idempotency search
     exceeds its bound, we do not publish.
2. **One serialisation point per table: the catalog's compare-and-swap.** We never add a second
   one, whether a lock service, a second catalog or a "catalog in front of a catalog". Locks and
   lanes exist to save retries, never to make writes correct.
3. **Idempotent at every step.**
   - Runs are created once per key.
   - Each staged attempt is private.
   - Publishing happens at most once per key.
   - Events are applied at most once per `(run_id, attempt, seq)`.
   - Usage is applied at most once per `event_id`.
   - Anything may be retried.
4. **Keep the system of record separate from derived data.**
   - Iceberg metadata is the truth about tables.
   - Postgres is the truth about runs, leases and billing.
   - Lineage, search, dashboards and the `_ldp` tables are derived and can be rebuilt.
5. **Scale with boring parts.**
   - Postgres until the numbers in §8 say otherwise; per-region cells for blast radius, not
     throughput.
   - No Kafka and no bespoke catalog.
6. **Maintainability for a team of 3–5.**
   - The same wheel runs on the laptop, in CI, in the runner and in the agent.
   - We buy anything that can be bought.
   - We page on SLO burn only.
   - Customer-caused failures page the customer, not us.
7. **Local first.** The OSS works offline with no account and no telemetry unless you opt in. The
   cloud is always an explicit `ldp login`. Resolving #29, the promise changes from "your data
   never leaves your machine" to "your data never leaves your account".
8. **Open formats, no lock-in.**
   - Tables are plain Iceberg.
   - Specs live in the customer's git.
   - The ledger is also written into the customer's own catalog as `_ldp.*` Iceberg tables.
   - A customer who leaves keeps everything except our coordination service.

---

## 4. Product surface and open-core boundary

### 4.1 The spec: `apiVersion: ldp/v1`

The spec is today's `Config` with the 4W1H fields. `Config.from_dict` already rejects unknown keys,
which makes additive versioning safe. The following fields are new and optional:

| Field | Meaning | Default |
|---|---|---|
| `apiVersion` | Spec version | `ldp/v1` |
| `metadata.target.catalog.type` | `local`, `sql`, `rest`, `glue` (plugins can add types) | `local`; today's `{identifier, warehouse_path}` keeps working |
| `metadata.target.catalog.ref` | A logical catalog name resolved through `~/.ldp/profiles.toml` or the workspace | none |
| `schedule` | Cron expression | Derived from `when` (`daily` becomes `0 2 * * *` plus jitter) |
| `window` | `day`, `hour`, `15m`, `none`: the logical window each run owns | Derived from `schedule` |
| `publish` | `direct` or `staged` | `direct` locally, always `staged` in the cloud |
| `columns.<name>.pii` | PII tag | `false` |
| `sla.freshness_minutes` | Freshness SLA | From the `freshness` check, if present |
| `schema_change.allow` | Breaking changes allowed by this version | `[]` |

`ldp schema` prints the JSON Schema. `spec_hash` is the sha256 of the canonical JSON: sorted keys,
no whitespace, paths relative to `base_dir`.

### 4.2 OSS library and CLI (Apache-2.0)

The library is everything in the v0.1.1 contract, plus the Phase 1 modules in §13:

- the catalog provider;
- the stage, publish and fence commit primitives;
- `fs`, streaming DuckDB, the router, events, spec, and maintenance.

The CLI keeps the contract's verbs (`run`, `pipelines`, `snapshots`, `query`, `demo`) and adds:

| Verb | What it does |
|---|---|
| `ldp schema` | Print the `ldp/v1` JSON Schema |
| `ldp plan CONFIG [--cost]` | Diff against the live table and the deployed spec: schema, partition spec, breaking changes, and an estimate of scanned bytes |
| `ldp run CONFIG --idempotency-key K --window START/END` | A local run with the cloud's stage-and-publish protocol |
| `ldp maintain TABLE` | Expire snapshots (with the idempotency floor), remove orphans (dry run by default), compact |
| `ldp agent` | Run the lease loop against a control plane (Phase 2) |

Cloud verbs, which use profiles that work like kubectl contexts:

| Verb | What it does |
|---|---|
| `ldp login` | Authenticate to LDP Cloud |
| `ldp deploy CONFIG --workspace W [--import-history]` | Upload the spec; optionally upload `.ldp/events.jsonl` |
| `ldp runs [--pipeline P] [--watch]` | List runs |
| `ldp logs RUN` | Show a run's logs |
| `ldp backfill CONFIG --from D1 --to D2` | Run one run per window, each with its own key |
| `ldp rollback TABLE --to SNAPSHOT` | Move `main` back to a snapshot |
| `ldp dataplane create --aws` | Apply the Terraform module |

### 4.3 Control-plane API (`/v1`, OpenAPI; the SDK `local_data_platform.cloud.Client` is generated from it)

| Method and path | Purpose |
|---|---|
| `POST /v1/workspaces/{ws}/pipelines` | Body is the spec; returns `pipeline_id`, `spec_version`, and a dry-run plan |
| `PUT /v1/pipelines/{id}` | New immutable spec version |
| `POST /v1/pipelines/{id}/runs` | `{window?, mode?}` returns `run_id`; the `Idempotency-Key` request header dedupes API retries |
| `GET /v1/runs/{id}` | State, attempts, `WriteResult`, cost |
| `GET /v1/runs/{id}/quality` | `QualityReport.to_dict()`, PII-redacted |
| `GET /v1/tables/{id}` | Schema, partition spec, health (file count, average file size, snapshot count) |
| `GET /v1/tables/{id}/ledger` | Every snapshot on `main`, with the run, spec, key, engine, quality and cost behind it |
| `POST /v1/tables/{id}/rollback` | `{snapshot_id}`; carried out by the agent |
| `POST /v1/query` | `{sql, params, pins}`; the agent runs it in the data plane and returns capped Arrow IPC |
| `GET /v1/usage?group_by=table,pipeline,meter` | Cost and usage |
| `POST /v1/data-planes` | Register a data plane; returns a bootstrap token with a 1 h TTL |
| `GET /v1/audit` | Hash-chained audit log |

Errors map from the existing `LDPError` hierarchy:

| Error | Status |
|---|---|
| `ConfigError` | 400 |
| `TableNotFound`, `PipelineNotFound` | 404 |
| `CommitConflict` (retries exhausted) | 409 |
| `DataQualityError` | 422, with the report as the body |
| Quota or budget exceeded | 429 with `Retry-After` |
| `EngineNotFound` | 501 |

### 4.4 Console

- **Runs:** timeline and state machine, with a quality verdict per run.
- **Table page:**
  - the ledger, as a snapshot timeline with WAP branches;
  - a diff between two snapshots (row counts and schema, through DuckDB);
  - one-click rollback;
  - health.
- **Quality trends** per check, and a freshness SLA board.
- **Cost per table and per pipeline:**
  - compute seconds reported by runners, at a price table the customer confirms;
  - storage from the `total-files-size` snapshot summaries;
  - warehouse credits through an optional usage-view connection (Phase 4).
- **Budget caps:** alert at 80%, hard stop at 100% on self-serve.
- **"Incidents prevented":** blocked bad writes per week, the ROI number we show every customer.

### 4.5 Integrations

These are free and sit under whatever the customer already runs:

- Airflow `LdpRunOperator` and a Dagster `ldp_asset` wrapper, both calling `etl.run_config` with an
  `HttpSink`. Orchestrator users keep their orchestrator, and their runs still land in the ledger.
- A `dbt-duckdb` profile generator for gold models over LDP tables.
- An OpenLineage emitter.
- `ldp import-gx`, which converts Great Expectations suites into LDP checks.

### 4.6 The open-core line

**Rule:** anything one engineer needs to build, test and run a *correct* pipeline on one machine,
or as a self-hosted install for one tenant, is free. Anything that coordinates many writers or many
people, keeps history, or runs around the clock on our infrastructure is paid.

| Capability | OSS | Cloud Free | Team | Business | Enterprise |
|---|---|---|---|---|---|
| Spec, formats, catalogs (local, sql, rest, glue), DuckDB, PySpark, Scala | yes | yes | yes | yes | yes |
| Quality checks, stage/publish/fence, idempotency keys, retry | yes | yes | yes | yes | yes |
| `ldp maintain` run by hand, JSONL and OpenLineage events | yes | yes | yes | yes | yes |
| `ldp agent` (the code; it needs a control plane) | yes | yes | yes | yes | yes |
| Scheduler, leases, table lanes, supersession fencing across machines | no | daily, 5 pipelines | hourly | 1 min | 1 min |
| Ledger history | local JSONL | 7 days | 90 days | 1 year | 7 years |
| Alerts (Slack, webhook, PagerDuty) | no | Slack | yes | yes | yes |
| Maintenance autopilot and cost per table | no | no | yes | yes | yes |
| External-commit discovery (other engines' snapshots in the ledger) | no | no | yes | yes | yes |
| SSO / SAML+SCIM, RBAC synced to catalog grants, publish approvals, audit export | no | no | OIDC | yes | yes |
| Several data planes, Terraform provider, PII-safe metric policy | no | no | 1 | yes | yes |
| Dedicated cell, PrivateLink, residency, SLA | no | no | no | 99.9% | 99.95% |

**What is paid, and why it is not free elsewhere.** The business review's main finding is that
correctness is free (by our choice) and orchestrators are free. So a team can get our core promise
inside Airflow without paying. We accept that; those teams are the funnel. The paid product is what
no single free component provides:

1. **Coordination across writers and machines.** This means table lanes, lease fencing lists and
   superseded-run handling (§7). It needs a shared, always-on service. The OSS keeps each writer
   correct, but it cannot stop a fleet from wasting work.
2. **The ledger.** It records every commit on every governed table, whichever catalog and engine
   made it, joined to the spec, run, quality verdict, cost and lineage, with retention and audit
   export. Catalogs see commits but not runs. Orchestrators see runs but not commits.
3. **Maintenance with safety guards, and cost per table.** Neither is sold neutrally across
   catalogs.

The demand test in §13 checks exactly these three items, before we build them.

**Conversion guardrail**, taken from the architecture-first proposal. If free-to-paid conversion
drops below 2% of weekly-active cloud orgs, we move the scheduling or history thresholds. We never
move correctness features.

---

## 5. Architecture

### 5.1 Planes

```text
                      CONTROL PLANE  (LDP-hosted; one cell per region; system of record = Postgres)
                 +------------------------------------------------------------------------------------+
 ldp CLI / SDK ->| edge: TLS, OIDC/SAML, scoped API tokens, per-tenant token buckets                  |
 console (SPA) ->| ldp-api (FastAPI, stateless, >= 3 pods)       one binary in v1:                    |
 Airflow/Dagster | /v1 specs pipelines runs tables ledger quality lineage usage query audit           |
   operators --->| /agent/v1 lease heartbeat events results                                           |
                 | scheduler+coordinator: leader = lease row, 1 s tick, deterministic jitter,         |
                 |   SKIP LOCKED claims, table lanes, fence lists, catchup / supersede                |
                 | relay: polls outbox -> notifier (Slack, webhook, PagerDuty) -> metering rollup     |
                 | Postgres primary + sync standby (2nd AZ) + PITR 35 d:                               |
                 |   orgs specs runs run_attempts table_lanes commits(ledger) quality lineage          |
                 |   usage audit outbox                                                               |
                 | ops lake: run_events, usage_events as Iceberg tables written by LDP itself         |
                 +-------------------------------------^----------------------------------------------+
                     outbound-only HTTPS + mTLS :443   |  leases (pinned spec, key, window, fence list),
                     (the agent dials out; no inbound) |  heartbeats, events, results -- METADATA ONLY
DATA PLANE  (customer account = BYOC in year 1; Phase 4 adds an LDP-hosted pooled cell)
+----------------------------------------------------------------------------------------------------------+
| ldp agent  (the OSS wheel; ECS service from one Terraform module, or any VM / K8s pod)                   |
|   local SQLite: 24 h schedule cache, 7-day / 10 GB event buffer, per-table local lanes                    |
|   -> runner task per attempt; image = the same wheel, pinned by digest                                    |
|        extract -> transform -> batch checks -> STAGE on private branch -> table checks -> PUBLISH (1 CAS) |
|        engine router: DuckDB/pyarrow in-process   |   spark/IcebergJob.scala on EMR Serverless or        |
|                                                       the customer's Spark (writes to the branch only)   |
|   -> query worker: DuckDB, read-only, snapshot-pinned, row-capped                                         |
|   -> maintenance worker: expire (with idempotency floor), orphan GC (dry run first),                      |
|        compaction via Spark rewrite_data_files, or S3 Tables native maintenance                          |
| customer catalog: Glue | S3 Tables REST | Polaris | Unity | BigLake | R2 | Postgres (Sql/JdbcCatalog)     |
| customer bucket: data + metadata;  _ldp/quarantine/<run_id>/;  _ldp.* ledger tables                      |
| customer secrets: AWS SM / GCP SM / Vault through workload identity (IRSA / WIF)                         |
+----------------------------------------------------------------------------------------------------------+
  Snowflake, Databricks, Trino, BigQuery and DuckDB read (and may write) the same tables through the same catalog.

LAPTOP / CI  (OSS; no network needed)
  ldp run | plan | query | snapshots | maintain | demo
  SQLite SqlCatalog (.ldp/catalog.db), file:// warehouse, DuckDB, PySpark and the Scala job on the same catalog
  JsonlSink -> .ldp/events.jsonl   (uploaded later with `ldp deploy --import-history`)
```

### 5.2 Components

**Control plane.** Everything below is one deployable in v1 except Postgres and the console.

| Component | Responsibility | State |
|---|---|---|
| Edge | TLS; OIDC, SAML and token validation; maps a workspace to its cell (cached 60 s); per-tenant token buckets | None |
| `ldp-api` | Specs, pipelines, runs, the ledger, quality, lineage, usage, audit; the agent protocol | None; stateless |
| Scheduler | Turns `schedule` and `window` into run rows; deterministic jitter `hash(pipeline_id) % 300 s` unless `exact: true` | Postgres |
| Coordinator | Leases, table lanes, fence lists, attempt counters, timeouts, run-level retries, backfill fan-out, per-tenant fair share (deficit round-robin weighted by plan) | Postgres |
| Relay | Polls `outbox` in commit order; delivers to notifier, metering and the ops-lake writer; at least once | Postgres |
| Metering | Rolls `usage_event` and `governed_table_month` up into billing lines; an hour closes at its watermark plus 24 h | Postgres |
| Console | SPA over `/v1` | None |

The scheduler leader holds a **lease row** (`leader(cell_id, holder, epoch, expires_at)`), not a
session-scoped advisory lock. Leadership survives a connection reset and moves only when the lease
expires.

**Data plane.**

| Component | Responsibility |
|---|---|
| `ldp agent` | Long-polls leases, heartbeats, starts runners, buffers events; outbound only |
| Runner | `ldp run --spec <pinned> --run-id --attempt --key --window --sink http`: the same code path as a local `ldp run` |
| Engine router | `estimate_scan_bytes` sums `file_size_in_bytes` over `plan_files()` after pruning. DuckDB when ≤ 50 GB scanned and ≤ 10 GB out (an assumption to tune); Spark otherwise; a DuckDB out-of-memory is retried on Spark and fed back into the threshold. It runs in the data plane, because manifests hold column bounds, which are data |
| Query worker | DuckDB over `to_arrow_batch_reader()` or `iceberg_scan` through a REST catalog; read-only and pinned to a snapshot |
| Maintenance worker | Lowest priority, preemptible, at most 20% of a data plane's slots |

### 5.3 Build or buy

| Bought | Built |
|---|---|
| RDS Postgres (Multi-AZ) and PgBouncer | `ldp-api`, scheduler and coordinator |
| ECS/Fargate for the agent; EMR Serverless for Spark | The agent and runner (the OSS wheel) |
| An OIDC/SAML/SCIM provider | The commit primitives (OSS) |
| A billing and invoicing provider | Metering rollup |
| Observability SaaS | Maintenance policies |
| Compliance automation | Console |
| AWS/GCP secret managers and KMS | Terraform module |
| Apache Polaris, hosted in Phase 4 only; **we do not write a catalog server** | |

### 5.4 Request path: run a pipeline (scheduled, cloud)

1. **Trigger.**
   - The scheduler computes window `W` from `schedule` and `window`.
   - It inserts the run with `K = sha256(pipeline_id ‖ target_table_uuid ‖ W.start ‖ W.end)`,
     using `ON CONFLICT (idempotency_key) DO NOTHING`, and pins the spec version as an attribute.
   - Duplicate cron fires, retried API calls and double clicks all collapse into one run.
2. **Lease.** The agent calls `POST /agent/v1/lease {capacity}`. In one Postgres transaction the
   coordinator:
   - picks a queued run whose table lane is free;
   - increments `attempt` and inserts a `run_attempt` row;
   - grants the lane;
   - returns
     `{run_id, attempt, K, spec, W, search_since_ms, fence_branches[], secret_refs[], image_digest, lease_ttl=60s}`.
3. **Fence.** The runner removes every branch in `fence_branches`, in one catalog commit. The list
   holds earlier attempts of this run and any cancelled or superseded run of the same pipeline.
4. **Deduplicate.**
   - Load the table and call `find_commit(K, since=search_since_ms)` on `main`.
   - If the key is found, finish as `skipped_duplicate`, recording the existing snapshot id.
5. **Extract, transform, validate.**
   - Batch checks run on the Arrow batch.
   - A failed blocking check ends the run as `blocked_quality`.
   - Failing rows go to `_ldp/quarantine/<run_id>/` in the customer's bucket.
6. **Stage.**
   - Create a branch `ldp_r<run_hex>_a<attempt>_<n>` at `base = main` and write to it with
     `snapshot_properties = {ldp.*}`.
   - Spark jobs write to the branch too, and only to the branch.
7. **Audit.** Table-level checks (uniqueness across the table, table row count) run on a scan pinned
   to the staged snapshot.
8. **Publish.**
   - One `catalog.commit_table` call with the requirements `{table uuid, main == base, branch == staged, schema id}`
     and the updates `{main -> staged, remove branch}`.
   - On conflict, follow the retry loop in §7.
9. **Report.**
   - The runner posts the result with its attempt number. Only the current attempt is accepted;
     this is bookkeeping, since the real fence already held at the catalog.
   - In the same transaction, the API writes the run transition, the ledger row, quality results,
     lineage edges, usage and outbox rows.
10. **React.** The relay delivers alerts and webhooks. The agent batches `_ldp.*` ledger rows into
    the customer's catalog every 5 minutes.

The heartbeat interval is 15 s. A runner that cannot renew within 45 s fences itself: it stops
before publishing.

### 5.5 Request path: query a table

1. `ldp query` or `POST /v1/query {sql, params, pins, timeout_s ≤ 60}` reaches the API. The API
   checks RBAC and routes the query to the workspace's agent over its open long-poll.
2. The query worker loads the tables through the customer's catalog with read-only credentials and
   registers them in DuckDB. It uses `to_arrow_batch_reader()` streams, or `iceberg_scan` when the
   catalog speaks REST. Tables are pinned to the requested snapshot ids, or to `main` at start.
3. Arrow IPC streams back through the agent tunnel with a cap of 100k rows or 50 MB. The response
   carries the snapshot ids that were read, so the result can be reproduced.
4. The control plane logs only the SQL hash, the bytes scanned and the snapshot ids. It never
   stores the results.

External engines skip all of this and query the catalog directly.

### 5.6 One library, laptop and cloud

| Concern | Laptop | Cloud runner |
|---|---|---|
| Code | `local-data-platform` wheel | The same wheel, pinned by image digest per run |
| Entry point | `ldp run cfg.json` | `ldp run --spec <pinned> --run-id --attempt --key --window` |
| Catalog | `create_catalog({"type": "local"})`, SQLite `SqlCatalog` | `create_catalog(workspace catalog_connection)` |
| Write protocol | `direct` by default; `staged` with `--idempotency-key` | Always `staged` |
| Concurrency | Local file lock around direct overwrite and upsert | Table lanes plus the catalog CAS |
| Events | `JsonlSink(".ldp/events.jsonl")` | `HttpSink` to the agent, then the control plane |
| Spark | `SparkEngine` / `ScalaSparkJob` via `spark_catalog_conf` | The same job on EMR Serverless or the customer's Spark |

A pipeline therefore behaves the same everywhere. The only differences are the catalog binding, the
event sink and whether a `CommitContext` is present.

### 5.7 Deployment modes over time

| Mode | When | Who | Isolation |
|---|---|---|---|
| Local | Now | Everyone | One machine |
| Self-hosted OSS | Phase 1 | Teams that won't pay | A Postgres `SqlCatalog` or any REST catalog; no coordinator |
| **BYOC** | Phase 2 (only paid mode until Phase 4) | All paid tiers | Customer account; the control plane holds metadata only |
| Pooled | Phase 4 | Team tier, for customers with no cloud account to spare | Hosted Polaris per workspace, gVisor runners, prefix-scoped STS |
| Silo / dedicated cell | Phase 4 | Enterprise | Dedicated capacity or a whole cell |

---

## 6. Data and metadata model

### 6.1 Hierarchy and identifiers

- **Hierarchy:** org → workspace (`dev`, `staging` or `prod`, each with its own warehouse and
  credentials) → catalog connection → Iceberg namespace → table.
- **Identifiers, locally:** today's `f"{namespace}.{name}"`.
- **Identifiers, in the cloud:**
  - Tables keep the customer's own namespaces.
  - LDP's own tables live under `_ldp`.
  - The control plane keys tables by `table_uuid`, never by name, so renames are safe.

### 6.2 Control-plane schema (Postgres, one per cell)

Every row carries `org_id`. Row-level security runs off `SET app.org_id` per transaction, and the
API role has no `BYPASSRLS`. Ids are uuid7.

**Tenancy and access**

- `org(id, slug, plan, cell_id, home_region, billing_account_id, created_at)`
- `workspace(id, org_id, name, env)`
- `principal(id, org_id, kind: user|service|agent, oidc_sub, scim_id)`
- `role_binding(principal_id, resource, role, conditions jsonb)`, with the roles
  `org_admin`, `workspace_admin`, `developer`, `deployer`, `viewer` and `runner`
- `api_token(id, principal_id, hash, scopes, expires_at ≤ 90 d)`
- `data_plane(id, org_id, kind: byoc|pooled|silo, cloud, region, agent_version, cert_fingerprint, capabilities jsonb, last_heartbeat_at)`

**Catalogs and secrets**

- `catalog_connection(id, workspace_id, type, uri, warehouse, catalog_name, secret_ref_id, properties jsonb)`
- `secret_ref(id, workspace_id, provider: aws-sm|gcp-sm|vault|k8s, ref)`: a pointer, never a value.
  This generalises the `GCPCredentials` pattern, which keeps a key path and never the key.

**Pipelines and specs**

- `pipeline(id, workspace_id, identifier, owner, target_table_id, write_mode, schedule_cron, window, jitter_s, catchup bool, sla_freshness_min, current_spec_version, enabled)`.
  This is where the 4W1H fields finally get used: `owner` comes from `who`, `schedule_cron` from
  `when`, and batch or stream from `how`.
- `spec_version(pipeline_id, version, api_version, spec jsonb, spec_hash, ldp_version, git_sha, created_by, created_at)`:
  immutable.

**Runs**

- `run(id, pipeline_id, spec_version, trigger, logical_window tstzrange, idempotency_key UNIQUE, state, attempt, first_attempt_at, engine, rows_read, rows_written, rows_before, rows_after, published_snapshot_id, duration_s, lcu_seconds, error_class, error_message_redacted, created_at)`.
  Partitioned by month.
- `run_attempt(run_id, attempt, lease_owner, lease_expires_at, branch, base_snapshot_id, staged_snapshot_id, image_digest, started_at, ended_at, outcome)`.
- `run_transition(run_id, seq, from_state, to_state, attempt, at, detail jsonb)`: append-only.
- `table_lane(table_id, mode: exclusive|shared, holders jsonb [{run_id, attempt, expires_at}], max_shared = 4)`.
- **Run states:**
  - The main path: `queued → leased → extracting → validating → staged → audited → published`.
  - End states: `skipped_duplicate`, `blocked_quality`, `retry_wait`, `failed`, `cancelled`,
    `superseded`.
  - `blocked_quality` is a first-class outcome, not a failure. It never counts against a platform
    SLO.

**The ledger and table metadata**

- `iceberg_table(id, workspace_id, catalog_connection_id, namespace text[], name, table_uuid, format_version, partition_spec jsonb, current_schema jsonb, owner, pii_field_ids int[], policies jsonb, managed bool)`
- `commit(table_id, snapshot_id, parent_id, sequence_number, metadata_location, operation, run_id NULL, attempt NULL, idempotency_key NULL, spec_hash NULL, engine, audited bool, discovered bool, added_records, deleted_records, added_bytes, total_records, total_files_size, committed_at)`,
  with primary key `(table_id, snapshot_id)`.
  - `run_id IS NULL` means another engine made the commit. We find those commits by polling
    snapshots and mark them `discovered = true, audited = false`.
  - There is an index on `(table_id, idempotency_key)`. It is deliberately not unique: the ledger
    must record reality, and the nightly auditor raises a SEV1 on any duplicate.
- `quality_result(run_id, attempt, table_id, snapshot_id NULL, check_name, check_type, blocking, passed, failing_rows, metrics jsonb, details_redacted, evaluated_at)`:
  loaded from `QualityReport.to_dict()` and `CheckResult`.
- `lineage_edge(run_id, input_ref, input_snapshot_id NULL, output_table_id, output_snapshot_id, column_map jsonb NULL)`.
  `input_ref` is a `table_uuid` or an external URI such as `bigquery://project/job` or
  `s3://…/file.csv#etag`. It is emitted as OpenLineage too.
- `release(id, workspace_id, name, run_id, created_at)` with
  `release_member(release_id, table_id, snapshot_id, tag)`, for publishes across several tables
  (§7.12).

**Usage, billing and audit**

- `usage_event(id uuid PK, org_id, workspace_id, run_id NULL, meter, quantity, dims jsonb, window_start, window_end, received_at)`.
  Meters: `runs`, `lcu_seconds_hosted`, `storage_gb_hours_hosted`, `query_bytes_scanned` and
  `llm_tokens`.
- `usage_hourly(org_id, workspace_id, meter, hour, quantity)`: an idempotent upsert keyed on
  `usage_event.id`.
- `governed_table_month(org_id, table_id, month, reason: published|policy|contract)`: the unit we
  bill.
- `audit_log(seq, org_id, actor, action, resource, request_id, ip, at, prev_hash, hash)`:
  hash-chained and append-only.
- `outbox(id bigserial, aggregate, aggregate_id, seq, event_type, schema_version, payload jsonb, created_at, relayed_at)`:
  written in the same transaction as the state change it describes, and deleted 24 h after it is
  relayed.

### 6.3 Iceberg-side metadata

Every LDP publish writes these snapshot-summary keys through `snapshot_properties`, which pyiceberg
0.12 accepts on `append`, `overwrite`, `dynamic_partition_overwrite`, `upsert` and `delete`, or
through Spark's `snapshot-property.*` write option:

| Key | Value |
|---|---|
| `ldp.run-id` | The run id (uuid7 hex) |
| `ldp.attempt` | The attempt number |
| `ldp.idempotency-key` | `K` |
| `ldp.spec-hash` | Hash of the pinned spec |
| `ldp.logical-window` | ISO interval |
| `ldp.quality` | `passed` or `warn` |
| `ldp.source.<name>` | Source position: Kafka offsets JSON, Postgres LSN, file ETag |

**Table properties on LDP-created tables:**

- `ldp.managed=true`, `ldp.owner`, `ldp.pii.<field-id>`, `ldp.maintenance.policy`;
- `ldp.idempotency.horizon-days=7`;
- `write.metadata.previous-versions-max=50`, `write.metadata.delete-after-commit.enabled=true`.

We deliberately **do not** set `commit.retry.num-retries`. It is a table property that Iceberg Java
reads too, so changing it would silently change the behaviour of the customer's own Spark writers
(see §15).

**Branch names:** `ldp_r<32-hex run id>_a<attempt>_<n>`. They avoid `/` and `-`, so Spark's
`branch_<name>` identifiers need no quoting.

**Bootstrap:** pyiceberg refuses to create a branch on a table with no snapshots. So when LDP creates
a table, it appends an empty snapshot tagged `ldp.init=true`. This was verified to produce a
snapshot on 0.12.0. Every later write has a `base` to branch from.

### 6.4 The `_ldp` namespace in the customer's catalog

The agent writes `_ldp.runs`, `_ldp.commits`, `_ldp.quality_results`, `_ldp.lineage` and, on
Business and above, `_ldp.audit`. Each is day-partitioned, appended in batches every 5 minutes and
compacted daily.

The customer can query its own history with SQL in any engine, and keeps it if it leaves. These
tables are also one of the two sources the control plane can be rebuilt from (§9.4).

### 6.5 Event envelope, shared by laptop and cloud

```python
@dataclass(frozen=True)
class RunEvent:
    event_id: str          # uuid7
    type: str              # run.started | run.extracted | quality.evaluated | run.staged |
                           # run.published | run.skipped_duplicate | run.blocked_quality |
                           # run.failed | run.finished | table.maintained
    schema_version: int
    ts: datetime
    run_id: str
    attempt: int
    seq: int               # monotonically increasing within (run_id, attempt)
    table_uuid: str | None
    payload: Mapping[str, Any]   # WriteResult / QualityReport.to_dict() / counts; allowlisted (§10.2)
```

- Consumers apply each event at most once per `(run_id, attempt, seq)` and ignore unknown fields.
- Each event type has a JSON Schema, and CI checks that changes are backward compatible.

### 6.6 Mapping from today's objects

| Today (on disk or in the contract) | Becomes |
|---|---|
| `Config` | `spec_version.spec`, with `spec_hash` |
| `Config.who` / `when` / `how` | `pipeline.owner` / `schedule_cron` / batch or stream |
| `PipelineResult` | a `run` row |
| `WriteResult` | `run` counts plus a `commit` row; `rows_before` and `rows_after` come from the parent's and the published snapshot's `total-records` |
| `QualityReport.to_dict()` | `quality_result` rows, PII-redacted |
| `DataQualityError` | `blocked_quality`, HTTP 422 |
| `GCPCredentials` | the `secret_ref` pattern |
| `LocalIcebergCatalog` | `catalog.type = local` |

### 6.7 Retention

| Data | Kept for |
|---|---|
| Hot run, quality and ledger rows in Postgres | 90 days, dropped by partition |
| Ledger history in the ops lake and `_ldp.*` | Team 90 days, Business 1 year, Enterprise 7 years |
| Audit log | 7 years |
| Usage | 7 years, for billing disputes |
| Rejected staged branches | 7 days, then removed by the maintenance worker |

---

## 7. Consistency, concurrency, idempotency

### 7.1 Ground truth (verified on pyiceberg 0.12.0)

1. **`SqlCatalog.commit_table`:**
   - It loads the current metadata and validates **every** requirement against it
     (`Catalog._update_and_stage_table`).
   - It writes the new `metadata.json`.
   - It runs `UPDATE … SET metadata_location = :new WHERE … AND metadata_location = :current`, and
     raises `CommitFailedException` if no row changed.
   - Java `JdbcCatalog` uses the same `iceberg_tables` layout (`jdbc.schema-version=V1`, the same
     `catalog_name`), which is how `spark/IcebergJob.scala` already shares the SQLite file.
   - For Glue, the CAS is on the Glue `VersionId`. For REST, the server validates the requirements.
2. **`Transaction.commit_transaction` retries blindly.** It retries `CommitFailedException` up to
   `commit.retry.num-retries` times (default 4, total timeout 30 minutes). Each time it rebuilds the
   snapshot producers and rebases them onto the new head. It stops early only if its own snapshot id
   is already present. It never looks at `ldp.idempotency-key`. The retry runs only when the
   transaction has snapshot producers; a transaction that only changes refs is not retried.
3. **`Transaction._stage` drops repeated requirement types.** It keeps only the first requirement of
   each type. A transaction that asserts both `main` and a branch silently sends only the first
   `AssertRefSnapshotId`. Checked: staging `set_current_snapshot` plus `remove_branch` left one
   requirement, on `main`.
4. **Direct `commit_table` calls keep every requirement.** Calling `Catalog.commit_table(table, requirements, updates)`
   directly, with `AssertRefSnapshotId` on both `main` and the branch, keeps both. Checked:
   - Two runs staged on branches from the same base.
   - Run A published.
   - Run B's publish failed with "branch main has changed".
   - A run whose branch had been removed failed with "branch or tag … is missing".
   - A's publish removed A's branch in the same commit.

Fact 3 is a pyiceberg bug, and we will fix it upstream (§12). Until then, LDP publishes through
`Catalog.commit_table` with requirement and update objects from `pyiceberg.table.update`, and pins
pyiceberg in the CI matrix.

### 7.2 Invariants

| # | Invariant | How it holds |
|---|---|---|
| I1 | **No unaudited publish.** Every snapshot on `main` of an LDP-managed table that carries `ldp.run-id` belongs to an attempt whose blocking checks passed. A snapshot without `ldp.run-id` is an external write, recorded as `audited = false` | Checks run before the publish; the publish is the only way LDP moves `main` |
| I2 | **At most one effect per key.** At most one snapshot reachable from `main` carries a given `ldp.idempotency-key` | `find_commit` on `base`, then a publish asserting `main == base` (argument in §7.5) |
| I3 | **No publish after fencing.** Once an attempt's successor has committed its fence, the older attempt cannot publish | The publish asserts `branch == staged`; the fence removed that branch |
| I4 | **Per-table linearisability.** Publishes on `main` are totally ordered by the catalog CAS, and readers pin immutable snapshots | The catalog |
| I5 | **No dual writes in the control plane.** Every state change and its outbox row commit in one Postgres transaction | Transactional outbox |

The **nightly auditor**, per cell, walks the `main` ancestry of every table committed that day. It
checks I1 and I2 against the snapshot summaries, the `commit` rows and `quality_result`. It also
reconciles ledger rows whose results were lost. Any violation is a SEV1 with a public RCA.

### 7.3 The idempotency key

`K = sha256(pipeline_id ‖ target_table_uuid ‖ window_start ‖ window_end)`.

**Why the spec hash is not in the key.** All three losing designs keyed on `spec_hash`. Redeploying
a spec and re-running a window then produced a new key, and append pipelines duplicated rows.

**What happens instead when a window is re-run under a new spec:**

- It is an explicit replace: `ldp backfill --replace`, or a spec whose write mode is a window
  overwrite.
- A new key version is created only by `--replace`, as
  `K' = sha256(K ‖ "replace" ‖ spec_version)`, and it writes with an overwrite filter scoped to the
  window. Re-running therefore replaces the window; it never adds to it.
- The spec version is recorded on the run and in `ldp.spec-hash`.

**Keys for other triggers:**

- Manual and API runs use the client's `Idempotency-Key` header when there is one; otherwise they
  get a fresh window.
- Micro-batch stream runs use `K = sha256(pipeline_id ‖ table_uuid ‖ source positions)`.

**Default write modes by pipeline kind.** The spec validator warns on a scheduled `append` with
`window: none`.

| Pipeline kind | Default write | Effect of a re-run |
|---|---|---|
| Scheduled, partitioned by time | `dynamic_partition_overwrite` (or `overwrite` with a filter on the window) | Replaces the window |
| Keyed entities | `upsert` on `join_cols` | Converges |
| Event streams | `append` with a key per micro-batch from source offsets | A no-op by key |

### 7.4 The publish protocol

```python
# src/local_data_platform/format/iceberg/commit.py  (Phase 1)
from pyiceberg.table.update import (AssertTableUUID, AssertRefSnapshotId, AssertCurrentSchemaId,
                                    SetSnapshotRefUpdate, RemoveSnapshotRefUpdate)

def publish(catalog, table, staged: StagedWrite) -> Snapshot:
    """Fast-forward main to the staged snapshot in ONE catalog CAS.

    Goes straight to Catalog.commit_table, so pyiceberg's Transaction retry (which rebases blindly)
    and its one-requirement-per-type rule (which would drop the branch assertion) are both bypassed.
    """
    catalog.commit_table(
        table,
        requirements=(
            AssertTableUUID(uuid=table.metadata.table_uuid),
            AssertRefSnapshotId(ref="main", snapshot_id=staged.base_snapshot_id),
            AssertRefSnapshotId(ref=staged.branch, snapshot_id=staged.staged_snapshot_id),
            AssertCurrentSchemaId(current_schema_id=staged.schema_id),
        ),
        updates=(
            SetSnapshotRefUpdate(ref_name="main", type="branch", snapshot_id=staged.staged_snapshot_id),
            RemoveSnapshotRefUpdate(ref_name=staged.branch),
        ),
    )
    return catalog.load_table(table.name()).snapshot_by_id(staged.staged_snapshot_id)
```

The write loop (`write_once`) for one attempt:

1. `fence(fence_branches)`: one commit that removes the listed branches. If a branch is already
   gone, the fence is complete.
2. Load `table`. Set `base = main head`, calling `ensure_base` first so a new table gets its `ldp.init`
   snapshot.
3. Call `find_commit(K, since=search_since_ms, max_depth=500)` over `base`'s ancestry.
   - If it finds the key, return `skipped_duplicate`.
   - If the ancestry reaches `max_depth` before the time bound, raise `CommitSearchExhausted`.
     This fails closed: the attempt does not publish.
4. `stage(...)`:
   - Create branch `b` at `base`.
   - Write to `b`, with the additive schema union in the same transaction as the write.
   - pyiceberg's inner retry may rebase *this* commit, which is harmless: only this attempt writes
     to `b`.
5. Run the table-level checks on the staged snapshot. If one fails, mark the attempt
   `blocked_quality`, keep the branch for 7 days, and stop.
6. `publish(...)`. On `CommitFailedException`:
   - Reload the table.
   - Call `find_commit(K)` again. If the key is found, stop; another attempt with this key won.
   - Otherwise `main` has moved. Apply the conflict matrix (§7.6), stage on `b_{n+1}` from the new
     `main`, and go back to step 5.
   - Back off with full jitter: base 200 ms, cap 8 s, at most 6 publish attempts or 300 s.
7. When publish attempts are exhausted, raise `CommitConflict(retriable=True)`. The coordinator
   retries the whole run as attempt `a+1`, at most 3 times, then marks it `failed`.
8. Build `WriteResult` from the snapshot summaries:
   - `rows_before`: the `total-records` of the parent (`base`);
   - `rows_after`: the `total-records` of the published snapshot;
   - `rows_written`: `added-records`, or upsert updated plus inserted.

### 7.5 Why this gives at most one effect per key

The claim: suppose attempt X publishes key `K` successfully. Then no other attempt Y can also leave
a snapshot carrying `K` reachable from `main`.

1. X's publish succeeded, so `main == base_X` at its CAS. X's `find_commit` ran over `base_X`'s
   ancestry and did not find `K`.
2. Suppose Y published `K` before X. Then Y's snapshot is an ancestor of every later `main`,
   including `base_X`. X would have found `K`, a contradiction.
3. Suppose Y publishes after X. Then `base_Y` must equal `main` at Y's CAS, which descends from X's
   snapshot. So Y's `find_commit`, run over `base_Y` in step 3 or step 6, finds `K`, and Y stops.

The argument depends on two things:

- **Snapshot history is kept long enough.** Expiry never removes snapshots younger than the
  idempotency horizon: `ldp.idempotency.horizon-days=7`, which is longer than the maximum run-level
  retry window.
- **`find_commit` fails closed** instead of guessing.

Pyiceberg's rebasing retry never moves `main` in this protocol. It only ever touches the private
branch.

A deliberate `ldp rollback` that moves `main` to before X's snapshot makes `K` unreachable, so the
window can be published again. That is the intended meaning of a rollback.

### 7.6 Conflict matrix: `main` moved from `base` to `base'` before our publish

| Our operation | Resolution | Checks |
|---|---|---|
| `append` | Re-stage from `base'` by calling `add_files` with the data files we already wrote. There are no delete files for an append, and no data is rewritten | Batch checks still hold; re-run the table-level checks |
| `dynamic_partition_overwrite` / window `overwrite` | Re-apply the same operation from `base'`. The last writer wins for this window, which is what partition overwrite means. Two different pipelines writing the same partitions is flagged as a config error | Re-run the table-level checks |
| Full `overwrite` | Re-apply from `base'` | Re-run the table-level checks |
| `upsert` | **Recompute** from `base'`, re-reading the keys. Never rebase | Re-run all checks |
| Schema changed concurrently | `AssertCurrentSchemaId` fails. Reload, union again, re-stage | Re-run all checks |

### 7.7 Table lanes: efficiency, not correctness

The CAS alone keeps writes correct. Lanes stop runs from wasting work on retries.

| Lane mode | Operations | Holders at once |
|---|---|---|
| `exclusive` | `overwrite`, `upsert`, maintenance commits | 1 |
| `shared` | `append` | Up to 4 |

- A lane is a Postgres row. It is granted in the same transaction as the lease, renewed by the
  heartbeat, and expires with the lease.
- Because it is a row and not a session lock, it survives scheduler failover and is visible to every
  agent.
- When a table sees more than 2 publish conflicts per second, its lane switches to `exclusive`.
- For append-only micro-batch pipelines, the next holder publishes the pending windows in one
  snapshot.
- If the control plane is down, each agent serialises the tables it owns with a local lane. That is
  only less efficient, never less safe.

### 7.8 Fencing zombies and superseded runs

Three cases, and what stops each:

- **An older attempt of the same run** (a GC pause or a partition past its lease):
  - It fences itself at 45 s without a renewal.
  - The successor's first catalog commit removes the older attempt's branch, so the older attempt's
    publish fails its `branch == staged` requirement (I3).
  - If the old attempt published before that fence, it published the same key, and the successor's
    `find_commit` turns the successor into a no-op (I2).
- **A cancelled or superseded run** (the user cancelled R1, or `catchup: false` superseded it with
  R2):
  - R2's lease carries R1's branches in `fence_branches`.
  - After R2's fence, R1 cannot publish.
  - If R1 published before that fence, then either R2 started from a base that already contains
    R1's snapshot, or R2's publish fails `main == base` and R2 re-plans on top of R1. Either way
    R2's overwrite or upsert lands after R1's, in serial order. No update is lost.
  - The worst case is that "a cancelled run landed before its successor". The ledger shows this, and
    the successor corrects it.
- **Result reporting:** a result posted with a stale attempt gets HTTP 409 and is logged. The
  snapshot, if one landed, is picked up by the successor's `find_commit` or by the nightly auditor.

### 7.9 Direct mode (local, and the contract's `put`)

`Iceberg.put(df, mode)` keeps the v0.1.1 semantics, with three fixes:

1. Schema union and the data write run in **one** `table.transaction()`, which is one CAS. This
   fixes the separate `update_schema()` at line 545.
2. `rows_before` and `rows_after` come from snapshot summaries, not from `_row_count` (lines 448 and
   467).
3. On `local` catalogs, direct `overwrite` and `upsert` take an exclusive file lock,
   `.ldp/locks/<identifier>.lock`: `fcntl` on POSIX, `msvcrt` on Windows. This closes the
   upsert race between processes on one laptop without any service.

`Iceberg.put(df, mode, *, commit=CommitContext(...))` switches to the staged protocol, so
`ldp run --idempotency-key` behaves locally exactly as it does in the cloud.

Direct mode on a shared remote catalog is documented as a single-writer mode.

### 7.10 What "exactly once" means, per path

| Path | Guarantee |
|---|---|
| Run creation | Exactly once per `K` (UNIQUE key) |
| Run execution | At least once (leases expire; attempts are retried) |
| Effect on `main` | **At most once per `K`** (I2); combined with at-least-once execution, effectively once |
| Control-plane events and usage | At least once delivery, applied at most once on `(run_id, attempt, seq)` or `event_id` |
| Streaming, source to table | Effectively once per source position (§7.11) |
| Producer to source (for example an app sending to Kafka) | **Not** covered. Duplicates from the producer are removed only if events carry an `event_id` (a 24 h dedupe window during compaction, or upsert tables). We say so plainly and never market end-to-end exactly-once |
| Local direct mode | The contract's semantics: appending twice gives duplicates, as the demo shows |

### 7.11 Streaming and CDC

This covers #89 and the CDC template. A stream is a pipeline scheduled every 1–5 minutes, not a
separate service.

- Each micro-batch run reads from the source positions recorded in `main`'s latest
  `ldp.source.<name>` summary. There is no side store for positions.
- It writes data and the new positions in the **same** snapshot.
- Before publishing, it checks **continuity**: the end positions in `base` must equal this batch's
  start positions, partition by partition.
  - A zombie committer left behind by a consumer-group rebalance fails this check, and it also fails
    `main == base`.
  - This is how we handle "several committers per table". Kafka consumer groups assign partitions,
    not tables, so partition ownership alone cannot give one committer per table.
- The fastest supported schedule is 1 minute, so a table gets about 3 catalog commits per minute
  (§8.3).
  Faster ingestion belongs to Tableflow, Firehose or Estuary; LDP governs the table they land in.

### 7.12 Several tables at once

pyiceberg has no transaction that spans tables. For a gold layer that must appear all at once
(0.1.4):

1. Stage every table.
2. Publish each one.
3. Tag each with `ldp-rel-<run_id>` and record a `release` row.

Consumers pin a release (`pins: {release: …}`) rather than `main`. Where the catalog supports the
REST `transactions/commit` endpoint, the coordinator uses it and the release is atomic.

### 7.13 Schema evolution

- **Automatic:**
  - additive union by name, which is today's behaviour;
  - Iceberg-legal widening: `int` to `long`, `float` to `double`, higher decimal precision;
  - renames, which are safe because Iceberg resolves columns by field id.
- **Everything else needs a new spec version.** Drops, narrowing, required-ness and partition-spec
  changes require `schema_change.allow`. They are shown by `ldp plan` and applied by
  `ldp plan --apply` in a separate reviewed commit, never inside a run.
  - Every downstream owner found through lineage is notified.
  - A downstream pipeline whose `schema` check names an affected column is blocked. This is how
    contracts propagate.
  - Today a changed partition spec only produces a warning (`_warn_if_spec_differs`).
- **Staging and schemas.** Iceberg schemas are table-wide, not per branch, so the additive union in a
  staged write is visible from stage time. We accept that for nullable additive columns only,
  because they are invisible to existing readers. The publish asserts the schema id.
- **Format version.**
  - v2 is the default.
  - v3 is opt-in per table, and allowed only once every engine seen writing that table in the ledger
    supports v3 writes.
  - Snowflake made v3 GA on May 7, 2026
    ([source](https://docs.snowflake.com/en/release-notes/2026/other/2026-05-07-iceberg-v3-ga));
    Databricks did so in May 2026
    ([source](https://www.databricks.com/blog/unity-catalog-and-next-era-apache-icebergtm)).
- **Specs.** `ldp/v1` is readable forever. `v2` may only add fields, and ships a converter.

### 7.14 Time

- `logical_window` comes from the scheduler, never from a worker's clock.
- `Freshness(now=...)`, which the contract lets us inject, receives `now = window.end`, so a backfill
  evaluates exactly what the original run did.
- `search_since_ms` is the first attempt's start minus 10 minutes of allowed clock skew. It is **not**
  the current lease's start, which would miss a commit an earlier attempt made before that lease
  began.
- Commit order always comes from the catalog, never from client timestamps.

### 7.15 How we test it

| Test | Scope | When |
|---|---|---|
| `tests/concurrency/test_upsert_race.py` | The 4-writer overlapping-upsert repro. It must fail on raw pyiceberg upserts and pass through `write_once` | Phase 1 |
| `tests/concurrency/test_publish_fence.py` | Stale base, removed branch, zombie attempt, superseded run | Phase 1 |
| `tests/concurrency/test_multi_writer.py` | 8 pyiceberg writers and 1 Spark writer against one table for 60 minutes, on Postgres `SqlCatalog`/`JdbcCatalog` plus MinIO and on a Polaris container, with injected `CommitFailedException` and `kill -9`. Invariant: final row count = the sum over distinct published keys, with 0 duplicate keys in the history | Phase 1 exit gate for #88; nightly after that |
| `tests/interop/` | Python writes, the Scala job stages on a branch, Python publishes, DuckDB time-travels | Phase 1 |
| A small TLA+ or P model | Lease, fence, `find_commit`, publish: checks I2 and I3 under message loss and reordering | Phase 1 (about 2 weeks of founder time) |
| Fault injection | Toxiproxy partitions between agent and control plane, Postgres failover, runner kills between write and publish and between publish and acknowledgement | Nightly from Phase 2 |

CI runs Postgres and MinIO as GitHub Actions service containers.

---

## 8. Scale: back-of-envelope numbers and what breaks first

### 8.1 Assumptions at about $100M ARR (end of 2032)

These are planning assumptions, not market data.

| Quantity | Value | Basis |
|---|---|---|
| Paying orgs | 2,250: 1,400 Team, 700 Business, 150 Enterprise | The ARR plan in §11 |
| Free cloud orgs | about 20,000 | Roughly the 10:1 ratio dbt had at $100M ARR: 50,000 weekly-active teams to 5,000+ Cloud customers ([source](https://www.getdbt.com/blog/dbt-labs-100m-arr-milestone)) |
| Governed tables | Team 1,400 × 400 = 0.56M; Business 700 × 3,600 = 2.52M; Enterprise 150 × 12,000 = 1.8M; free 20,000 × 15 = 0.3M; **about 5.2M** | ICP sizing |
| LDP pipelines | 0.25 per governed table, **about 1.3M** (the rest are written by dbt, external engines, or fan-out) | Assumption |
| Schedule mix | 70% daily, 25% hourly, 5% every 15 minutes: 0.7 + 6 + 4.8 = **11.5 runs per pipeline per day** | Assumption |
| Average run | 4 minutes, about 1.5 vCPU | Assumption |

### 8.2 Control plane

| Quantity | Fleet | Per cell (8 shared cells plus dedicated) |
|---|---|---|
| Runs per day | 1.3M × 11.5 ≈ **15M/day ≈ 174/s average** | about 20/s |
| Run starts at peak | Top-of-hour alignment would be about 10×; jitter over 300 s caps it near 3×: **about 520/s** | about 65/s |
| Concurrent runs | 174/s × 240 s ≈ **42k** | about 5k |
| Postgres row writes (about 30 per run: lease, 16 heartbeats, 3 event batches, result, ledger, quality, outbox) | **about 5.2k/s average, about 15k/s peak** | about 2k/s peak |
| Agent long-polls (about 23k data planes, most idle, 25 s poll) | about 940/s, as held connections | about 120/s |
| API, CLI and console reads (30k daily users × 200 requests, 10× peak) | about 70/s average, 700/s peak | about 90/s peak |
| Hot storage (runs 15 GB, ledger 7.5 GB, quality 18 GB per day) | about 40 GB/day; 90 days ≈ **3.6 TB** | about 400 GB |
| Run events (25 per run, about 0.3 KB each), sent to the ops lake, not OLTP | 375M/day ≈ 112 GB/day raw, about 20 GB/day Parquet, about 7 TB/year | — |

A cell's peak (65 claims/s, about 2k row writes/s) is roughly an order of magnitude below what a
16-vCPU Postgres primary sustains for short transactions. That is an assumption to confirm with a
load test in Phase 2. Cells exist to limit blast radius, not to add throughput. The queue moves to
SQS per cell only if a cell sustains more than 1k claims/s. There is no Kafka anywhere in this plan.

### 8.3 Data plane: the flagship event workload (#89)

This is a 400M-user consumer app emitting 50 events per user per day, about 500 B each (an
assumption).

| Quantity | Value |
|---|---|
| Events | 20B/day ≈ 230k/s average |
| Raw volume | about 10 TB/day; about 1.5 TB/day as zstd Parquet, assuming 6–7× compression |
| Commits | 5-minute micro-batches, so 288 batches per day per table. Each batch is 3 catalog commits (create branch, staged write, publish) but only 1 snapshot, because the staged snapshot is the one published. That is about 864 commits per day per table, trivial for any catalog. Folding branch creation into the staged write is an optimisation still to verify |
| Small files | 64 writer tasks × 288 batches ≈ 18k files/day; hourly compaction of the closed hour to 512 MB brings it to about 3k/day |
| Compaction compute | About 1 Spark vCPU-hour per 20–40 GB rewritten (an assumption to measure): about 40–75 vCPU-hours/day, in the customer's account |
| Compaction at S3 Tables prices ($0.005/GB processed plus $0.002 per 1K objects, [source](https://aws.amazon.com/s3/pricing/)) | 1,500 GB × $0.005 + 18k × $0.002/1K ≈ **$7.5/day ≈ $2.8k/year** per table. Maintenance is a cost-avoidance feature, not a profit centre, so on S3 Tables we defer to native maintenance |
| Metadata | 288 snapshots/day, kept for the 7-day idempotency floor (§7.5), so about 2,000 live snapshots. At about 1 KB per snapshot, `metadata.json` stays near 2 MB, or about 0.4 MB gzipped, with `previous-versions-max=50` |
| Interactive queries | One hour-partition is about 60 GB, above the DuckDB threshold; funnels over a day of one event type (≤ 50 GB after pruning) stay on DuckDB |

Hosted compute arrives in Phase 4. Team hosted usage of about $3.4k/year per org is 13.6k LCU-hours
per org and about 19M LCU-hours/year across the fleet. That is about 2.2k LCUs busy on average and
about 5k at peak, spread over the pooled cells.

### 8.4 How each tier scales, and what breaks first

| Tier | Scales by | Breaks first | Mitigation |
|---|---|---|---|
| Laptop | One process, SQLite | (1) DuckDB `to_arrow()` loads the whole scan into RAM (duckdb line 150); (2) SQLite's database-wide write lock under several processes | Stream with `to_arrow_batch_reader()`; one project catalog `.ldp/catalog.db` with a shim for `<name>_catalog.db`; the local file lock |
| Self-hosted OSS (Postgres `SqlCatalog` or REST, S3) | More writers | (1) Blind upsert rebase under concurrency (§2.1); (2) CAS thrash above about 2 commits/s on one table; (3) no shared coordinator | Staged publish, which is free; lanes and coordination, which are what we sell |
| Control-plane cell | Stateless API horizontally; Postgres vertically; more cells | (1) Heartbeat and event write amplification; (2) top-of-hour spikes; (3) queue-table bloat from dead tuples | Batch events (10 per request), events go to the ops lake, jitter, a daily-partitioned queue, PgBouncer; move a hot tenant to its own cell by logical replication |
| Scheduler | One leader per cell, O(due) work per tick from the `next_run_at` partial index | Leader failover stalls starts for one lease TTL (30 s) | Missed windows are caught up by `logical_window`, never lost |
| Runner (Python) | Tasks per attempt, spot capacity for batch | Single-process pyiceberg write throughput on batches above about 50 GB | The router sends them to Spark |
| Customer catalog | Theirs | Commit rate limits and server-side quotas on hot tables (unknown per catalog; to be measured in Phase 1) | About 3 commits/min per table on the fastest schedule; lanes |
| Object storage | Effectively unbounded | Small files and `metadata.json` rewrites at high commit frequency | Adaptive micro-batch interval, compaction, `previous-versions-max`, and the Iceberg v4 single-file commit work when it ships |

---

## 9. Reliability and SLOs

### 9.1 SLOs

SLOs are measured monthly per cell and published on a status page per cell.

| # | Surface | Target |
|---|---|---|
| 1 | Control-plane API availability | 99.9% on Team and Business (43 min/month); 99.95% on an Enterprise dedicated cell |
| 2 | Scheduler start latency | 99% of runs leased within 60 s of their jittered time, 99.9% within 5 min, when under the tenant's concurrency cap |
| 3 | Platform-attributable run success | At least 99.5%. Excludes `blocked_quality`, user-code errors, and failures of source or customer-catalog credentials or availability |
| 4 | **Correctness, I1–I4** | **Zero.** No error budget. Any breach is a SEV1 with a public RCA |
| 5 | Console freshness | Run state visible within 5 s p95 of the runner emitting it |
| 6 | Alert delivery | Freshness-breach alerts within 2 min; webhooks within 60 s p99, retried with backoff for 24 h |
| 7 | Control-plane durability | RPO 0 for acknowledged writes (synchronous standby in a second AZ); failover RTO ≤ 5 min; PITR 35 days; region loss RPO ≤ 5 min, RTO ≤ 4 h |
| 8 | BYOC independence | Pipelines keep running for 24 h with the control plane down |
| 9 | Hosted Polaris (Phase 4) | 99.9%; `loadTable` p99 < 150 ms; commit p99 < 500 ms |
| 10 | Hosted SQL (Phase 4) | p95 < 3 s, p99 < 10 s for scans under 1 GB |

We do not offer 99.99%. The architecture-first proposal did, and the business review rightly called
that a contractual liability a seed-stage company cannot carry. Service credits are 10% below the
SLA and 25% below 99.0%.

### 9.2 Failure modes

| Failure | Detection | Recovery |
|---|---|---|
| Runner crashes mid-run | Lease expiry (60 s TTL, 15 s heartbeat) | Requeue with the same `K`; the successor fences the old branch; orphan GC reclaims the files |
| Zombie runner | Self-fence at 45 s; catalog requirement at publish | I2 and I3 (§7.8); a stale result gets 409 |
| Commit landed, acknowledgement lost | The successor's `find_commit`; the nightly auditor | Recorded as published; no second effect |
| Control-plane Postgres primary lost | RDS health | Standby promoted; agents run from the schedule cache and replay buffered events on `(run_id, attempt, seq)` |
| Control-plane region lost | Status probes | BYOC keeps running for 24 h; restore from the cross-region PITR copy; rebuild derived state (§9.4) |
| Customer catalog throttled or down | Publish errors of the "unavailable" class | `retry_wait` with backoff; outside the platform SLO; alert the customer |
| Hot table CAS thrash | More than 2 publish conflicts/s | The lane switches to exclusive; append windows are coalesced |
| Noisy neighbour | Per-tenant queue age | Concurrency caps (Free 1, Team 8, Business 32, Enterprise by contract); token buckets (Team 20 rps, Business 100 rps); plan-weighted fair share |
| Maintenance deletes too much | Dry-run diffs, audit log | Expiry never removes snapshots referenced by a branch or tag, or younger than the idempotency horizon. Orphan GC runs dry for 7 days on every new table class, never deletes files referenced by the last 50 metadata versions or younger than the longest run plus 24 h, and soft-deletes (bucket versioning or a 7-day trash prefix) on managed buckets |
| Bad runner or agent release | Platform failure rate | Digests pinned per run; 1% canary for 24 h; automatic rollback at more than 2× the baseline failure rate; agents supported back to N-2 |
| Agent credential leak | Anomaly alerts | Revoke; certificates last 24 h; the agent holds only its own tenant's references |

### 9.3 Back-pressure

- **Runs:**
  - A run waits in `queued` while its tenant is at its cap.
  - When queue age exceeds the schedule interval:
    - `catchup: true` runs every window in order, which suits append and partition pipelines;
    - `catchup: false` marks older runs `superseded` and fences them, which suits latest-state
      pipelines.
  - At most one run is in flight per `(pipeline, window)`.
- **Events:** the API returns 429 with `Retry-After` when the p99 insert latency in a cell exceeds
  500 ms. Agents buffer up to 7 days or 10 GB.
- **Maintenance:** at most 20% of a data plane's slots, preemptible, run last.
- **Relay:** pull-based with bounded in-flight messages. It pauses when a consumer lags; it does not
  grow buffers.

### 9.4 Disaster recovery and rebuilding the control plane

- **Specs:** rebuilt from the customer's git, using the stored `git_sha`.
- **Ledger:** rebuilt from `_ldp.commits` in each customer's catalog plus the Iceberg snapshot
  summaries. Every LDP commit carries `ldp.run-id`, `ldp.idempotency-key` and `ldp.spec-hash`.
- **Hosted catalog loss beyond PITR** (Phase 4 hosted Polaris):
  - For each table, take the last `metadata_location` the ledger recorded as *acknowledged*.
  - Verify that the file exists and that its current snapshot id matches the ledger.
  - Call `register_table`.
  - Newer `metadata.json` files are listed for human review and **never selected automatically**.
    Failed CAS attempts also leave higher-numbered metadata behind, and picking "the latest on disk"
    could resurrect an unpublished snapshot whose files GC has already deleted.
  - `ldp catalog recover --from-ledger` is rehearsed in a restore drill every quarter.

### 9.5 On-call for 3–5 engineers

- **Paging:**
  - We page only on SLO burn rate: 2% of the monthly budget in 1 h (fast), or 5% in 6 h (slow).
  - Targets: at most 2 pages a week and at most 1 night page a month. If either is missed two weeks
    running, the next sprint starts by fixing whatever caused the pages.
- **Rotation:** one weekly primary rotation, with the founder as secondary once there are 4
  engineers. Design partners get business-hours support in their contract until GA.
- **Runbooks:** each of the top 10 alerts has a runbook with a one-command mitigation:
  - drain a cell;
  - pause a tenant's scheduler;
  - roll back an image digest;
  - fail over Postgres;
  - revoke agent tokens;
  - re-register a table from the ledger.
- **Error-budget policy:** when a service exhausts its monthly budget, feature deploys to it freeze
  until it recovers.
- **Game days,** quarterly: Postgres failover, a lost catalog row, a zombie attempt, a cell outage.

---

## 10. Security, tenancy and compliance

### 10.1 Isolation

| Mode | Control plane | Data plane |
|---|---|---|
| BYOC (all paid tiers in years 1–2) | Multi-tenant cell; RLS on `org_id`; per-org KMS key for envelope-encrypted columns | Customer account. The agent is outbound-only on 443, with mTLS and 24 h certificates issued at registration; no inbound ports; no standing IAM access for LDP. Break-glass is a time-boxed role that the customer grants, and it is audited |
| Pooled (Phase 4) | Same | Hosted Polaris catalog per workspace; storage at `s3://ldp-{cell}/t={tenant}/w={ws}/`; STS session policies scoped to the prefix, 15-minute TTL; per-tenant KMS key; single-tenant gVisor tasks with no host network and an egress allowlist. **No custom Python until the sandbox passes an external pen test** |
| Dedicated cell (Enterprise) | Own Postgres and API; PrivateLink or PSC; region pinning; optional customer-managed KMS | BYOC or silo |

### 10.2 The boundary manifest: what crosses to the control plane

This is published, and a CI test enforces it (`tests/boundary/test_payloads.py`). The test validates
every event and result payload against an allowlist schema and searches the payloads for
credential patterns.

- **Allowed:**
  - specs, including SQL text and column names;
  - schemas (names and types);
  - row, file and byte counts;
  - snapshot ids, snapshot summaries and `metadata_location` strings;
  - check verdicts;
  - numeric metrics for numeric columns;
  - usage meters.
- **Counts only:** string columns and PII-tagged columns report counts only. Violating values are
  sent as salted hashes.
- **Never:**
  - rows;
  - failing-row samples, which stay in `_ldp/quarantine/<run_id>/` and are fetched through the agent
    only by users with `data:read` on that table;
  - manifests, whose per-column min and max bounds are data;
  - credentials;
  - query results, which stream through the tunnel and are never persisted.

### 10.3 Secrets

- Specs hold `secret_ref`s only.
- The spec validator rejects values that look like inline secrets: PEM blocks, `AKIA…`, and
  `password` fields.
- In BYOC, the agent resolves secrets at run time through workload identity.
- On the pooled tier, secrets are envelope-encrypted with the per-tenant KMS key and decrypted only
  inside the run's task.
- The library's logger already never configures handlers (`NullHandler`, namespaced loggers). The
  service attaches a JSON formatter that redacts secrets, and a CI test proves that nothing secret
  reaches logs, URLs or events.

### 10.4 Identity and access

- **Authentication:**
  - OIDC through Google or GitHub on Team;
  - SAML and SCIM on Business and above;
  - scoped API tokens that expire within 90 days;
  - mTLS for agents.
- **RBAC:** the roles are `org_admin`, `workspace_admin`, `developer` (deploy to dev and staging),
  `deployer` (prod), `viewer` and `runner`. Prod deploys and publishes on tables tagged `approval`
  can require a second person.
- **Table and namespace grants** are synced to the catalog's own grants (Polaris catalog roles,
  Unity grants, Lake Formation for Glue), not implemented a second time.
- **Masking on the query path:** masking and row filters apply to queries that go through
  `/v1/query`, using DuckDB views per role.
- **Where masking stops.** Engines that read files directly bypass masking. So for tables with
  masked columns, principals without an unmask grant get no raw-file credentials on the pooled tier.
  In BYOC this is the customer's IAM, and we document the limitation.

### 10.5 PII, GDPR and residency

- **Erasure:** `ldp forget --table T --where "user_id = ?"` runs a copy-on-write delete through
  pyiceberg `delete(delete_filter)`, then expires snapshots older than the erase, then removes
  orphans.
  - The SLA for physical erasure is 30 days end to end, and it is written into the DPA.
  - Tables tagged PII use a 72-hour idempotency horizon, not 7 days, and a maximum snapshot
    retention of 7 days. Tags and branches on PII tables are limited to 30 days.
  - A reproducible ML dataset (0.1.7) on PII data must be a de-identified derived table.
- **Residency:**
  - Each tenant is pinned to a regional cell when it is created.
  - The EU cell opens with the first EU contract, not before.
  - In BYOC, data stays wherever the customer's bucket is.
- **Published documents:** the subprocessor list, the DPA and the SCCs.

### 10.6 SOC 2 and the supply chain

- **SOC 2:**
  - Compliance automation is bought in the first month of cloud work.
  - Type I at about month 9 of cloud work (Phase 3); Type II after a 6-month window (Phase 4).
  - HIPAA only in BYOC with a BAA; ISO 27001 when enterprise pipeline demand requires it.
- **Controls:**
  - the contract's 4-job CI, plus PR review, as change management;
  - infrastructure as code for everything;
  - employee SSO, MFA and device management;
  - just-in-time production access with session recording, logged to the audit log the tenant can
    see;
  - dependency and container scanning;
  - an annual pen test and quarterly access reviews;
  - encryption with TLS 1.2 or later everywhere and SSE-KMS at rest.
- **Supply chain:**
  - Worker and agent images are signed with cosign and ship with an SBOM.
  - Dependencies are pinned. `spark/project.scala` already pins Spark 4.1.3, Iceberg 1.12.0 and
    sqlite-jdbc 3.53.4.0.
  - Wheels are reproducible.
  - `publish.yml` must build from the repo root.
- **The security review package** is maintained like code and treated as a product artifact: the
  SOC 2 report, DPA, boundary manifest, BYOC architecture document and pen-test summary.

### 10.7 Licensing

- `LICENSE` is currently the MIT template with "Copyright (c) 2022 Read the Docs Inc", while
  `pyproject.toml` says `Apache-2.0`. This is fixed in Phase 0; Apache-2.0 matches Iceberg and
  Polaris and carries a patent grant.
- We add a DCO before outside contributions grow.
- The control plane is proprietary. The agent is part of the Apache-2.0 wheel, so security teams can
  read it.
- We register the "LDP" and "LDP Cloud" trademarks, so a cloud provider can host the OSS but cannot
  call its service by our name.

---

## 11. Economics

### 11.1 COGS drivers, largest first at scale

| # | Driver | Why it stays small | Share of COGS at $100M (planning) |
|---|---|---|---|
| 1 | Support, solutions engineering and customer success counted in COGS | Self-serve up to Team; the security package shortens reviews | about 50% |
| 2 | Control-plane cells: Postgres Multi-AZ, API, observability; about $25k/month per shared cell (an assumption) | Fixed per cell; nearly zero marginal cost per BYOC tenant | about 20% |
| 3 | Hosted compute for the pooled Team tier (Phase 4) | About $0.04 per vCPU-hour on demand, about $0.08 loaded with spot and idle overhead. **These are assumptions, not research; verify against the AWS price list** | about 12% |
| 4 | Third-party SaaS: auth, billing, compliance, observability seats | — | about 10% |
| 5 | Hosted storage, passed through near cost | Bring-your-own-bucket is the default | about 5% |
| 6 | LLM tokens for the AI analyst (Phase 5), billed at cost plus 30% | Its own SKU | about 3% |

**BYOC carries no customer compute in our COGS.** That is the structural reason gross margin can
exceed a compute platform's. It is also why our NRR will be lower than one (§11.5).

### 11.2 Pricing and packaging

The price is per org, users are unlimited, and the value unit is the **governed table**. A table is
governed in a month if LDP published to it, or it has an LDP maintenance or retention policy, or it
has an LDP data contract. Four reasons for this unit:

- It follows the ledger, which is the moat.
- It is predictable. Airbyte moved from volume to capacity pricing because volume pricing was
  unpredictable, after feedback from 500+ organisations
  ([source](https://airbyte.com/blog/introducing-capacity-based-pricing)).
- It survives procurement's comparison with Dagster+ hybrid, which has no compute charge
  ([source](https://dagster.io/pricing)).
- Neither of the losing proposals' fees per LCU-hour on the customer's own compute survives that
  comparison.

| Tier | Price | Includes |
|---|---|---|
| OSS | $0, Apache-2.0 | Everything in §4.6's OSS column |
| Cloud Free | $0 | 1 workspace, 3 users, 25 governed tables, 5 pipelines, daily schedules, 7-day ledger, Slack alerts |
| Team | **$500/month** + $2 per governed table-month above 250 | 3 workspaces, hourly schedules, 90-day ledger, alerts, maintenance autopilot, cost per table, OIDC SSO, one data plane |
| Business | **From $3,000/month, billed annually ($36k)**, including 2,000 governed tables; $1.50 per table-month above that | Several data planes, SAML/SCIM, RBAC synced to catalog grants, publish approvals, PII-safe metric policy, audit export, Terraform provider, 1-year ledger, 99.9% SLA |
| Enterprise | **From $120k/year**, including 5,000 governed tables; $1.00 per table-month above that | Dedicated cell option (+$25–50k), PrivateLink, residency, 7-year audit retention, 99.95% SLA, named support, committed-use discounts |
| Hosted meters (Phase 4) | $0.25 per LCU-hour (1 vCPU + 4 GiB, billed per second, 60 s minimum); $0.025/GB-month storage | Pooled compute and storage |

Price anchors:

- Team's fee sits beside MotherDuck Business at $250/month plus usage
  ([source](https://motherduck.com/pricing/)) and Dagster Starter at $100/month
  ([source](https://dagster.io/pricing)).
- Hosted storage sits at cost, against S3 Standard at $0.023 and S3 Tables at $0.0265 per GB-month
  ([source](https://aws.amazon.com/s3/pricing/)) and R2 at $0.015
  ([source](https://blog.cloudflare.com/r2-data-catalog-public-beta/)).

Runs are included up to 3,000 per governed table per month, which covers a 15-minute schedule. There
is no fee per run: a fee on retries would tax correctness.

### 11.3 Unit checks, at 2032 averages

| Tenant | Revenue per year | COGS per year (planning) | Gross margin |
|---|---|---|---|
| Team, 400 tables with hosted compute | $6k + 150 × $2 × 12 = $3.6k + 13.6k LCU-h × $0.25 = $3.4k, so **$13k** | Compute $1.1k + control-plane share $0.4k + support $1.5k = $3.0k | about 77% |
| Business, 3,600 tables | $36k + 1,600 × $1.50 × 12 = $28.8k, so **about $65k** | Control-plane share $2k + support and SE $9k = $11k | about 83% |
| Enterprise, 12,000 tables | $120k + 7,000 × $1 × 12 = $84k, plus about $46k of add-ons and dedicated cells, so **about $250k** | Blended $45k (20% take dedicated cells) | about 82% |

Blended gross-margin targets:

- about 60–65% in 2027–28, with design-partner support and fixed cell cost;
- at least 75% by $15M ARR;
- about 80% at $100M.

### 11.4 Path from $0 to $100M ARR

This is a plan, not a forecast. The calendar is: 0.1.1 now, cloud GA at the end of 2027.

| Year end | Team (orgs × ACV) | Business | Enterprise | ARR | Growth |
|---|---|---|---|---|---|
| 2027 | 40 × $7k | 12 × $40k | 2 × $120k | **$1.0M** | — |
| 2028 | 200 × $8k | 50 × $45k | 8 × $150k | **$5.1M** | 5.0× |
| 2029 | 500 × $9.5k | 150 × $50k | 25 × $180k | **$16.8M** | 3.3× |
| 2030 | 900 × $11k | 330 × $55k | 60 × $210k | **$40.7M** | 2.4× |
| 2031 | 1,200 × $12k | 520 × $60k | 110 × $235k | **$71.5M** | 1.8× |
| 2032 | 1,400 × $13k | 700 × $65k | 150 × $250k | **$101.2M** | 1.4× |

Sanity checks:

- **Pace.** $1.0M to $101M in five years is about 2.5× a year compounded. That is slower than dbt
  Labs' $2M to $100M in four years, about 2.7× a year
  ([source](https://www.getdbt.com/blog/dbt-labs-100m-arr-milestone)). The operations-first plan's
  3.8× a year was faster than its own precedent; this plan is not.
- **ACV.** The blended ACV at $100M is about $45k. That is in line with Astronomer's roughly $56k per
  customer (my arithmetic: about $39.5M of revenue, [unverified](https://research.contrary.com/company/astronomer),
  over 700+ enterprise customers, [source](https://www.astronomer.io/press-releases/astronomer-secures-93-million-series-d-funding/)).
  It is not 4× it, which was the business review's objection to the other plans.
- **Customer count.** 2,250 paying orgs is well under dbt Cloud's 5,000+ customers at its $100M.
- **NRR assumption:** 110% in 2028, 115% in 2029, 120% from 2030. Governed tables grow with the
  customer's data estate, but we do not own compute. So we plan below Snowflake's 125–126%
  ([source](https://www.sec.gov/Archives/edgar/data/0001640147/000164014726000033/fy2027q2earnings.htm))
  and Starburst's 130% NDR
  ([source](https://www.starburst.io/press-releases/starburst-crosses-100m-arr-as-enterprises-move-from-bi-to-ai/)).
- **New-logo ARR implied by that NRR:** about $4M in 2028, $11M in 2029, $21M in 2030, $23M in 2031
  and $15M in 2032.
- **Sales capacity.** Assume a ramped AE and SE pair closes about $1M of new ARR a year, and
  self-serve sources about 35% of new-logo ARR. That needs about 7 pairs in 2029, 13 in 2030 and 15
  in 2031, plus CSMs to carry NRR. The first AE and SE are hired at about $1.5M ARR, in H1 2028.

### 11.5 Capital plan

These are assumptions, anchored to the research.

| Round | Size | At | Anchor |
|---|---|---|---|
| Seed | $4–6M | Now to Phase 2 | Bauplan's $7.5M seed ([reported](https://technews180.com/funding-news/python-first-ai-platform-bauplan-scores-7-5m-in-seed-funding/)); Tower's EUR 5.5M ([reported](https://tech.eu/2026/03/13/tower-secures-eur55m-to-support-data-engineers-in-the-ai-era/)) |
| Series A | $15–20M | About $1.5–2M ARR (2028) | Series A rounds of $15–32M in the category: Mozart, Keboola, Y42 (§2.4) |
| Series B | $40–50M | About $6–10M ARR (2029) | — |
| Series C | $60–80M | About $25–40M ARR (2030–31) | — |
| Total | **about $120–150M** by $100M ARR | | MotherDuck had raised about $133M ([reported](https://sacra.com/c/motherduck/)) |

### 11.6 What a $1B valuation implies

The multiples below are **my assumptions**. The comparables are from the research only.

| Path | Needs | Multiple | Comparable |
|---|---|---|---|
| **A: the plan** | About $100M ARR in 2032, growing about 40%, NRR ≥ 120%, gross margin ≥ 78% | 10× (assumption) | Starburst crossed $100M ARR growing nearly 40% with 130% NDR ([source](https://www.starburst.io/press-releases/starburst-crosses-100m-arr-as-enterprises-move-from-bi-to-ai/)); its last mark was $3.35B in 2022, a peak-era mark ([source](https://www.starburst.io/blog/starburst-announces-250m-series-d/)). 10× looks conservative next to it, but Starburst owns a query engine and we do not |
| **B: early** | About $50M ARR growing at least 2× | About 20× | Astronomer's $775M ([reported](https://research.contrary.com/company/astronomer)) on about $39.5M revenue ([unverified](https://research.contrary.com/company/astronomer)) is about 20×, and that was a down round from about $1.5B. The plan passes $40M in 2030 at 2.4×, which would need about 25×. **Not plannable** |
| **C: strategic** | Control of an Iceberg control point that acquirers compete for | n/a | Tabular sold for more than $1B ([reported](https://www.techtarget.com/searchdatamanagement/news/366588032/Databricks-1B-plus-Tabular-acquisition-adds-Iceberg-support)), nearly $2B per Bloomberg ([reported](https://techcrunch.com/2024/08/14/databricks-reportedly-paid-2-billion-in-tabular-acquisition)), on about $1M ARR (reported, same source). Acquirers have paid for Iceberg positions: SAP–Dremio ([source](https://news.sap.com/2026/05/sap-to-acquire-dremio-unify-sap-and-non-sap-data-power-agentic-ai/)), Qlik–Upsolver ([source](https://www.qlik.com/us/news/company/press-room/press-releases/qlik-acquires-upsolver-to-deliver-low-latency-ingestion-and-optimization-for-apache-iceberg)), and IBM–Confluent at an $11B enterprise value ([source](https://newsroom.ibm.com/2025-12-08-ibm-to-acquire-confluent-to-create-smart-data-platform-for-enterprise-generative-ai)). **Upside only.** LDP will not control the format. Neutrality keeps several possible acquirers interested |

**What the comparables warn.** The two proposals that lost read the lesson wrongly as "seat pricing
gets absorbed, consumption pricing compounds". The record says something narrower:

- Seat-priced tools were absorbed: Dagster by Prefect ([source](https://www.prefect.io/prefect-acquires-dagster)),
  GX Cloud by FICO and then shut down ([source](https://greatexpectations.io/blog/an-update-from-great-expectations/)),
  and Metaplane by Datadog ([source](https://www.datadoghq.com/about/latest-news/press-releases/datadog-metaplane-aquistion/)).
- So were consumption-priced ones. Fivetran merged with dbt Labs, and Astronomer was marked down.
- The 125%+ NRR and 10×-at-$100M anchors belong to companies that own a compute engine.

The base rate for a control-plane-only tool is an outcome below $1B. Path A therefore requires all
of the following:

**Metrics that must be true at the $1B mark:**

- at least $100M ARR, growing 35–40%;
- NRR of at least 120%;
- gross logo churn below 1% a month on Business and above;
- gross margin of at least 78%;
- at least 70% of Business and Enterprise governed tables carrying more than 12 months of ledger
  history;
- the median Business customer paying for at least 3 modules (ledger, maintenance and cost, or
  hosted SQL and the AI analyst);
- no single cloud above 40% of ARR;
- zero correctness breaches in the trailing 12 months;
- about 25k weekly-active OSS projects.

**If these fail at Series B, we say so.** The realistic outcomes are then a $200–400M company or a
strategic sale, and we plan capital accordingly.

### 11.7 Moat, and how we test it at each fundraise

**What is not a moat:** the table format (open), the catalog (Polaris and Unity OSS are free),
storage and compaction (commoditised), and the engines (DuckDB is MIT, and its team now works at AWS
[source](https://www.aboutamazon.com/news/company-news/aws-ducklabs)). Anyone can wrap pyiceberg in a
CLI.

**What compounds:**

1. **The ledger as system of record.** Audits, rollbacks, erasure proofs, reproducible ML datasets
   and incident forensics all come to depend on it. Switching cost grows with governed tables times
   months. The raw facts also live in the customer's tables (`ldp.*` summaries and `_ldp.*`), so
   customers trust it enough to depend on it.
2. **Specs in the customer's repo.** This is the dbt pattern. Hundreds of `ldp.json` specs,
   checks and CI wiring are the switching cost; the tables stay open.
3. **Neutrality.** Each hyperscaler and warehouse promotes its own catalog. None can credibly offer a
   write path that is equally good on all of them.
4. **Correctness reputation and upstream standing.** Once upstreamed, the fixes to pyiceberg in §12
   put the project in the path of pyiceberg's distribution.
5. **Fleet data.** Compaction thresholds, file-size targets and router thresholds get tuned on
   millions of tables.
6. **The BYOC trust package,** which takes 12–18 months to earn.

**Tests at each fundraise:**

- logo churn below 1% a month on Business and above;
- at least 70% of Business and Enterprise tables with more than 90 days of ledger history;
- at least 25% of connectors in use contributed by the community;
- zero correctness breaches in the trailing 12 months.

---

## 12. Go-to-market

**Motion.** Developer-led, then product-led, then sales-assisted.

| Stage | Action | Metric |
|---|---|---|
| 1 | `pip install local-data-platform && ldp demo` | Time to first query under 5 minutes (0.1.9, #107) |
| 2 | `ldp run` on the user's own data from a template: Postgres CDC, events (#89), Sheets (#106), BigQuery | First run within 1 day |
| 3 | `ldp login && ldp deploy` to Cloud Free with a data plane from `ldp dataplane create --aws` | First scheduled cloud run within 24 h of signup |
| 4 | Product-qualified lead: at least 3 scheduled pipelines, at least 2 active users, and either more than 50 GB or at least 1 blocked bad write | Free-to-paid conversion of at least 4% of weekly-active cloud orgs within 90 days; guardrail at 2% (§4.6) |
| 5 | Team by credit card; Business sales-assisted (security review, SSO, several data planes); prices published | 60% of paid ARR from product-qualified accounts |
| 6 | Enterprise: field sales plus an SE | $120k+ ACV |

**Beachheads,** each time-boxed to one quarter with a kill metric:

1. **Snowflake bronze and silver offload.** `ldp plan --cost` compares the warehouse credits a
   transform uses today with its LDP cost. The gold layer stays in Snowflake, which reads the tables
   through its S3 Tables REST integration
   ([source](https://docs.snowflake.com/en/release-notes/2026/other/2026-08-10-amazon-s3-tables-iceberg-rest-catalog-integration-ga)).
2. **S3 Tables and R2 Data Catalog adopters.** They have a catalog but no write framework. R2 lists
   PyIceberg as a supported engine ([source](https://blog.cloudflare.com/r2-data-catalog-public-beta/)).
3. **GX Cloud refugees.** GX Cloud stopped being publicly available on June 1, 2026
   ([source](https://greatexpectations.io/blog/an-update-from-great-expectations/)). We offer
   `ldp import-gx` and three free months of Team. The pool is probably small, because GX Core
   continues under Fivetran, so this play is limited to one quarter.

**Content and community.** These are cheap and come first:

- **Publish the upsert repro:** "pyiceberg upserts duplicate keys under concurrency; here is why and
  the fix."
- **Upstream to pyiceberg:**
  - fix `Transaction._stage` dropping a second `AssertRefSnapshotId`;
  - add a public `fast_forward(branch, expected_main)`;
  - add a hook so an idempotency-key check runs inside the commit retry.
- **Talks:** Iceberg Summit, which drew 600+ attendees in 2026
  ([source](https://www.snowflake.com/en/blog/engineering/iceberg-summit-2026-recap-v4-spec/)), and
  PyData.
- **A content series:** "WAP for Iceberg in Python", "one table, every engine" (Python writes, the
  Scala job reads and aggregates, DuckDB queries, Snowflake attaches), and "idempotent backfills".

**Positioning against Fivetran and dbt Labs.**

- We are the governed write path for tables your engineers write in Python, and the ledger across
  every writer, theirs included.
- Land raw SaaS data with Fivetran or Estuary; build gold with dbt; use LDP for everything
  Python-authored and for the audit trail.
- We do not build a SaaS connector catalogue.

**Channels:**

- **AWS Marketplace** for BYOC, so customers can spend existing AWS commitments.
- **An S3 Tables quickstart.**
- **Listings** on the Polaris and Unity OSS ecosystem pages.
- **Consultancies** in fintech CDC, consumer event analytics and customer-facing analytics, each with a
  template.

**Sales motion and hiring:**

- The founder sells to the first 10–20 design partners.
- The first two engineering hires are control-plane and data-plane (§13). DevRel comes at month 6.
- A GTM co-founder or VP Sales is hired in 2027. The first AE and SE pair comes at about $1.5M ARR.
- Enterprise AEs, each paired with an SE, come from about $8M ARR, following the capacity plan in
  §11.4.

**Weekly metrics:**

- weekly-active OSS projects (opt-in telemetry, off by default, plus GitHub dependents);
- deploys;
- conversion;
- blocked bad writes, shown as incidents prevented;
- NRR.

We never steer by downloads.

**What we will not do:**

- sell SMB services (the "Talk to Us" agency in `docs/wiki/business/PROPOSAL.md`);
- gate correctness;
- build a table format or a catalog;
- price per row;
- tax customer compute.

---

## 13. Roadmap

Dates assume a start on 2026-10-01. Every phase has exit criteria. A milestone closes only through
linked, merged PRs; about 30 issues were bulk-closed as completed on 2025-12-21 without code, and
that must not happen again.

### Phase 0: ship 0.1.1 honestly (2 weeks, to 2026-10-14)

Milestones 0.1.1 (#19–#24, #51, #8), 0.1.2 (#3, #23) and 0.1.9 (#107); contract items F1–F10.

**The real risk is a lost working tree, not missing code.** Most of 0.1.1 is written but not
committed:

- `cli.py`, `config.py`, `demo.py`, `paths.py`;
- `engine/duckdb/`, `engine/spark/`, `quality/`, `pipeline/registry.py`;
- `spark/`, `docs/design/`;
- about 20 test files.

The last commit is `09755aa` from 2025-07-27.

1. Commit and push the working tree on `feat/v0.1.1-hardening`, review it, and merge.
2. Fix `LICENSE` to Apache-2.0. Make `publish.yml` build from the repo root. Add the contract's
   4-job CI (lint; Python 3.12 and 3.13 × pyiceberg 0.9 and latest; wheel smoke test;
   `mkdocs build --strict`).
3. Land the two write-path fixes (§7.9: one transaction, and counts from summaries), since they
   change `WriteResult`, which is in the contract.
4. Publish 0.1.1 to PyPI. It still has only 0.1.0, from 2024-09-30, pinned to `pyiceberg<0.8`.
5. Make the README describe only what exists.
6. Clean up the backlog:
   - close #101–#105 and #114 as off-topic;
   - reopen #88, #89 and #106 with acceptance criteria;
   - close PR #115, which `factory_registry.md` supersedes;
   - file one issue per Phase 1 module.

**Exit:**

- `pip install local-data-platform==0.1.1 && ldp demo` passes all 8 steps in a fresh venv in CI on
  Python 3.12 and 3.13;
- `make smoke` is green;
- there are no import errors;
- the #29 positioning decision is written into `VISION.md`.

### Phase 0.5: demand test (6 weeks, in parallel, to 2026-11-18)

This gates Phase 2.

1. Publish the upsert repro and the fix as content.
2. Hold 20 discovery calls with teams running pyiceberg in production, found through the founder's
   network and the pyiceberg community.
3. Offer a **concierge paid pilot** at $500–1,000/month. The founder runs the team's `ldp run` under
   their own scheduler, with a shared Postgres runs table, Slack alerts, and a weekly ledger and
   cost-per-table report. No control plane is built.

**Kill or pivot** if fewer than 3 of 10 qualified teams pay within 6 weeks. The pivots, in order of
preference:

- sell the ledger as an add-on to existing orchestrators;
- OSS plus paid support.

### Phase 1: 0.2.0, "multi-writer, any catalog, object storage" (to 2027-01-31)

This rewrites the open 0.2.0 "Cloud Integration" milestone. It covers #88, F9,
`SupportedEngine.PYSPARK` and the docker-compose fixtures from `origin/v0.1.1` `f372eeb`. #75 moves to
Phase 3.

**New OSS modules and interfaces:**

```python
# src/local_data_platform/catalog/provider.py
CatalogFactory = Callable[[Mapping[str, Any], Path | None], "pyiceberg.catalog.Catalog"]
def register_catalog_type(name: str) -> Callable[[CatalogFactory], CatalogFactory]: ...
def create_catalog(spec: Mapping[str, Any], *, base_dir: Path | None = None,
                   profile: "Profile | None" = None) -> "pyiceberg.catalog.Catalog": ...
# built-ins: "local" (today's {identifier, warehouse_path}; one project .ldp/catalog.db with a shim
# for <name>_catalog.db), "sql" (any SQLAlchemy URI, e.g. postgresql+psycopg://, JdbcCatalog-compatible),
# "rest", "glue". format/iceberg line 284 switches to create_catalog().

# src/local_data_platform/format/iceberg/commit.py
@dataclass(frozen=True)
class CommitContext:
    run_id: str; attempt: int; idempotency_key: str; spec_hash: str
    search_since_ms: int
    logical_window: tuple[datetime, datetime] | None = None
    source_positions: Mapping[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class CommitPolicy:
    max_publish_attempts: int = 6; base_delay_s: float = 0.2; max_delay_s: float = 8.0
    deadline_s: float = 300.0; max_search_depth: int = 500

@dataclass(frozen=True)
class StagedWrite:
    branch: str; base_snapshot_id: int; staged_snapshot_id: int; schema_id: int; rows_written: int

def branch_name(run_id: str, attempt: int, n: int = 0) -> str: ...        # ldp_r<hex>_a<attempt>_<n>
def ensure_base(table) -> int: ...                                        # empty ldp.init snapshot
def find_commit(table, key: str, *, since_ms: int, max_depth: int = 500) -> "Snapshot | None": ...
def fence(catalog, table, branches: Iterable[str]) -> None: ...
def stage(table, df: pa.Table, mode: str, ctx: CommitContext, *, join_cols=None,
          overwrite_filter=None, n: int = 0) -> StagedWrite: ...
def publish(catalog, table, staged: StagedWrite) -> "Snapshot": ...
def write_once(load: Callable[[], "Table"], catalog, df: pa.Table, mode: str, ctx: CommitContext, *,
               audit: Callable[["Table", int], "QualityReport"] | None = None,
               policy: CommitPolicy = CommitPolicy()) -> "WriteResult": ...
class CommitConflict(LDPError): retriable: bool
class CommitSearchExhausted(LDPError): ...

# format/iceberg: Iceberg.put(df, mode=None, *, commit: CommitContext | None = None,
#                             overwrite_filter=None) -> WriteResult
# WriteResult gains: branch, idempotency_key, attempts, skipped_duplicate
```

Other Phase 1 modules:

- **`fs.py`:** `filesystem_for(uri)`, `open_input(uri)` and `open_output_atomic(uri)` over
  `pyarrow.fs`. CSV, Parquet and JSON move off `os.path` and `open()`.
- **`engine/duckdb`:** `register_iceberg(..., stream=True)` using `scan().to_arrow_batch_reader()`,
  and `attach_rest(catalog_uri, token)` for `iceberg_scan`.
- **`engine/router.py`:**
  - `estimate_scan_bytes(table, row_filter=None, snapshot_id=None) -> int`;
  - `choose_engine(scan_bytes, how, available, *, duckdb_max_bytes=50 * 2**30) -> SupportedEngine`.
- **`engine/spark`:**
  - Generalise `spark_catalog_conf` into `spark_catalog_conf_for(spec) -> dict[str, str]` for the
    `local`, `sql` (JDBC Postgres) and `rest` types. Keep `jdbc.schema-version=V1` and the
    requirement that `catalog_name` matches.
  - `ScalaSparkJob` gains `--branch`. The job writes only to the branch, with
    `snapshot-property.ldp.*` options, and Python publishes.
- **`events.py`:**
  - `RunEvent` and an `EventSink` protocol with `NullSink`, `JsonlSink`, `HttpSink` and
    `OpenLineageSink`.
  - `Pipeline.run(mode=None, *, commit=None, sink=NullSink()) -> PipelineResult`. `PipelineResult`
    gains `run_id`, `idempotency_key` and `published_snapshot_id`.
- **`spec.py`:**
  - `API_VERSION = "ldp/v1"`, `json_schema()`, `spec_hash(config)`, `validate_spec(data) -> list[ConfigError]`;
  - the `ldp schema` and `ldp plan` commands.
- **`maintenance/`:**
  - `expire_snapshots(table, *, older_than, retain_last=20, horizon=timedelta(days=7))`, which
    refuses to break the idempotency floor;
  - `remove_orphans(table, *, older_than_hours=72, dry_run=True, keep_metadata_versions=50)`;
  - `compact(table, *, target_file_mb=512, engine=None)`, using Spark `rewrite_data_files`, or
    deferring to S3 Tables;
  - the `ldp maintain` command.
- **Tests:** the Phase 1 rows in §7.15, plus `dev/docker-compose.yml` (Postgres, MinIO, Polaris)
  revived from `f372eeb`.

**Exit:**

- `test_upsert_race` fails on raw pyiceberg and passes through `write_once`;
- the 60-minute harness (8 pyiceberg writers and 1 Spark writer on Postgres + MinIO and on Polaris)
  loses 0 rows and duplicates 0 keys;
- re-running the same `(pipeline, window)` is a no-op;
- one config runs on the laptop and against Postgres + MinIO with only the profile changed;
- an upstream pyiceberg PR for the requirement bug is open;
- 0.2.0 is on PyPI.

### Phase 2: 0.3, "Deploy": control-plane alpha, BYOC only (Feb–Jun 2027)

Milestones 0.1.3 Orchestration (#42, #44; PR #50 comes back as the scheduler) and 0.1.5 Monitoring;
#106 alerts; #29. This phase starts only if Phase 0.5 passed.

**In a private `ldp-cloud` repo:**

- `api/` (FastAPI, §4.3) and `db/` (§6.2, Alembic, expand/contract migrations only);
- `scheduler/` (lease-row leader, jitter, SKIP LOCKED, lanes, fence lists, catchup and supersede);
- the relay and notifier (Slack, webhook);
- metering, still unbilled;
- console v0 (runs, the ledger, quality timeline, cost per pipeline);
- the nightly auditor.

**In the OSS repo:**

- `agent/` (the lease loop, schedule cache, event buffer) and `ldp agent`;
- `cloud/client.py`;
- the verbs `login`, `deploy`, `runs`, `logs`, `backfill` and `rollback`;
- one Terraform module for AWS ECS. There is no Helm chart and no Cloud Run until a paying customer
  asks.

**Agent protocol:**

- `POST /agent/v1/lease {data_plane_id, capacity, agent_version}` returns a `Lease`
  (§5.4, step 2).
- `POST /agent/v1/heartbeat {run_id, attempt}` returns 200, or 409 if the lease is lost, in which
  case the runner stops before publishing.
- `POST /agent/v1/events [RunEvent]` is idempotent on `(run_id, attempt, seq)`.
- `POST /agent/v1/results {run_id, attempt, write_result, quality, metadata_location}`.

**Exit:**

- 10 design partners, at least 5 of them paying;
- at least 1,000 scheduled runs a day sustained for 30 days;
- the scheduler SLO met;
- zero correctness violations in the auditor;
- a fault-injection suite (toxiproxy, Postgres failover, runner kills) passing nightly.

### Phase 3: 1.0 GA (Jul–Dec 2027)

0.1.5 Monitoring completes; #75; #106 and #48 as free templates.

- Billing on governed tables. Self-serve Team; Business on annual contracts.
- SSO (OIDC; SAML/SCIM for Business), RBAC synced to catalog grants, and the audit log with export.
- The maintenance autopilot, with the guards in §9.2.
- Cost per table and per pipeline, and budget caps.
- External-commit discovery: other engines' snapshots in the ledger.
- The Airflow and Dagster operators, and `ldp import-gx`.
- Connectors through the registry: a Snowflake source (#75), Google Sheets (#106), Excel (#48).
- SOC 2 Type I. The EU cell opens only with the first EU contract.

**Exit:**

- $1.0M ARR, about 54 paying orgs;
- 99.9% API availability for 90 days;
- at most 2 pages a week;
- gross margin of at least 60%;
- the first product-qualified accounts converting without a sales call.

### Phase 4: platform expansion (2028)

Milestones 0.1.4 Gold (#45), 0.1.6 BI (#106) and 0.1.7 Data Science; #89.

- **The pooled tier:** hosted Polaris per workspace with credential vending, gVisor runners after a
  pen test, and hosted compute and storage meters.
- **Serverless SQL,** and scheduled SQL reports to Slack.
- **Gold data products with owners (`Config.who`):**
  - releases across several tables (§7.12);
  - `dbt-duckdb` on Iceberg.
- **Snapshot-pinned ML datasets:** `ldp dataset pin`.
- **Streaming micro-batch** with offset continuity (#89), with a 1B-events/day reference customer.
- **Platform and compliance:**
  - a GCP data plane once there are 5 paying GCP customers;
  - a dedicated-cell option;
  - SOC 2 Type II.

**Exit:**

- $5M ARR;
- NRR of at least 110%;
- 8 or more Enterprise logos at $100k or more;
- 5k weekly-active OSS projects.

### Phase 5: scale (2029 onwards)

Milestone 0.1.8 LLM (#59).

- **The AI analyst:** text-to-SQL grounded in schemas, quality history and the ledger, executed
  through `/v1/query` against pinned snapshots, plus semantic catalog search (#59).
- **Iceberg format:** v3 by default where every writing engine supports it; v4 single-file commits.
- **Platform:** multi-region cells, and a marketplace for connectors and checks on the registry SDK.
- **Hedges:** column-level lineage through SQL parsing, and a DuckLake read connector if mid-market
  demand appears.

### Team plan

| Who | When | Owns |
|---|---|---|
| Founder | Now | Iceberg semantics, the commit protocol, Spark, the TLA+/P model, sales to design partners |
| Engineer 2 | Phase 1 | Control-plane API, scheduler, coordinator, metering (Postgres and distributed systems) |
| Engineer 3 | Phase 2 | Agent, runner images, Terraform, SRE, on-call tooling |
| Engineer 4 | Month 6 of cloud work | DevRel and developer experience: docs, templates, integrations |
| Engineer 5 | Month 9 of cloud work | Security and compliance, the boundary manifest, sandboxing |
| GTM co-founder or VP Sales | 2027 | Pricing, pipeline, first AE and SE hires |

---

## 14. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Monetization gap.** Correctness is free and orchestrators are free, so teams get our core promise without paying | Phase 0.5 tests the paid features specifically (ledger, coordination, cost) before we build them; the conversion guardrail; per-table pricing tied to the ledger; honest ceilings in §11.6 |
| 2 | **Control-plane ceiling.** Category comparables stall below $1B: Astronomer was marked down ([reported](https://research.contrary.com/company/astronomer)), and Dagster and GX Cloud were absorbed | Several modules (ledger, maintenance, cost, hosted SQL, AI analyst); moat tests at each round; a capital plan that survives a $200–400M outcome |
| 3 | **No demand evidence yet.** PyPI has 0.1.0 only; there are no design partners | Gate Phase 2 on paid pilots; steer by paying teams, never downloads |
| 4 | **Fivetran and dbt Labs move into governed Iceberg writes** (about $600M ARR, 100,000+ teams) | Neutrality, Python-first, provable correctness with several writers; integrate rather than fight; their commits appear in our ledger |
| 5 | **Hyperscalers bundle maintenance and governance** (S3 Tables compaction cut 50–90%; Cloudflare Data Platform, [source](https://blog.cloudflare.com/cloudflare-data-platform/)) | Never charge a margin on storage or compaction; defer to native maintenance; sell the cross-catalog ledger and cost view they will not build neutrally; no cloud above 40% of ARR |
| 6 | **Dependence on pyiceberg internals.** `Catalog.commit_table` and `pyiceberg.table.update` classes are public-ish; `_stage` drops requirements | Upstream a fix and a public fast-forward API; pin and test pyiceberg (0.9, latest) in CI; the commit primitives live in one module |
| 7 | **A correctness incident** (a duplicate or bad publish in a customer's gold table) | Invariants with zero budget; the TLA+/P model; the harness as a release gate; the nightly auditor; rollback is one ref move; the ledger shows exactly what happened |
| 8 | **Schema changes visible at stage time** (Iceberg schemas are table-wide) | Allow only additive nullable changes in runs; breaking changes go through `ldp plan --apply`; the publish asserts the schema id |
| 9 | **Idempotency horizon versus expiry versus GDPR** | A 7-day horizon by default, 72 hours on PII tables; `find_commit` fails closed; erasure SLA of 30 days |
| 10 | **BYOC support cost before product-market fit** | One Terraform module (AWS ECS) and one agent that is the OSS wheel; other runtimes only for paying customers |
| 11 | **DuckLake as a competing format.** Metadata in SQL, like our SQLite catalog; AWS owns DuckLabs ([source](https://ducklake.select/2026/04/13/ducklake-10/)) | Iceberg stays the default, because it is where Snowflake, Databricks, Google, AWS and SAP converged; the format seams stay open; a DuckLake read connector if 3 paying customers ask |
| 12 | **Losing the untracked working tree** (most of 0.1.1 is not committed) | Phase 0, step 1, this week |
| 13 | **Small-team burnout** | Paging on burn rate only; business-hours support until GA; customer-caused failures route to the customer; buy everything |
| 14 | **Licensing confusion** (the MIT and Read the Docs `LICENSE` versus Apache-2.0 in pyproject) | Fixed in Phase 0; a DCO; registered trademarks |
| 15 | **Soft market data.** Several marks are 2021–22 peaks; many 2025–26 deal terms are undisclosed; there is no analyst TAM | The plan rests on bottom-up unit economics and a 10× assumption; strategic multiples are upside only |
| 16 | **A single technical founder with no GTM leader** | A GTM co-founder or VP Sales in 2027; the founder sells the first 10–20 design partners |

---

## 15. Alternatives considered

### 15.1 Architecture-first: "an Iceberg control plane with our own REST catalog"

**The design:**

- `catalogd`, a self-built Iceberg REST catalog, running fence check, dedupe, CAS, `commit_log` and
  outbox in one Postgres transaction;
- Kafka and seven event consumers;
- an ingest gateway;
- a query router;
- consumption pricing that included $0.08 per LCU-hour on the customer's own compute.

**Why it lost:**

- **Its guarantees needed customers to move their catalog of record to a startup.** Bring-your-own
  catalog was a degraded mode with external writes unfenced. Yet the ICP runs Glue (39.3%), S3
  Tables (25%) and Polaris (21.4%).
- **It contradicted its own security model.** A REST catalog writes `metadata.json` on the server,
  so BYOC would have required the control plane to write the customer's metadata prefix, against
  its own "metadata only" promise.
- **Phase 2 could not be built by 3–5 people:** catalogd, the coordinator, the relay, the console, a
  REST compatibility suite, chaos testing and 20 partners in four months.
- **Its enterprise revenue rested on a fee that procurement compares with $0.**
- **Its streaming design was wrong.** Consumer groups assign Kafka partitions, not tables, so the
  one-committer-per-table claim did not hold.

**What we kept:**

- the invariants and the nightly auditor;
- fencing at the serialisation point, which we do with ref requirements at the customer's catalog
  instead of our own;
- rebuilding catalog pointers from the ledger, never from "the latest metadata";
- the conflict matrix;
- `logical_time` from the scheduler and `Freshness(now=...)`;
- planning inside the data plane;
- no raw-file credentials for masked tables;
- the JSONL event sink and `--import-history`;
- the `_ldp` system tables;
- one project catalog file;
- the product-qualified-lead definition, "incidents prevented" and the conversion guardrail;
- the garbage-collection guards.

### 15.2 Business-first: "Laketop, the open-core Iceberg write path"

**The design:** catalog-neutral, with the commit coordinator in the library, four deployables and a
Postgres queue. The business review rated it best.

**Why it lost narrowly:**

- **Its fencing was check-then-act:** it fenced heartbeats, not the commit.
- **It set `commit.retry.num-retries=10` on LDP tables.** That makes pyiceberg and Spark rebase
  blindly, which breaks its own idempotency proof and changes the customer's own Spark writers.
- **Its writer lanes were session-held advisory locks,** lost on failover.
- **It planned a new Spark 3.5 / Scala 2.12 jar,** ignoring the `spark/` job that is already pinned
  to Spark 4.1.3 and Iceberg 1.12.0.
- **It shipped a BYOC agent for three runtimes before product-market fit.**
- **It charged an LCU fee on customer compute.**
- **It had no conversion guardrail.**

**What we kept:**

- catalog neutrality and optional hosted Polaris;
- the ICP, the trigger, the anti-ICP and the beachheads;
- the Postgres-only control plane;
- table lanes, fixed to be rows in Postgres;
- source positions in the same snapshot as the data;
- release tags for publishes across tables;
- PII-safe metrics and quarantine in the customer's bucket;
- the agent's schedule cache and event buffer;
- `ldp plan --cost` and budget caps;
- the reconciled ARR model, the capital plan, the metrics that must hold at $1B, and the moat tests;
- trademark registration.

### 15.3 Other options we rejected

| Option | Why not |
|---|---|
| Set `commit.retry.num-retries=0` on LDP tables and keep `Transaction` commits | It is a table property, so it would silently remove retries from the customer's own Spark writers. It also does not solve the second-requirement drop needed for branch fencing |
| `ldp catalog serve`, a local REST server wrapping SQLite | A Polaris or Lakekeeper container does this already; building it is the start of building a catalog |
| Kafka from day one | 65 claims/s per cell is two orders of magnitude below where a bus pays off; the outbox table plus a polling relay is enough |
| A pooled hosted tier in year 1 | It would need a Python sandbox that has passed a pen test, a hosted catalog, and our own compute COGS before product-market fit; BYOC fits the ICP's data gravity and the local-first promise |
| Staying OSS only, with support and consulting | This is the fallback if Phase 0.5 fails, not the plan: services revenue does not compound, and the research found no platform outcome built on services |
| Pricing per row or per run | Unpredictable bills (the Airbyte lesson) and a tax on retries |

---

## 16. Open questions

1. **Demand.** Will at least 3 of 10 qualified teams pay $500–1,000/month for the concierge pilot,
   and which of ledger, coordination or cost do they say they are paying for?
2. **Governed-table unit.** How should tables that only *other* engines write, which the ledger
   discovers, be billed: free, 25%, or full price? Does procurement accept $1.50–2 per table-month at
   2,000 or more tables?
3. **Catalog semantics.** Do Glue (`VersionId`), S3 Tables REST, Unity, BigLake and R2 all validate
   two `assert-ref-snapshot-id` requirements atomically with the update? Phase 1 tests this against
   each catalog; any that fail get lanes as the correctness mechanism, documented.
4. **Upstream.** Will pyiceberg accept the `_stage` requirement fix and a public fast-forward? If
   not, how much of `pyiceberg.table.update` do we have to treat as a stable API?
5. **Catalog write limits.** What commit-rate limits do hosted catalogs impose per table and per
   account? This sets the fastest micro-batch schedule we can support.
6. **Schema at stage time.** Will customers accept additive, nullable schema changes becoming visible
   before a publish, or do we need a "schema-first" pre-publish step?
7. **Idempotency horizon.** Is 7 days (72 hours for PII) long enough for the longest real run-level
   retry and backfill windows?
8. **Self-serve without hosted compute.** Will Team convert when the customer must stand up a data
   plane (Terraform or `ldp agent` on a VM), or does Team need the pooled tier earlier than Phase 4?
9. **Agent licence.** Apache-2.0 in the wheel for trust, or source-available to stop a clone of the
   control plane from reusing it? The current choice is Apache-2.0.
10. **Local catalog migration.** Can we move from one SQLite file per identifier to one project
    `.ldp/catalog.db` without breaking `spark_catalog_conf` users and existing warehouses?
11. **Postgres headroom.** Does one 16-vCPU primary really sustain about 2k short writes per second
    per cell with heartbeats and lanes? A Phase 2 load test answers this before cell sizes are
    fixed.
12. **DuckLake.** Is there enough mid-market pull for a DuckLake target to be worth a second format?
    Revisit at the Series A.
