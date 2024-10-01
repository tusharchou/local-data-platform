from local_data_platform.source.gcp.gcp_bigquery.source_gcp_bigquery import SourceGCPBigQuery
"""
This module defines the Source_Near_GCP class, which inherits from SourceGCPBigQuery
and provides functionality to fetch transaction data from the NEAR protocol on Google Cloud Platform.

Classes:
    Source_Near_GCP: A class to fetch NEAR protocol transaction data from GCP BigQuery.

Methods:
    fetch_near_transaction(self):
        Fetches NEAR protocol transaction data from the previous day.
        Returns:
            DataFrame: A pandas DataFrame containing the transaction data.
"""
class Source_Near_GCP(SourceGCPBigQuery):

    def fetch_near_transaction(self):
        query = """
            SELECT *
            FROM `bigquery-public-data.crypto_near.transactions`
            LIMIT 1000;
        """
        return self.fetch_data(query)