> [!NOTE]
> Hi Followers,
> Thank you for taking the time to read me. Let me help you understand the scope and progress with better ease below:
> 1. [Milestones](https://github.com/tusharchou/local-data-platform/milestones)
> 2. README.md (This document) 
> 3. [Enhancements](https://github.com/tusharchou/local-data-platform/pulls)
> 6. Documentation (Coming Soon)

Updated At Thu 3 Oct 2024

# Local Data Engineering Toolkit in Python

## Plan

| Milestone | Epic                 | Target Date | Delivery Date | Release Owner   | Comment      |
|-----------|----------------------|-------------|---------------|-----------------|--------------|
| 0.1.0     | HelloWorld           | 1st Oct 24  | 1st Oct 24    | @tusharchou     | Good Start   |
| 0.1.1     | Ingestion            | 3rd Oct 24  | 9th Oct 24    | @tusharchou     | First Sprint | 
| 0.1.2     | Warehousing          | 18th Oct 24 | TBD           | @tusharchou     | Coming Soon  |
| 1.0.0     | Ready for Production | 1st Nov 24  | TBD           | TBD             | End Game     |

### Milestone

- [x] 0.1.0 : Done+ Published Library on [PyPI](https://pypi.org/project/local-data-platform/)

- [ ] 0.1.1 : In Progress- [Demo BigQuery compatibility](https://github.com/tusharchou/local-data-platform/milestone/2)
  - [x] 0.1.1 : Done+ [Documentation: Updated README to explain clearly problem and plan of excecution](https://github.com/tusharchou/local-data-platform/issues/6) 
  - [ ] PR : In Progress- [Feature: Simply query NEAR Coin GCP Data Lake through BiqQuery](https://github.com/tusharchou/local-data-platform/pull/25)
  - [ ] PR : In Progress- [Feature: Privately store NYC Yellow Taxi Rides Data in Local Data Platform](https://github.com/tusharchou/local-data-platform/pull/26)
  - [ ] FR : In Progress- [Change: Easily solve for User's Local Data Need](https://github.com/tusharchou/local-data-platform/pull/28)
  - [ ] IS : In Progress- [Documentation: Align on Product Framework](https://github.com/tusharchou/local-data-platform/issues/29)
  - [ ] IS : In Progress- [Request: Source Parquet Table](https://github.com/tusharchou/local-data-platform/issues/24)
  - [ ] IS : In Progress- [Request: Source Iceberg Table](https://github.com/tusharchou/local-data-platform/issues/21)
  - [ ] IS : In Progress- [Request: Target Iceberg Table](https://github.com/tusharchou/local-data-platform/issues/22)
  - [ ] IS : In Progress- [Request: Target.put() Iceberg Table](https://github.com/tusharchou/local-data-platform/issues/20)
  - [ ] IS : In Progress- [Request: NYCYellowTaxi.rides.put()](https://github.com/tusharchou/local-data-platform/issues/8)
  - [ ] IS : In Progress- [Request: NYCYellowTaxi.rides.get()](https://github.com/tusharchou/local-data-platform/issues/3)
  - [ ] IS : In Progress- [Request: test.iceberg.exception()](https://github.com/tusharchou/local-data-platform/issues/1)
  - [ ] IS : In Progress- [Documentation: NEAR Trader-How to use NEAR Data Lake](https://github.com/tusharchou/local-data-platform/issues/12)
  - [ ] IS : In Progress- [Request: Source.get() BigQuery](https://github.com/tusharchou/local-data-platform/issues/19)
  - [ ] IS : To-do- [Request: Iceberg Partitioning and Version Control](https://github.com/tusharchou/local-data-platform/issues/29)
  - [ ] IS : To-do- [Request: Align on Product Framework](https://github.com/tusharchou/local-data-platform/issues/29)
  - [ ] IS : In Progress- [Align on Product Framework](https://github.com/tusharchou/local-data-platform/issues/29)
- [ ] 0.1.2 : To-do Continuous Integration
- [ ] 0.1.9 : To-do[Launch Documentation](https://github.com/tusharchou/local-data-platform/milestone/2)
- [ ] 0.2.0 : To-do [Cloud Integration](https://github.com/tusharchou/local-data-platform/milestone/3)
- [ ] 1.0.0 : To-do [Demo BigQuery compatibility](https://github.com/tusharchou/local-data-platform/milestone/2)

# Local Data Platform 

Business information systems require fresh data every day organised in a manner that retrival is cost effective.
Making a local data platform requires a setup where you can recreate production usecases and develop new pipelines.

## Problem Statement

| Question | Answer                                                                                            |
|----------|---------------------------------------------------------------------------------------------------|
| What?    | a local data platform that can scale up to cloud                                                  |
| Why?     | save costs on cloud infra and developement time                                                   |
| When?    | start of product development life cycle                                                           |
| Where?   | local first                                                                                       |
| Who?     | Business who want a product data platform that will run locally and scale up when the time comes. |

> A python library that uses open source tools to orchestrate a data platform operations locally for development and testing

## Components 

1. Orchestrator 
   - cron
   - Airflow
2. Source
   - APIs
   - Files
3. Target
   - Iceberg
   - DuckDB
   - Space and Time
4. Catalog
   - Rest

### Source

#### Parquet

Data can be available as single file in the source format. For example New York Yellow taxi data is available to be 
pulled from [here](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)

```
curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o /tmp/yellow_tripdata_2023-01.parquet
```
`local-data-platform/`

### Target

1. CSV
2. Google Sheet
3. Iceberg 

### References

[iceberg-python](https://py.iceberg.apache.org)
[near-data-lake](https://docs.near.org/concepts/advanced/near-lake-framework)
[duckdb](https://duckdb.org/docs/extensions/iceberg.html)

#### Self Promotion

[Reliable Change Data Capture using Iceberg](https://medium.com/@tushar.choudhary.de/reliable-cdc-apache-spark-ingestion-pipeline-using-iceberg-5d8f0fee6fd6)
[Introduction to pyiceberg](https://medium.com/@tushar.choudhary.de/internals-of-apache-pyiceberg-10c2302a5c8b)