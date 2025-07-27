# Local Data Platform

**local-data-platform** is a Python library to build, test, and run a complete data platform on your local machine. The core idea is to provide a "toy box for data"—a local environment where you can manage the entire data lifecycle, from ingestion to reporting, before needing to scale up to a cloud environment.

This approach allows developers and businesses to save on cloud infrastructure costs during the initial development and testing phases, with a clear path for future scaling.

> **Vision:** Local Data Platform is used as a python library to learn 
> and operate data lake house locally. <br/>
> **Mission:** Develop a python package which provides solutions for all stages
> of data organisation, ranging from ingestion to reporting. The goal is that one can build data pipelines locally, test them, and easily scale up to the cloud.

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

The project follows a standard `src` layout for Python packages. Key directories include:
- **src/local_data_platform/**: The main source code for the library.
- **docs/**: MkDocs documentation sources.
- **tests/**: The Pytest test suite.

## How to test Pre-release as a User

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/tusharchou/local-data-platform.git
    cd local-data-platform
    ```
2.  **Install dependencies:**
    This project uses Poetry for dependency management. Use the Makefile for convenience.
    ```bash
    make install
    ```
3.  **Run the tests:**
    ```bash
    make test
    ```



## Package Modules

The library's main modules are located in `src/local_data_platform`. Key modules include:

*   **`store`**: Handles data storage and interaction with sources.
*   **`pipeline`**: Provides tools for building ETL pipelines.
*   **`catalog`**: Manages data cataloging with Apache Iceberg.
*   **`cloud`**: Contains components for interacting with cloud services.
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

#### Completed
- **v0.1.0**: Initial release on [PyPI](https://pypi.org/project/local-data-platform/).
- **v0.1.1**: Implemented data ingestion and improved documentation.

#### Upcoming

- **v0.1.2**: Warehousing with DuckDB, Iceberg, and dbt.
- **v0.1.3**: Pipeline orchestration.
- **v0.1.9**: Full documentation launch.
- **v0.2.0**: Cloud integration features.
- **v1.0.0**: Production-ready release.


### References

  

[iceberg-python](https://py.iceberg.apache.org)

[near-data-lake](https://docs.near.org/concepts/advanced/near-lake-framework)

[duckdb](https://duckdb.org/docs/extensions/iceberg.html)

  

#### Self Promotion

  

[Reliable Change Data Capture using Iceberg](https://medium.com/@tushar.choudhary.de/reliable-cdc-apache-spark-ingestion-pipeline-using-iceberg-5d8f0fee6fd6)

[Introduction to pyiceberg](https://medium.com/@tushar.choudhary.de/internals-of-apache-pyiceberg-10c2302a5c8b)