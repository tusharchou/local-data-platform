import os
from local_data_platform.store.source.gcp import GCP, GCPCredentials
from google.cloud import bigquery
from google.oauth2 import service_account
from local_data_platform.format.csv import CSV
from pathlib import Path
from local_data_platform.logger import log
from pandas import DataFrame as Dataframe
from pyarrow import Table as Table

logger = log()


class BigQuery(GCP):
    """
    A base class for BigQuery Store implementation
    """

    def __init__(self, name: str, credentials: GCPCredentials, path: Path):
        self.name = name
        self.credentials = credentials
        self.project_id = self.credentials.project_id
        self.path = path
        self.client = self._get_bigquery_client()
        logger.info(f"BigQuery initialised with {self.path}")
        super().__init__(self.name, self.path)

    def get(self, query: str) -> Table:
        logger.info(f"Getting data from BigQuery {self.name}")
        logger.info(f"Query: {query}")
        try:
            logger.info(f"query_job: {self.client.query(query)}")
            query_job = self.client.query(query)
            df = query_job.to_dataframe()
            print({type(df)})
        except Exception as e:
            logger.error(f"Error getting data from BigQuery {self.name}")
            logger.error(e)
            raise e
        logger.info(f"Data from BigQuery {self.name} fetched successfully")
        logger.info(f"Data: {len(df)} type: {type(df)}")
        return Table.from_pandas(df)

    def _get_bigquery_client(self) -> bigquery.Client:
        credentials = service_account.Credentials.from_service_account_file(
            self.credentials.path
        )
        client = bigquery.Client(credentials=credentials, project=self.project_id)
        return client

    def put(self):
        pass
