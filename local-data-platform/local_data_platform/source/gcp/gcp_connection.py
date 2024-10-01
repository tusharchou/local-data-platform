from google.cloud import bigquery
from google.oauth2 import service_account

class GCPBigQueryConnection:
    def __init__(self, credentials_path, project_id):
        self.credentials_path = credentials_path
        self.project_id = project_id
        self.client = self._create_client()

    def _create_client(self):
        credentials = service_account.Credentials.from_service_account_file(self.credentials_path)
        client = bigquery.Client(credentials=credentials, project=self.project_id)
        return client

    def query(self, query_string):
        query_job = self.client.query(query_string)
        results = query_job.result()
        return results

# Example usage:
# connection = GCPBigQueryConnection('/path/to/credentials.json', 'your-project-id')
# results = connection.query('SELECT * FROM your_dataset.your_table')
# for row in results:
#     print(row)