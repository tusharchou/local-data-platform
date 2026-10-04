# Welcome to the Local Data Platform (LDP)!

**Empowering Personal Data Mastery with Python.**

The Local Data Platform (LDP) is a revolutionary open-source initiative designed to put the power of "big data" Python libraries directly into the hands of individuals. Whether you're a data enthusiast, a researcher, or simply someone looking to gain deeper insights from your personal information, LDP provides the tools and framework to do so securely and privately, right on your laptop.

---

## Problem Statement

_The full problem statement is detailed on the [Problem Statement](./PROBLEM_STATEMENT.md) page._

---

## Why Local?

In an era where data privacy is paramount, LDP champions a local-first approach. Your data stays on your machine, under your control. This eliminates the need to upload sensitive information to third-party cloud services, giving you peace of mind while still enabling powerful analysis.

## Key Features

What works today:

* **Offline capability:** The default setup (a SQLite catalog and a local warehouse folder) needs no
  server, no cloud account and no network.
* **Privacy by design:** Your data stays in folders you choose unless you point a config at a remote
  catalog or bucket.
* **Extensible architecture:** New sources, targets and catalog types plug in through registries
  (`register_pipeline`, `register_catalog_type`).

What is planned, not built:

* **More processing libraries:** Pandas, Dask or Polars engines. Today tables move as Apache Arrow
  and SQL runs on DuckDB (Spark is experimental).
* **Community-driven solutions:** Shared templates for common personal data problems.

## Get Started

Ready to take control of your personal data? Head over to our [Quickstart](../quickstart.md) guide to set up LDP on your machine.