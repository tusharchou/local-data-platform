# Local Data Platform 

Business information systems require fresh data every day organised in a manner that retrival is cost effective.
Making a local data platform requires a setup where you can recreate production usecases and develop new pipelines.

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