from source.gcp.gcp_connection import GCPBigQueryConnection

class SourceGCPBigQuery:
    """
    A class to interact with Google BigQuery.
    client : GCPBigQueryConnection
        An instance of GCPBigQueryConnection to interact with BigQuery.
    """
    def __init__(self):
        self.client = GCPBigQueryConnection('path/to/credentials.json', 'your-project-id')

    def fetch_data(self, query):
        """
        Executes a given SQL query on Google BigQuery and returns the result as a pandas DataFrame.

        Args:
            query (str): The SQL query to be executed.

        Returns:
            pandas.DataFrame: The result of the query as a DataFrame.
        """
        query_job = self.client.query(query)
        return query_job.to_dataframe()