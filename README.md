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


## Components


It uses below tools:
1. Ingestion using [Apache Arrow](https://arrow.apache.org/) in [Parquet](https://parquet.apache.org/) file format.
2. Data Catalog using [Iceberg](https://iceberg.apache.org/)
3. [DuckDB](https://duckdb.org/) as Datawarehouse
4. [DBT](https://www.getdbt.com/) for transformation operations.
5. [Apache Airflow](https://airflow.apache.org/) for orchestration

## Plan

| Milestone | Epic                 | Target Date | Delivery Date | Release Owner   | Comment      |
|-----------|----------------------|-------------|---------------|-----------------|--------------|
| 0.1.0     | HelloWorld           | 1st Oct 24  | 1st Oct 24    | @tusharchou     | Good Start   |
| 0.1.1     | Ingestion            | 3rd Oct 24  | 9th Oct 24    | @tusharchou     | First Sprint | 
| 0.1.2     | Warehousing          | 18th Oct 24 | TBD           | @tusharchou     | Coming Soon  |
| 1.0.0     | Ready for Production | 1st Nov 24  | TBD           | TBD             | End Game     |

  

### Milestone

- [x] 0.1.0 : Done- Published Library on [PyPI](https://pypi.org/project/local-data-platform/)

- [ ] 0.1.1 : In Progress- [Demo BigQuery compatibility](https://github.com/tusharchou/local-data-platform/milestone/2)

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

- [ ] 0.1.2 : To-do [Warehousing: DuckDB, Iceberg, DBT](https://github.com/tusharchou/local-data-platform/milestone/5)
- [ ] 0.1.3 : To-do [Orchestration](https://github.com/tusharchou/local-data-platform/milestone/6)
- [ ] 0.1.4 : To-do [Self Serving Gold Layer](https://github.com/tusharchou/local-data-platform/milestone/11)
- [ ] 0.1.5 : To-do [Monitoring](https://github.com/tusharchou/local-data-platform/milestone/10)
- [ ] 0.1.6 : To-do [Business Intelligence Reporting Dashboard](https://github.com/tusharchou/local-data-platform/milestone/9)
- [ ] 0.1.7 : To-do [Data Science Insights](https://github.com/tusharchou/local-data-platform/milestone/8)
- [ ] 0.1.8 : To-do [LLM](https://github.com/tusharchou/local-data-platform/milestone/7)
- [ ] 0.1.9 : To-do [Launch Documentation](https://github.com/tusharchou/local-data-platform/milestone/2)
- [ ] 0.2.0 : To-do [Cloud Integration](https://github.com/tusharchou/local-data-platform/milestone/3)
- [ ] 1.0.0 : To-do Product


### References

  

[iceberg-python](https://py.iceberg.apache.org)

[near-data-lake](https://docs.near.org/concepts/advanced/near-lake-framework)

[duckdb](https://duckdb.org/docs/extensions/iceberg.html)

  

#### Self Promotion

  

[Reliable Change Data Capture using Iceberg](https://medium.com/@tushar.choudhary.de/reliable-cdc-apache-spark-ingestion-pipeline-using-iceberg-5d8f0fee6fd6)

[Introduction to pyiceberg](https://medium.com/@tushar.choudhary.de/internals-of-apache-pyiceberg-10c2302a5c8b)