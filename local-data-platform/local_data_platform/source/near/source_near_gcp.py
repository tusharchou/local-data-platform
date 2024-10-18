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
    """
    A class to represent the data source for Near transactions from Google Cloud Platform (GCP) BigQuery.
    Methods
    -------
    fetch_near_transaction():
        Reads an SQL query from a file and fetches Near transaction data from GCP BigQuery.
    """
    """
        Reads an SQL query from a specified file and fetches Near transaction data from GCP BigQuery.
        The method reads the SQL query from 'local_data_platform/source/near/queries/near_transactions.sql',
        prints the query for debugging purposes, and then uses the query to fetch data from BigQuery.
    Returns
    -------
    DataFrame
        A DataFrame containing the fetched Near transaction data.
    """
    def fetch_near_transaction(self):
        
        def read_query_from_file(file_path):
            with open(file_path, 'r') as file:
                query = file.read()
            return query

        # Path to the configuration file
        query_file_path = 'local_data_platform/source/near/queries/near_transactions.sql'

        # Read the query from the file
        query = read_query_from_file(query_file_path)

        print("SQL QUERY: ",query)

        return self.fetch_data(query)


        