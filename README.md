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

| Milestone  | Epic | Target Date |
| ------------- | ------------- | ------------ |
| 0.0.1  | Ready for Feedback  | 1st Oct 24 |
| 1.0.0  | Ready for Production | 1st Nov 24 |

### Milestone

- [ ] 0.0.1 : Minimum viable product
  - [ ] 1. [#6](https://github.com/tusharchou/local-data-platform/issues/6) : README 🥇 explain with clarity what I want to build and when
  - [ ] 2. [#1](https://github.com/tusharchou/local-data-platform/issues/1) : pyiceberg 0.7.1 list limitations [#2](https://github.com/tusharchou/local-data-platform/pull/2)
  - [ ] 3. [#3](https://github.com/tusharchou/local-data-platform/issues/3) : nyc data proof of concept with duck db
  - [ ] 4. [#5](https://github.com/tusharchou/local-data-platform/issues/5) : roll out library on pipy 

[#6](https://github.com/tusharchou/local-data-platform/issues/6)

# Local Data Platform 

Business information systems require fresh data every day organised in a manner that retrival is cost effective.
Making a local data platform requires a setup where you can recreate production usecases and develop new pipelines.

## Problem Statement

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

#### CSV

Human readable format and accessible platforms like google sheets or notion
Easily pushed into 
### References


#### Self Promotion
