from google.cloud import bigquery

class SourceGCPBigQuery :

    def fetch_data(query):
        client = bigquery.Client()
        query_job = client.query(query)
        return query_job.to_dataframe()
    