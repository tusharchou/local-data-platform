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
