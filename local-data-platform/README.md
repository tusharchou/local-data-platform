
It uses below tools:
1. Ingestion using [Apache Arrow](https://arrow.apache.org/) in [Parquet](https://parquet.apache.org/) file format.
2. Data Catalog using [Iceberg](https://iceberg.apache.org/)
3. [DuckDB](https://duckdb.org/) as Datawarehouse
4. [DBT](https://www.getdbt.com/) for transformation operations.
5. [Apache Airflow](https://airflow.apache.org/) for orchestration


### Milestones

- [x] 0.1.1 : Done- [Documentation: Updated README to explain clearly problem and plan of excecution](https://github.com/tusharchou/local-data-platform/issues/6)

  - [x] 0.1.1.A : Done- [Feature: Simply query NEAR Coin GCP Data Lake through BiqQuery](https://github.com/tusharchou/local-data-platform/pull/25)

  - [x] 0.1.1.B : Done- [Feature: Privately store NYC Yellow Taxi Rides Data in Local Data Platform](https://github.com/tusharchou/local-data-platform/pull/26)

  - [ ] 0.1.1.C : In Progress- [Change: Easily solve for User's Local Data Need](https://github.com/tusharchou/local-data-platform/pull/28)

  - [ ] 0.1.1.D : In Progress- [Documentation: Align on Product Framework](https://github.com/tusharchou/local-data-platform/issues/29)

  - [x] 0.1.1.E : Done- [Request: Source Parquet Table](https://github.com/tusharchou/local-data-platform/issues/24)

  - [x] 0.1.1.F : Done- [Request: Source Iceberg Table](https://github.com/tusharchou/local-data-platform/issues/21)

  - [x] 0.1.1.G : Done- [Request: Target Iceberg Table](https://github.com/tusharchou/local-data-platform/issues/22)

  - [x] 0.1.1.H : Done- [Request: Target.put() Iceberg Table](https://github.com/tusharchou/local-data-platform/issues/20)

  - [x] 0.1.1.I : Done- [Request: NYCYellowTaxi.rides.put()](https://github.com/tusharchou/local-data-platform/issues/8)

  - [x] 0.1.1.J : Done- [Request: NYCYellowTaxi.rides.get()](https://github.com/tusharchou/local-data-platform/issues/3)

  - [x] 0.1.1.K : Done- [Request: test.iceberg.exception()](https://github.com/tusharchou/local-data-platform/issues/1)

  - [ ] 0.1.1.L : In Progress- [Documentation: NEAR Trader-How to use NEAR Data Lake](https://github.com/tusharchou/local-data-platform/issues/12)

  - [x] 0.1.1.M : Done- [Request: Source.get() BigQuery](https://github.com/tusharchou/local-data-platform/issues/19)

  - [ ] 0.1.1.N : To-do- [Request: Iceberg Partitioning and Version Control](https://github.com/tusharchou/local-data-platform/issues/29)

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
