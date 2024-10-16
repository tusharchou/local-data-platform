# Local Data Platform
Local Data Platform is a python library that uses open source tools to orchestrate a data platform operations locally for development and testing. <br/>
This library provides solutions for all stages ranging from ingestion to reporting all of which one can build data pipeline locally, test and easily scale up to cloud. 

## Problem Statement


| Question | Answer                                                                                            |
|----------|---------------------------------------------------------------------------------------------------|
| What?    | a local data platform that can scale up to cloud                                                  |
| Why?     | save costs on cloud infra and developement time                                                   |
| When?    | start of product development life cycle                                                           |
| Where?   | local first                                                                                       |
| Who?     | Business who want a product data platform that will run locally and scale up when the time comes. |

## Components

It uses below tools:
1. Ingestion using [Apache Arrow](https://arrow.apache.org/) in [Parquet](https://parquet.apache.org/) file format.
2. Data Catalog using [Iceberg](https://iceberg.apache.org/)
3. [DuckDB](https://duckdb.org/) as Datawarehouse
4. [DBT](https://www.getdbt.com/) for transformation operations.
5. [Apache Airflow](https://airflow.apache.org/) for orchestration
### Source

Our local data platform supports `Parquet Files` for now and new formats and sources will be added in subsequent releases.<br/>

`Parquet Files` : High-performance columnar storage format, optimized for efficient reading and querying of large datasets.<br  />

  

### Data Catalog with Apache Iceberg on SQLite

Our platform uses Apache Iceberg to manage large-scale datasets efficiently while ensuring ACID compliance, schema evolution, and performant queries.<br  />

Our platform leverages Apache Iceberg as the data catalog on top of SQLite for storing and transforming raw data.<br  />

Initially, raw data in form of Parquet files are ingested into SQLite. SQLite, being a lightweight, serverless database, serves as an intermediary layer where the data can be stored, processed, and transformed as needed. Apache Iceberg acts as the data catalog throughout the process. It manages metadata for all datasets, including raw data in SQLite.

  

### Transformations

Once raw data is ingested into SQLite, we use `DBT` (Data Build Tool) for transforming and modeling the data.

  

### Target

Once the transformations are complete, the processed and clean data is stored in `DuckDB` using Apache Iceberg's table format.<br  />

Apache Iceberg acts as a unified metadata layer across both the raw and processed data. The platform can handle complex data versioning, schema evolution, and partition pruning, ensuring optimal performance during querying. With DuckDB’s in-memory analytical capabilities and Iceberg’s efficient data layout, querying the processed data becomes highly performant and scalable.



## Example
#### Sample Data

Data can be available as single file in the source format. For example New York Yellow taxi data is available to be

pulled from [here](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)

  

```

curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o /tmp/yellow_tripdata_2023-01.parquet

```
#### Ingestion Layer
Please refer given ingestion layer [python script](https://github.com/tusharchou/local-data-platform/blob/main/local-data-platform/nyc_yellow_taxi.py)

#### Subsequent Layers
[yet to be released](null) . Please check the Plan and milestone below. 

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


### References

  

[iceberg-python](https://py.iceberg.apache.org)

[near-data-lake](https://docs.near.org/concepts/advanced/near-lake-framework)

[duckdb](https://duckdb.org/docs/extensions/iceberg.html)

  

#### Self Promotion

  

[Reliable Change Data Capture using Iceberg](https://medium.com/@tushar.choudhary.de/reliable-cdc-apache-spark-ingestion-pipeline-using-iceberg-5d8f0fee6fd6)

[Introduction to pyiceberg](https://medium.com/@tushar.choudhary.de/internals-of-apache-pyiceberg-10c2302a5c8b)