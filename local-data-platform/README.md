Dear User
# Local Data Platform
### Explain this to me like I am five 
Imagine you have a toy box where you keep all your favorite toys. 
A local data platform is like that toy box, but for storing and 
organizing important information instead of toys.
Just like how your toy box, 
**a local data platform keeps all your data** 
(like pictures, documents, and other info) **in one place 
so you can easily find, use and manage it.**

It's really handy for keeping everything organized and in one spot! 🌟📦

Got it? What else are you curious about?

> **Vision:** Local Data Platform is used as a python library to learn 
> and operate data lake house locally. <br/>
> **Mission:** Develop a python package which provides solutions for all stages
> of data organisation, ranging from ingestion to reporting. 
> The goal is that one can build data pipeline locally, test and 
> easily scale up to cloud.  <br/>
> <br/>
> **By 2025,** local-data-platform is a python package that uses open source 
> tools to orchestrate a data platform operation, locally, for development 
> and testing. <br/>

## Problem Statement


| Question | Answer                                                                                             |
|----------|----------------------------------------------------------------------------------------------------|
| What?    | a local data platform that can scale up to cloud                                                   |
| Why?     | save costs on cloud infra and development time                                                    |
| When?    | start of product development life cycle                                                            |
| Where?   | local first                                                                                        |
| Who?     | Business who wants a product data platform that will run locally and scale up when the time comes. |

# Technical Specifications
This will help you understand how to read the repository. \
**Note:** The users install the package and the developer import the library.

## Directory Structure

### local-data-platform/ `repository`
- **.github/** `hidden folder`
  - ISSUE-TEMPLATE/ `samples`
    - bug_report.md `Report bugs here`
    - custom.md `Report ad hoc issues here`
    - feature_request.md `Request a new feature here`
  - pull_request_template.md `Raise a pull request on the repo`

- **docs/** `Documentation for Read the Docs`

- **local-data-platform** `package`
  - local_data_platform `library`
    - hello_world.py `module`
      - hello_world `function`
        - prints 'Hello, world!' `output`

- **samples/** `tutorials`
    - bigQueryTutorial.py `Demo bigQuery compatibility here`
- .gitignore `Mention files to ignore in your PR`
- .readthedocs.yaml `Configuration for Read the Docs`
- LICENSE `for legal purposes`
- lumache.py `Template used in Sphinx projects for Read the Docs`
- pyproject.toml `template configuration`
- README.md `How to understand the repo`
- README.rst `Configuration for Read the Docs`

## How to test Pre-release as a User

1. Check the directory structure \
`ls`
2. Change directory to local-data-platform   
`cd local-data-platform`
2. Install the dependencies listed in your pyproject.toml file \
`$poetry install` 
2. Execute your test suite to ensure everything is working as expected \
`poetry run pytest`
3. Run hello world command \
`poetry run python hello_world.py`



## Package structure
- **local-data-platform** `package`
  - **dist** `Package distribution files`
  - **docs** `Documentation`
  - **local_data_platform** `library`
    - **catalog** `Catalog your data`
      - **local** `Catalog your data locally`
        - **iceberg** `Catalog your data in iceberg SQL lite db`
          - export.py  `Export your catalog data to csv`
    - **cloud** `Interact with cloud service providers`
      - **gcp** `Interact with Google Cloud Platform`
        - **login** `Login to GCP to get API credentials`
    - **engine** `Underlying processing Tech`
    - **format** `Supported formats for storage`
      - **csv** `Supports Google sheets and Excel sheets`
      - **iceberg** `Supports Apache Iceberg`
      - **parquet** `Supports Apache Parquet`
    - **issue** `Github Issues`
    - **pipeline** `Data Pipeline`
      - **egression** `Downstream pipelines`
        - **csv_to_iceberg** `Raw to Silver Layer`
        - **iceberg_to_csv** `Silver Layer to Gold Layer`
      - **ingestion** `Upstream pipelines`
        - **bigquery_to_csv** `Source to Raw`
        - **csv_to_iceberg** `Raw to Silver Layer`
        - **paraquet_to_iceberg** `Raw to Silver Layer`
      - **scraper** `HTML to CSV`
    - **store** `Data store`
      - **source** `Source data class`
        - **gcp** `GCP Storage`
          - **bigquery** `GCP service`
        - **json** `Local JSON file`
        - **near** `NEAR Data Lake`
        - **parquet** `Local Parquet file`
        - **target** `Target data class`
          - **iceberg** `Local Data Lake house`
    - etl.py `Sample pipeline`
    - exceptions.py `Known limitations`
    - hello_world.py `Test Feature`
    - is_function.py `Query Library Functions`
    - logger.py `Library logger`
  - **real_world_use_cases** `User Test Cases`
    - **near_data_lake** `NEAR Coin Transactions`
      - **config** `Pipeline configurations`
        - **sample_queries** `NEAR Data Lake Transaction Table`
          - near_transaction.json `Query List`
        - egression.json `Loading data in local data lake house`
        - ingestion.json `Extracting data from NEAR data lake house`
      - **data** `target path`
        - **near_transactions.db** `Local data lake house`
          - **transactions** `iceberg table`
            - **data** `table records`
            - **metadata** `iceberg table metadata`
        - near_transactions_catalog.db `iceberg local data catalog`
      - **reports** `Production analysis`
        - get_data.py `Get insights`
        - put_data.py `Refresh Gold Layer`
      - near_transactions.csv `Output`
    - **nyc_yello_taxi_dataset** `NYC Yello Taxis Rides`
      - **config** `Pipeline configurations`
        - egression.json `Loading data in local data lake house`
        - egression_payments.json `Loading payments report in Gold Layer`
        - ingestion.json `Extracting data from local parquet file`
      - **data** `target path`
        - **nyc_yello_taxi_dataset.db** `Local data lake house`
          - **rides** `iceberg table`
            - **data**  `table records`
            - **metadata** `iceberg table metadata`
        - nyc_yellow_taxi_dataset_catalog.db `iceberg local data catalog`
        - nyc_yellow_taxi_rides.csv `Ouput`
      - **reports** `Production analysis`
        - export_catalog.py `Saves local iceberg catalog in CSV`
        - get_data.py `Create Gold Layer`
        - get_report.py `Updates Gold Layer`
        - put_data.py `Refreshes Gold Layer`
      - monthly_reporting.md `Report in MD`
  - **tests** `PyTest Unit testing`
    - test_gcp_connection.py `Testing GCP Login`


## Plan

| Milestone | Epic                    | Target Date | Delivery Date | Comment                                 |
|-----------|-------------------------|-------------|---------------|-----------------------------------------|
| 0.1.0     | HelloWorld              | 1st Oct 24  | 1st Oct 24    | Good Start                              |
| 0.1.1     | Ingestion               | 31st Oct 24 | 5th Nov 24    | First Release: _Completed in 2 Sprints_ | 
| 0.1.2     | Warehousing             | 15th Nov 24 | TBD           | Coming Soon                             |
| 0.1.3     | Orchestration           | 29th Nov 24 | TBD           | Coming Soon                             |
| 0.1.4     | Self Serving Gold Layer | 29th Nov 24 | TBD           | Coming Soon                             |
| 0.1.5     | Monitoring              | 29th Nov 24 | TBD           | Coming Soon                             |
| 0.1.6     | BI Reporting Dashboard  | 31st Dec 24 | TBD           | Coming Soon                             |
| 0.1.7     | Data Science Insights   | 31st Dec 24 | TBD           | Coming Soon                             |
| 0.1.8     | LLM                     | 31st Dec 24 | TBD           | Coming Soon                             |
| 0.1.9     | Launch Documentation    | 30th Nov 24 | TBD           | Coming Soon                             |
| 1.0.0     | Ready for Production    | 1st Nov 24  | TBD           | End Game                                |

  

### Releases

- [x] 0.1.0 : Done- Published Library on [PyPI](https://pypi.org/project/local-data-platform/)

- [ ] 0.1.1 : In Progress- [Demo BigQuery compatibility](https://github.com/tusharchou/local-data-platform/milestone/2)

- [x] 0.1.1 : Done- [Documentation: Updated README to explain clearly problem and plan of excecution](https://github.com/tusharchou/local-data-platform/issues/6)

- [ ] 0.1.2 : To-do- [Warehousing: DuckDB, Iceberg, DBT](https://github.com/tusharchou/local-data-platform/milestone/5)
- [ ] 0.1.3 : To-do- [Orchestration](https://github.com/tusharchou/local-data-platform/milestone/6)
- [ ] 0.1.4 : To-do- [Self Serving Gold Layer](https://github.com/tusharchou/local-data-platform/milestone/11)
- [ ] 0.1.5 : To-do- [Monitoring](https://github.com/tusharchou/local-data-platform/milestone/10)
- [ ] 0.1.6 : To-do- [Business Intelligence Reporting Dashboard](https://github.com/tusharchou/local-data-platform/milestone/9)
- [ ] 0.1.7 : To-do- [Data Science Insights](https://github.com/tusharchou/local-data-platform/milestone/8)
- [ ] 0.1.8 : To-do- [LLM](https://github.com/tusharchou/local-data-platform/milestone/7)
- [ ] 0.1.9 : To-do- [Launch Documentation](https://github.com/tusharchou/local-data-platform/milestone/2)
- [ ] 0.2.0 : To-do- [Cloud Integration](https://github.com/tusharchou/local-data-platform/milestone/3)
- [ ] 1.0.0 : To-do- Product


### References

  

[iceberg-python](https://py.iceberg.apache.org)

[near-data-lake](https://docs.near.org/concepts/advanced/near-lake-framework)

[duckdb](https://duckdb.org/docs/extensions/iceberg.html)

  

#### Self Promotion

  

[Reliable Change Data Capture using Iceberg](https://medium.com/@tushar.choudhary.de/reliable-cdc-apache-spark-ingestion-pipeline-using-iceberg-5d8f0fee6fd6)

[Introduction to pyiceberg](https://medium.com/@tushar.choudhary.de/internals-of-apache-pyiceberg-10c2302a5c8b)