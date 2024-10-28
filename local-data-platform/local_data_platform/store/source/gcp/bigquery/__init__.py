import os
from local_data_platform.store.source.gcp import GCP,GCPCredentials
from google.cloud import bigquery
from google.oauth2 import service_account
from local_data_platform.format.csv import CSV


class BigQuery(GCP): 
    """
    A base class for BigQuery Store implementation
    """
    def __init__(self,name: str,path: str):
        self.name = name
        self.credentials = GCPCredentials(path)
        self.project_id = self.credentials.project_id
        self.client = self._get_bigquery_client()
        super().__init__(self.name,self.path)

    def get(self,query: str) -> CSV:
        query_job = self.client.query(query)
        df = query_job.to_dataframe()
        return CSV.put(df)
       
        
    def _get_bigquery_client(self) -> bigquery.Client:
        credentials = service_account.Credentials.from_service_account_file(self.credentials.path)
        client = bigquery.Client(credentials=credentials, project=self.project_id)
        return client
    
    def put(self):
        pass


    