> [!NOTE]
> Hi Followers,
> Thank you for taking the time to read me. Let me help you understand the scope and progess with better ease below:
> 1. Projects
> 2. Milestones
> 3. Issues
> 4. Pull Request
> 5. Wiki
> 6. Documentation

## Plan

| Milestone | Epic                 | Target Date |
|-----------|----------------------|-------------|
| 0.1.0     | Hello world          | 1st Oct 24  |
| 0.1.1     | Source: BiqQuery     | 2nd Oct 24  |
| 0.1.2     | Target: Iceberg      | 1st Oct 24 |
| 1.0.0     | Ready for Production | 1st Nov 24  |

### Milestone

- [ ] 0.1.0 : Minimum viable product
  - [ ] 1. [#6](https://github.com/tusharchou/local-data-platform/issues/6) : README 🥇 explain with clarity what I want to build and when
  - [ ] 4. [#5](https://github.com/tusharchou/local-data-platform/issues/5) : roll out library on pipy 
- [ ] 0.1.1
  - [ ] 2. [#1](https://github.com/tusharchou/local-data-platform/issues/1) : pyiceberg 0.7.1 list limitations [#2](https://github.com/tusharchou/local-data-platform/pull/2)
  - [ ] 3. [#3](https://github.com/tusharchou/local-data-platform/issues/3) : nyc data proof of concept with duck db
  - [ ] 5. [#24] target iceberg
  - [ ] 6. [#19] source big query

# Local Data Platform 

Business information systems require fresh data every day organised in a manner that retrival is cost effective.
Making a local data platform requires a setup where you can recreate production usecases and develop new pipelines.

## Problem Statement
| Question | Assumption |
|----------|------------|
| What? | a local data platform that can scale up to cloud |
| Why? | save costs on cloud infra and developement time |
| When? | start of product development life cycle |
| Where? | local first |
| Who? | Business who want a product data platform that will run locally and scale up when the time comes. |

### A python library that uses open source tools to orchestrate a data platform operations locally for development and testing

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

#### Bulk data

Data can be available as single file in the source format. For example New York Yellow taxi data is available to be 
pulled from [here](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)

```
curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o /tmp/yellow_tripdata_2023-01.parquet
```
`local-data-platform/`

### Target

#### Iceberg
#### CSV

Human readable format and accessible platforms like google sheets or notion
Easily pushed into 
### References


#### Self Promotion
