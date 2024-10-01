from local_data_platform.source.gcp.gcp_bigquery import SourceGCPBigQuery

class Source_Near_GCP(SourceGCPBigQuery):

    def fetch_near_transaction(self):
        