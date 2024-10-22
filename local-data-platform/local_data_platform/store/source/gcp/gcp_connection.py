from google.cloud import bigquery
from google.oauth2 import service_account

class GCPBigQueryConnection:
    """
    GCPBigQueryConnection is a class that facilitates connection to Google Cloud Platform's BigQuery service using service account credentials.

    Attributes:
        credentials_path (str): The file path to the service account credentials JSON file.
        project_id (str): The GCP project ID.

    Methods:
        __init__(credentials_path, project_id):
            Initializes the GCPBigQueryConnection with the given credentials path and project ID.
        
        _create_client():
        
        query(query_string):
            Executes a SQL query on BigQuery and returns the results.


        Example usage:
        connection = GCPBigQueryConnection('/path/to/credentials.json', 'your-project-id')
        results = connection.query('SELECT * FROM your_dataset.your_table')
        for row in results:
            print(row)    
    """
    def __init__(self, credentials_path, project_id):
        self.credentials_path = credentials_path
        self.project_id = project_id
        self.client = self._create_client()

    def _create_client(self):
        """
        Creates a BigQuery client using service account credentials.

        This method reads the service account credentials from a file specified
        by `self.credentials_path` and uses them to create a BigQuery client
        for the project specified by `self.project_id`.

        Returns:
            bigquery.Client: An authenticated BigQuery client.
        """
        credentials = service_account.Credentials.from_service_account_file(self.credentials_path)
        client = bigquery.Client(credentials=credentials, project=self.project_id)
        return client

    def query(self, query_string):
        """
        Executes a SQL query on the GCP BigQuery client and returns the results.

        Args:
            query_string (str): The SQL query to be executed.

        Returns:
            google.cloud.bigquery.table.RowIterator: An iterator over the rows in the query result.
        """
        query_job = self.client.query(query_string)
        results = query_job.result()
        return results
